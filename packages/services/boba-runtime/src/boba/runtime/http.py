"""HTTP-граница входа, общая для chainlit и studio: BaseError -> статус и тело,
запрос SSO -> модель сервиса, токен входа из cookie или Authorization, cookie
сессии в ответе и ответы SPNEGO-обмена (401 Negotiate со страницей-переходом).
"""

import html
import logging
from collections.abc import Mapping
from http.cookies import SimpleCookie
from typing import Any, ClassVar

from starlette.requests import HTTPConnection, Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

from boba.identity.errors import BaseError, FailureReport, to_domain
from boba.identity.session import LogLine
from boba.identity.signin import ProxyHeaderNames, ProxyRequest
from boba.identity.sso import OwnRequest, RequestHeader, SsoErrorCode, SsoRequest
from boba.identity.token import (
    CookieJar,
    CookieSpec,
    TokenReader,
    TokenRejectedError,
    TokenRejection,
)

__all__ = [
    "DomainErrorMiddleware",
    "ProxyRequests",
    "RequestTokens",
    "SessionCookie",
    "SsoRequests",
    "SsoResponses",
    "StaleSessionMiddleware",
]


class SessionCookie:
    """Cookie входа в ответе HTTP: атрибуты из CookieSpec, чанки — CookieJar.

    Одна реализация на оба приложения: chainlit читает её своим кодом cookie,
    что проверяет test_session_cookie_shared.
    """

    def __init__(self, spec: CookieSpec) -> None:
        self._spec = spec
        self._jar = CookieJar(spec.name)

    @property
    def jar(self) -> CookieJar:
        return self._jar

    def put(self, response: Response, present: Mapping[str, str], token: str) -> None:
        """Ставит токен и снимает чанки прежнего, более длинного токена."""
        pieces = self._jar.pieces(token)
        for key, value in pieces:
            self._set(response, key, value)

        for key in self._jar.stale(present, pieces):
            self._delete(response, key)

    def token_of(self, present: Mapping[str, str]) -> str | None:
        """Токен из cookie запроса: целиком либо из чанков."""
        return self._jar.token_of(present)

    def clear(self, response: Response, present: Mapping[str, str]) -> None:
        for key in self._jar.ours(present):
            self._delete(response, key)

    def _set(self, response: Response, key: str, value: str) -> None:
        response.set_cookie(
            key=key,
            value=value,
            httponly=True,
            secure=self._spec.secure,
            samesite=self._spec.samesite,
            max_age=self._spec.ttl_sec,
            path=self._spec.path,
        )

    def _delete(self, response: Response, key: str) -> None:
        response.delete_cookie(
            key=key,
            path=self._spec.path,
            secure=self._spec.secure,
            samesite=self._spec.samesite,
        )


class ProxyRequests:
    """Запрос proxy-входа из запроса starlette: заголовки по именам из
    ProxyHeaderNames и адрес клиента."""

    @classmethod
    def of(cls, request: Request, names: ProxyHeaderNames) -> ProxyRequest:
        roles = ""
        if names.roles:
            roles = request.headers.get(names.roles, "")

        profiles = ""
        if names.profiles:
            profiles = request.headers.get(names.profiles, "")

        profile = ""
        if names.profile:
            profile = request.headers.get(names.profile, "")

        return ProxyRequest(
            login=request.headers.get(names.user, ""),
            timestamp=request.headers.get(names.timestamp, ""),
            signature=request.headers.get(names.signature, ""),
            roles=roles,
            profiles=profiles,
            profile=profile,
            client=SsoRequests.client_of(request),
        )


class SsoResponses:
    """Ответы SPNEGO-обмена: 401 Negotiate — браузер домена повторит запрос сам."""

    NEGOTIATE: ClassVar[str] = "Negotiate"
    PAGE: ClassVar[str] = (
        '<!doctype html><meta http-equiv="refresh" content="0;url={url}">'
    )

    @classmethod
    def headers(cls) -> dict[str, str]:
        return {RequestHeader.WWW_AUTHENTICATE.value: cls.NEGOTIATE}

    @classmethod
    def challenge(cls, login_url: str) -> Response:
        """401 со страницей-переходом: без тикета браузер уйдёт на логин с кодом."""
        url = html.escape(SsoErrorCode.TICKET.login_url(login_url), quote=True)

        return Response(
            content=cls.PAGE.format(url=url),
            status_code=401,
            headers=cls.headers(),
            media_type="text/html",
        )

    @classmethod
    def silent_challenge(cls) -> Response:
        """401 без страницы: ответ читает скрипт обновления, не человек."""
        return Response(status_code=401, headers=cls.headers())


class RequestTokens:
    """Токен входа запроса: cookie (целиком либо чанками), иначе Bearer."""

    BEARER: ClassVar[str] = "Bearer "
    COOKIE_HEADER: ClassVar[str] = "HTTP_COOKIE"
    AUTHORIZATION: ClassVar[str] = "Authorization"

    def __init__(self, cookie: str) -> None:
        self._jar = CookieJar(cookie)

    def of_cookies(self, cookies: Mapping[str, str]) -> str | None:
        return self._jar.token_of(cookies)

    def of_request(self, request: HTTPConnection) -> str | None:
        """Токен http-запроса или websocket-подключения starlette."""
        token = self.of_cookies(request.cookies)
        if token is not None:
            return token

        header = request.headers.get(self.AUTHORIZATION)
        if not header:
            return None

        if not header.startswith(self.BEARER):
            return None

        return header[len(self.BEARER) :]

    def of_environ(self, environ: Mapping[str, Any]) -> str | None:
        """Cookie из WSGI/ASGI environ подключения socket.io."""
        raw = environ.get(self.COOKIE_HEADER)
        if not isinstance(raw, str):
            return None

        if not raw:
            return None

        parsed = SimpleCookie()
        parsed.load(raw)

        cookies: dict[str, str] = {}
        for name, morsel in parsed.items():
            cookies[name] = morsel.value

        return self.of_cookies(cookies)


class StaleSessionMiddleware:
    """Отсекает cookie входа чужого поколения сессий до маршрутов приложения.

    Chainlit проверяет cookie своим декодером и поколения не знает, поэтому
    после рестарта процесса его маршруты приняли бы старую сессию. Здесь
    токен читается нашим читателем: чужое поколение — 401 и снятие cookie,
    страница уходит на вход. Прочие отказы (срок, подпись) оставляются
    маршрутам, они умеют их сами.
    """

    STALE_GRACE_SEC: ClassVar[int] = 10 * 365 * 24 * 3600
    """Срок здесь не проверяется: просроченный токен — забота маршрутов."""

    def __init__(self, app: ASGIApp, tokens: TokenReader, cookie: CookieSpec) -> None:
        self.app = app
        self._tokens = tokens
        self._cookie = SessionCookie(cookie)
        self._request_tokens = RequestTokens(cookie.name)
        self._logger = logging.getLogger(__name__)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        request = Request(scope)
        token = self._request_tokens.of_cookies(request.cookies)
        if token is None:
            return await self.app(scope, receive, send)

        try:
            self._tokens.read_stale(token, self.STALE_GRACE_SEC)
        except TokenRejectedError as exc:
            if exc.reason is not TokenRejection.GENERATION:
                return await self.app(scope, receive, send)

            self._logger.info(
                "%s %s: stale session cookie refused: %s",
                scope.get("method", "?"),
                scope.get("path", "?"),
                exc,
            )
            response = JSONResponse(
                content={
                    "detail": (
                        "your sign-in belongs to a previous session generation, "
                        "sign in again"
                    )
                },
                status_code=401,
            )
            self._cookie.clear(response, request.cookies)

            return await response(scope, receive, send)

        return await self.app(scope, receive, send)


class DomainErrorMiddleware:
    "Единая точка обработки исключений приложения"

    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self._logger = logging.getLogger(__name__)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        started = False

        async def guarded_send(message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True

            await send(message)

        try:
            await self.app(scope, receive, guarded_send)
        except Exception as e:
            # http-слой пишет в журнал ту же формулировку, что чат и история
            described = FailureReport.of(e).log
            request = f"{scope.get('method', '?')} {scope.get('path', '?')}"
            if not isinstance(e, BaseError):
                self._logger.exception("%s: %s", request, LogLine.safe(described))
            else:
                self._logger.error("%s: %s", request, LogLine.safe(described))

            domain = to_domain(e)

            if started:
                # ответ уже начат — подменить его уже нельзя
                raise

            if (http := domain.http_message()) is not None:
                # chainlit мапит detail в auth.login.errors.<code> (локализация)
                await JSONResponse(
                    content={"detail": http.content},
                    status_code=http.status_code,
                    headers=http.headers,
                )(scope, receive, send)


class SsoRequests:
    """Запрос SPNEGO-обмена из запроса starlette: заголовки и адрес клиента."""

    @classmethod
    def of(cls, request: Request) -> SsoRequest:
        mark = request.headers.get(OwnRequest.HEADER, "")

        return SsoRequest(
            authorization=request.headers.get(RequestHeader.AUTHORIZATION, ""),
            own_request=OwnRequest.asked(mark),
            client=cls.client_of(request),
        )

    @staticmethod
    def client_of(request: Request) -> str:
        """Лучший идентификатор клиента: реальный IP за прокси, иначе peer.

        X-Forwarded-For и X-Real-IP доверяются, потому что nginx перед
        приложением перезаписывает их сам.
        """
        if xff := request.headers.get(RequestHeader.FORWARDED_FOR):
            first, _, _ = xff.partition(",")
            return first.strip()

        if real := request.headers.get(RequestHeader.REAL_IP):
            return real

        if request.client is not None:
            return request.client.host

        return SsoRequest.UNKNOWN_CLIENT
