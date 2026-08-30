"""HTTP-граница: BaseError -> статус и тело ответа, запрос SSO -> модель сервиса,
токен входа из cookie или Authorization запроса.
"""

import logging
from collections.abc import Mapping
from http.cookies import SimpleCookie
from typing import Any, ClassVar

from starlette.requests import HTTPConnection, Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from boba.identity.errors import BaseError, FailureReport, to_domain
from boba.identity.session import LogLine
from boba.identity.sso import OwnRequest, RequestHeader, SsoRequest
from boba.identity.token import CookieJar

__all__ = ["DomainErrorMiddleware", "RequestTokens", "SsoRequests"]


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
            if not isinstance(e, BaseError):
                self._logger.exception("%s", LogLine.safe(described))
            else:
                self._logger.error("%s", LogLine.safe(described))

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
            client=cls._client_of(request),
        )

    @staticmethod
    def _client_of(request: Request) -> str:
        """Лучший идентификатор клиента для логов: реальный IP за прокси, иначе peer."""
        if xff := request.headers.get(RequestHeader.FORWARDED_FOR):
            first, _, _ = xff.partition(",")
            return first.strip()

        if real := request.headers.get(RequestHeader.REAL_IP):
            return real

        if request.client is not None:
            return request.client.host

        return SsoRequest.UNKNOWN_CLIENT
