"""HTTP-граница входа приложений: BaseError -> статус и тело,
запрос SSO -> модель сервиса, токен входа из cookie или Authorization, cookie
сессии в ответе и ответы SPNEGO-обмена (401 Negotiate со страницей-переходом).
SignalledServer — сервер uvicorn, остановку которого по сигналу ведёт
ProcessStop.

HealthProbe — проба готовности службы подкомандой `health`.

Ошибки:
ServerStartError — приложение сервера не поднялось, процессу пора выйти
    с ненулевым кодом.
HealthCheckError — служба не ответила на пробу готовности успехом.
"""

import logging
import socket
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping, Sequence
from types import FrameType
from typing import ClassVar

import uvicorn
from starlette.requests import HTTPConnection, Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send
from uvicorn.lifespan.on import LifespanOn

from boba.auth.http import SsoRequests, SsoResponses
from boba.cancellation import ProcessStop
from boba.identity.errors import BaseError
from boba.identity.session import LogLine
from boba.identity.signin import ProxyHeaderNames, ProxyRequest
from boba.identity.token import (
    CookieJar,
    CookieSpec,
    TokenReader,
    TokenRejectedError,
    TokenRejection,
)
from boba.runtime.errors import FailureReports
from boba.tool_io.closing import ProcessClosers

__all__ = [
    "DomainErrorMiddleware",
    "HealthCheckError",
    "HealthProbe",
    "ProxyRequests",
    "RequestTokens",
    "ServerStartError",
    "SessionCookie",
    "SignalledServer",
    "SsoRequests",
    "SsoResponses",
    "StaleSessionMiddleware",
]


class HealthCheckError(Exception):
    """Служба не ответила на пробу готовности успехом."""


class HealthProbe:
    """Проба готовности службы: GET на свой порт и префикс по loopback.

    Её зовёт подкоманда `health` программы (HEALTHCHECK образа, проверка
    юнита) над портом и префиксом из своей секции конфига; путь пробы —
    маршрут готовности самой службы.
    """

    LOOPBACK: ClassVar[str] = "127.0.0.1"
    SCHEME: ClassVar[str] = "http"
    TIMEOUT_SEC: ClassVar[float] = 4.0

    def __init__(self, port: int, url_prefix: str, path: str) -> None:
        self._url = urllib.parse.urlunsplit(
            (self.SCHEME, f"{self.LOOPBACK}:{port}", f"{url_prefix}{path}", "", "")
        )

    def check(self) -> str:
        """Строка итога пробы; не 2xx или нет ответа — HealthCheckError."""
        try:
            # схема http и адрес 127.0.0.1 собраны здесь: file:/ сюда не попадает
            with urllib.request.urlopen(self._url, timeout=self.TIMEOUT_SEC) as answer:  # noqa: S310  # nosec B310
                status = answer.status
        except (urllib.error.URLError, OSError) as exc:
            msg = f"health probe GET {self._url}: expected a 2xx answer, got {exc}"
            raise HealthCheckError(msg) from exc

        return f"health probe GET {self._url}: {status}"


class SessionCookie:
    """Cookie входа в ответе HTTP: атрибуты из CookieSpec, чанки — CookieJar.

    Cookie ставят маршруты входа чата aid, а читает её код cookie chainlit.
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
    ProxyHeaderNames и адрес клиента. Создаётся маршрутом входа proxy
    приложения и лежит у него полем."""

    def __init__(self) -> None:
        self._requests = SsoRequests()

    def of(self, request: Request, names: ProxyHeaderNames) -> ProxyRequest:
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
            client=self._requests.client_of(request),
        )


class RequestTokens:
    """Токен входа запроса: cookie (целиком либо чанками), иначе Bearer."""

    BEARER: ClassVar[str] = "Bearer "
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

        if not header.casefold().startswith(self.BEARER.casefold()):
            return None

        return header[len(self.BEARER) :].strip()


class StaleSessionMiddleware:
    """Проверяет поколение и идентификатор сессии перед декодером Chainlit.

    HTTP и WebSocket с устаревшим токеном идут к приложению как гости.
    HTTP-ответ также снимает cookie входа в браузере. Срок и подпись
    проверяют штатные маршруты Chainlit.
    """

    STALE_GRACE_SEC: ClassVar[int] = 10 * 365 * 24 * 3600
    """Срок здесь не проверяется: просроченный токен — забота маршрутов."""

    COOKIE_HEADER: ClassVar[bytes] = b"cookie"
    SET_COOKIE_HEADER: ClassVar[bytes] = b"set-cookie"
    HEADER_ENCODING: ClassVar[str] = "latin-1"
    PAIR_SEPARATOR: ClassVar[str] = ";"

    def __init__(self, app: ASGIApp, tokens: TokenReader, cookie: CookieSpec) -> None:
        self.app = app
        self._tokens = tokens
        self._cookie = SessionCookie(cookie)
        self._request_tokens = RequestTokens(cookie.name)
        self._authorization = RequestTokens.AUTHORIZATION.lower().encode("ascii")
        self._bearer = RequestTokens.BEARER.lower().encode("ascii")
        self._logger = logging.getLogger(__name__)
        self._failures = FailureReports()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in {"http", "websocket"}:
            return await self.app(scope, receive, send)

        request = HTTPConnection(scope)
        token = self._request_tokens.of_request(request)
        if token is None:
            return await self.app(scope, receive, send)

        try:
            self._tokens.read_stale(token, self.STALE_GRACE_SEC)
        except TokenRejectedError as exc:
            if exc.reason not in {
                TokenRejection.GENERATION,
                TokenRejection.IDENTITY,
                TokenRejection.MALFORMED,
            }:
                return await self.app(scope, receive, send)

            self._logger.info(
                "%s %s: stale session token dropped, the request goes on as a "
                "guest: %s",
                scope.get("method", "?"),
                scope.get("path", "?"),
                exc,
            )
            guest = self._without_ours(scope, request.cookies)
            if scope["type"] == "websocket":
                return await self.app(guest, receive, send)

            clearing = self._clearing(request.cookies)

            async def send_clearing(message: Message) -> None:
                if message["type"] == "http.response.start":
                    headers = list(message.get("headers", []))
                    headers.extend(clearing)
                    message = {**message, "headers": headers}

                await send(message)

            return await self.app(guest, receive, send_clearing)

        return await self.app(scope, receive, send)

    def _without_ours(self, scope: Scope, present: Mapping[str, str]) -> Scope:
        """Scope без токенов входа; прочие cookie и заголовки сохраняются."""
        ours = self._cookie.jar.ours(present)

        headers: list[tuple[bytes, bytes]] = []
        for name, value in scope["headers"]:
            if name.lower() == self._authorization:
                if value.lower().startswith(self._bearer):
                    continue

            if name != self.COOKIE_HEADER:
                headers.append((name, value))
                continue

            kept = self._kept_pairs(value.decode(self.HEADER_ENCODING), ours)
            if kept:
                headers.append((name, kept.encode(self.HEADER_ENCODING)))

        return {**scope, "headers": headers}

    def _kept_pairs(self, raw: str, ours: set[str]) -> str:
        kept: list[str] = []
        for pair in raw.split(self.PAIR_SEPARATOR):
            name, _, _ = pair.partition("=")
            if name.strip() in ours:
                continue

            kept.append(pair.strip())

        return f"{self.PAIR_SEPARATOR} ".join(kept)

    def _clearing(self, present: Mapping[str, str]) -> list[tuple[bytes, bytes]]:
        """Заголовки Set-Cookie, снимающие cookie входа и её чанки."""
        cleared = Response()
        self._cookie.clear(cleared, present)

        headers: list[tuple[bytes, bytes]] = []
        for name, value in cleared.raw_headers:
            if name != self.SET_COOKIE_HEADER:
                continue

            headers.append((name, value))

        return headers


class DomainErrorMiddleware:
    "Единая точка обработки исключений приложения"

    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self._logger = logging.getLogger(__name__)
        self._failures = FailureReports()

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
            described = self._failures.describe(e).log
            request = f"{scope.get('method', '?')} {scope.get('path', '?')}"
            if not isinstance(e, BaseError):
                self._logger.exception("%s: %s", request, LogLine.safe(described))
            else:
                self._logger.error("%s: %s", request, LogLine.safe(described))

            domain = self._failures.domain(e)

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


class ServerStartError(Exception):
    """Приложение сервера не поднялось: его lifespan сорвался на старте."""


class SignalledServer(uvicorn.Server):
    """Сервер uvicorn, остановку которого по сигналу ведёт ProcessStop.

    Создаётся входом процесса приложения (чат, сервис MCP) из
    настроек uvicorn и остановки процесса. На время работы uvicorn ставит
    свои обработчики сигналов: пойманный сигнал сервер сразу сообщает
    остановке процесса — та выполняет действия владельца, не дожидаясь
    закрытия соединений. Закончив остановку, uvicorn посылает сигнал заново
    прежнему обработчику; им стоит обработчик остановки процесса, поэтому
    процесс не погибает с кодом 143, а доходит до кода после сервера.

    Повторный сигнал uvicorn понимает как «не ждать соединения» (force_exit)
    и вместе с ожиданием пропускает остановку приложения (lifespan
    shutdown); сервер проводит её сам, иначе задачу lifespan отменил бы уже
    asyncio.run на выходе, и ресурсы приложения закрывались бы отменой.

    Сорванный старт приложения uvicorn только пишет в лог и возвращается из
    serve() штатно; сервер превращает его в ServerStartError, чтобы процесс
    вышел с ненулевым кодом. Перед выходом закрываются процессные ресурсы
    (ProcessClosers): пул, оставшийся открытым после сорванного старта, не
    даёт asyncio.run закрыть цикл событий, и процесс не завершается.
    """

    def __init__(self, config: uvicorn.Config, stop: ProcessStop) -> None:
        super().__init__(config)
        self._stop = stop
        self._closers = ProcessClosers()

    async def serve_until_stopped(self, sockets: Sequence[socket.socket] = ()) -> None:
        """Слушает адрес настроек uvicorn либо готовые сокеты sockets до
        сигнала остановки и возвращается штатно.

        Ошибки:
        ServerStartError — приложение не поднялось.
        """
        self._stop.install()

        listeners: list[socket.socket] | None = None
        if sockets:
            listeners = list(sockets)

        try:
            await self.serve(listeners)
        finally:
            await self._closers.close_all()

        if not self.started:
            msg = (
                f"server on {self.config.host}:{self.config.port}: the application "
                "startup failed, the cause is logged above by the lifespan; "
                "expected a started server, exiting with a failure"
            )
            raise ServerStartError(msg)

    async def shutdown(self, sockets: list[socket.socket] | None = None) -> None:
        await super().shutdown(sockets)

        if not isinstance(self.lifespan, LifespanOn):
            return

        if self.lifespan.shutdown_event.is_set():
            return

        await self.lifespan.shutdown()

    def handle_exit(self, sig: int, frame: FrameType | None) -> None:
        self._stop.deliver(sig)

        super().handle_exit(sig, frame)
