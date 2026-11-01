"""Порты процесса aid: чат и страница-прокси песочницы MCP Apps.

Ошибки:
ConfigError — в app_root нет страницы-прокси или шрифта песочницы.
ServerStartError — порт чата или песочницы не удалось занять.
"""

import hmac
import logging
import secrets
import socket
from enum import StrEnum
from pathlib import Path
from typing import ClassVar
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from starlette.applications import Starlette
from starlette.datastructures import MutableHeaders
from starlette.requests import Request
from starlette.responses import FileResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from boba.aid.infra.config import AidSection, AppConfig
from boba.config import ConfigError
from boba.runtime.http import ServerStartError

logger = logging.getLogger(__name__)


class SandboxFile(StrEnum):
    """Файлы песочницы в app_root: страница-прокси и шрифт чата Inter,
    тот же, что у ленты."""

    PAGE = "mcp-apps/sandbox.html"
    FONTS = "public/vendor/inter/files"

    def under(self, root: Path) -> Path:
        return root / self.value


class SandboxRoute(StrEnum):
    """Пути порта песочницы: страница-прокси в корне, шрифт рядом — по нему
    элемент ToolApp строит @font-face приложения."""

    PAGE = "/"
    FONTS = "/fonts/inter/files"


class SandboxHeader(StrEnum):
    """Заголовки каждого ответа порта песочницы."""

    POLICY = "content-security-policy"
    CORS = "access-control-allow-origin"
    NOSNIFF = "x-content-type-options"
    REFERRER = "referrer-policy"
    CACHE = "cache-control"


class ListenScheme(StrEnum):
    """Схема сокетов aid в журнале: TLS у всех сокетов один, из [aid]."""

    PLAIN = "http"
    TLS = "https"


class SandboxToken:
    """Токен страницы-прокси песочницы: пропуск к ней из страницы чата.

    Порт песочницы открыт всем, кто до него дотянется, и защищён только
    токеном, а не источником запроса: страница чата передаёт его в адресе
    iframe, страница-прокси читает и сразу убирает из адреса, а приложения
    сервера его не видят — они живут в непрозрачном источнике внутреннего
    iframe. Создаёт провайдер процесса один раз на процесс; страницу-прокси
    пускает по нему SandboxSite, адрес с токеном строит url().
    """

    PARAM: ClassVar[str] = "token"

    def __init__(self, secret: str) -> None:
        self._secret = secret

    def admits(self, presented: str | None) -> bool:
        """Токен непуст и совпадает с выданным."""
        if not presented:
            return False

        if not presented.isascii():
            return False

        return hmac.compare_digest(presented, self._secret)

    def url(self, base: str) -> str:
        """Адрес страницы-прокси base с токеном в запросе."""
        parts = urlsplit(base)
        query = parse_qsl(parts.query, keep_blank_values=True)
        query.append((self.PARAM, self._secret))

        return urlunsplit(
            (parts.scheme, parts.netloc, parts.path, urlencode(query), "")
        )


class SandboxLinks:
    """Песочница приложений процесса: её адрес для страницы чата и токен
    пропуска к странице-прокси.

    Одна на процесс: токен выдаётся при старте и живёт, пока жив процесс.
    Создаёт run_app по секции [aid] и кладёт в контейнер до старта приложения;
    адрес с токеном (app_url) уезжает ленте в карточку приложения, токен
    (token) получает AidListeners для страницы-прокси.
    """

    BYTES: ClassVar[int] = 32

    def __init__(self, section: AidSection) -> None:
        self._token = SandboxToken(secrets.token_urlsafe(self.BYTES))
        self._base = section.sandbox_url()

    def app_url(self) -> str:
        """Адрес страницы-прокси с токеном: его страница чата кладёт в iframe
        приложения."""
        return self._token.url(self._base)

    @property
    def token(self) -> SandboxToken:
        return self._token


class SandboxHeaders:
    """Заголовки ответов порта песочницы: встроить страницу может только
    страница чата (frame-ancestors), формы приложения никуда не
    отправляются (form-action; политику страницы наследует iframe приложения
    из srcdoc), шрифт открыт непрозрачному источнику приложения (CORS *:
    на порту лежит только открытая статика без данных пользователя).
    Оборачивает сайт песочницы (SandboxSite)."""

    def __init__(self, app: ASGIApp, chat_origin: str) -> None:
        self._app = app
        self._headers = {
            SandboxHeader.POLICY: (
                f"frame-ancestors {chat_origin}; form-action 'none'; object-src 'none'"
            ),
            SandboxHeader.CORS: "*",
            SandboxHeader.NOSNIFF: "nosniff",
            SandboxHeader.REFERRER: "no-referrer",
            SandboxHeader.CACHE: "no-store",
        }

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        async def stamped(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in self._headers.items():
                    headers[name] = value

            await send(message)

        await self._app(scope, receive, stamped)


class SandboxSite:
    """Сайт порта песочницы процесса: страница-прокси в корне и шрифт чата.

    Других маршрутов нет — ни входа, ни API, ни cookie, ни сокетов: скрипт
    приложения, дотянувшийся до своего порта, находит там только статику.
    Страница отдаётся лишь по токену чата (SandboxToken), остальным ответ
    404: без токена порт не выдаёт даже страницу-прокси. Создаёт AidListeners
    один на процесс.
    """

    def __init__(self, root: Path, chat_origin: str, token: SandboxToken) -> None:
        self._page = SandboxFile.PAGE.under(root)
        self._token = token
        fonts = StaticFiles(directory=SandboxFile.FONTS.under(root))
        site = Starlette(
            routes=[
                Route(SandboxRoute.PAGE, self._page_response, methods=["GET"]),
                Mount(SandboxRoute.FONTS, app=fonts),
            ]
        )
        self.app = SandboxHeaders(site, chat_origin)

    async def _page_response(self, request: Request) -> Response:
        presented = request.query_params.get(SandboxToken.PARAM)
        request.scope["query_string"] = b""
        if not self._token.admits(presented):
            return Response(status_code=404)

        return FileResponse(self._page, media_type="text/html")


class AidListeners:
    """ASGI-вход процесса aid и его сокеты: порт чата и порт песочницы.

    Один сервер uvicorn слушает оба сокета; запрос раскладывается по
    локальному порту сокета, на который пришёл (scope["server"]), а не по
    заголовку Host: на порт песочницы отвечает её SandboxSite, на порт чата —
    приложение чата, ему же уходит lifespan. Так песочница живёт на своём
    источнике без внешнего веб-сервера. Создаёт run_app по конфигу,
    приложению чата, корню статики chainlit (root) и токену песочницы.
    """

    def __init__(
        self,
        config: AppConfig,
        chat: ASGIApp,
        root: Path,
        token: SandboxToken,
    ) -> None:
        self._chat = chat
        self._host = config.aid.host
        self._chat_port = config.aid.port
        self._sandbox_port = config.aid.sandbox_port
        self._scheme = ListenScheme.PLAIN
        if config.aid.ssl_cert:
            self._scheme = ListenScheme.TLS

        self._check(root)
        site = SandboxSite(root, config.aid.chat_origin(), token)
        self._sites: dict[int, ASGIApp] = {self._sandbox_port: site.app}

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        site = self._site(scope)
        if site is None:
            await self._chat(scope, receive, send)
            return

        await site(scope, receive, send)

    def sockets(self) -> list[socket.socket]:
        """Занятые сокеты: чат первым, затем песочница.

        Ошибки:
        ServerStartError — порт занят или адрес не годится.
        """
        chat = self._bound(self._chat_port, "the chat")
        try:
            sandbox = self._bound(self._sandbox_port, "the app sandbox")
        except ServerStartError:
            chat.close()
            raise

        return [chat, sandbox]

    def _site(self, scope: Scope) -> ASGIApp | None:
        if scope["type"] == "lifespan":
            return None

        server = scope.get("server")
        if server is None:
            return None

        return self._sites.get(server[1])

    def _bound(self, port: int, what: str) -> socket.socket:
        family = socket.AF_INET
        if ":" in self._host:
            family = socket.AF_INET6

        # слушать сокет начинает uvicorn, когда поднимется приложение чата
        sock = socket.socket(family)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((self._host, port))
        except OSError as exc:
            sock.close()
            msg = (
                f"aid: binding {self._host}:{port} for {what} failed: {exc}; "
                "free the port or give it another one in the config"
            )
            raise ServerStartError(msg) from exc

        logger.info("aid serves %s on %s://%s:%d", what, self._scheme, self._host, port)

        return sock

    def _check(self, root: Path) -> None:
        """Ошибки:
        ConfigError — страницы или шрифта песочницы нет в app_root.
        """
        for wanted in SandboxFile:
            path = wanted.under(root)
            if path.exists():
                continue

            msg = (
                f"[aid]: the chat serves the app sandbox from share/ of the "
                f"program {root}, but {path} is missing; install the chat with "
                "its assets (in the sources make -C build build APP=runtime puts "
                "them into the assets of boba-aid)"
            )
            raise ConfigError(msg)
