"""Порты процесса aid: чат и страницы-прокси песочниц MCP Apps.

Ошибки:
ConfigError — в app_root нет страницы-прокси или шрифта песочницы.
ServerStartError — порт чата или песочницы не удалось занять.
"""

import logging
import socket
from enum import StrEnum
from pathlib import Path

from starlette.applications import Starlette
from starlette.datastructures import MutableHeaders
from starlette.requests import Request
from starlette.responses import FileResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from boba.aid.infra.config import AppConfig
from boba.config import ConfigError
from boba.mcp_client.client import McpServerApps
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


class SandboxHeaders:
    """Заголовки ответов порта песочницы одного сервера: встроить страницу
    может только страница чата (frame-ancestors), формы приложения никуда не
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
            SandboxHeader.CACHE: "no-cache",
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
    """Сайт порта песочницы одного сервера с секцией apps: страница-прокси
    в корне и шрифт чата. Других маршрутов нет — ни входа, ни API, ни
    cookie, ни сокетов: скрипт приложения, дотянувшийся до своего порта,
    находит там только статику. Создаёт AidListeners на каждый такой
    сервер."""

    def __init__(self, apps: McpServerApps, root: Path) -> None:
        self._page = SandboxFile.PAGE.under(root)
        fonts = StaticFiles(directory=SandboxFile.FONTS.under(root))
        site = Starlette(
            routes=[
                Route(SandboxRoute.PAGE, self._page_response, methods=["GET"]),
                Mount(SandboxRoute.FONTS, app=fonts),
            ]
        )
        self.app = SandboxHeaders(site, apps.chat_origin())

    async def _page_response(self, request: Request) -> FileResponse:
        return FileResponse(self._page, media_type="text/html")


class AidListeners:
    """ASGI-вход процесса aid и его сокеты: порт чата и по порту на
    песочницу каждого сервера с секцией apps.

    Один сервер uvicorn слушает все сокеты; запрос раскладывается по
    локальному порту сокета, на который пришёл (scope["server"]), а не по
    заголовку Host: на порт песочницы отвечает её SandboxSite, на порт
    чата — приложение чата, ему же уходит lifespan. Так песочница живёт на
    своём источнике без внешнего веб-сервера. Создаёт run_app по конфигу,
    приложению чата и корню статики chainlit (root), где лежат страницы
    песочницы MCP Apps.
    """

    def __init__(self, config: AppConfig, chat: ASGIApp, root: Path) -> None:
        self._chat = chat
        self._host = config.aid.host
        self._chat_port = config.aid.port
        self._scheme = ListenScheme.PLAIN
        if config.aid.ssl_cert:
            self._scheme = ListenScheme.TLS

        self._sites: dict[int, ASGIApp] = {}
        self._servers: dict[int, str] = {}
        for name, server in config.mcp.servers.items():
            apps = server.apps
            if apps is None:
                continue

            self._check(name, root)
            self._sites[apps.port] = SandboxSite(apps, root).app
            self._servers[apps.port] = name

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        site = self._site(scope)
        if site is None:
            await self._chat(scope, receive, send)
            return

        await site(scope, receive, send)

    def sockets(self) -> list[socket.socket]:
        """Занятые сокеты: чат первым, затем песочницы.

        Ошибки:
        ServerStartError — порт занят или адрес не годится.
        """
        bound = [self._bound(self._chat_port, "the chat")]
        for port, name in self._servers.items():
            bound.append(self._bound(port, f"the app sandbox of MCP server {name!r}"))

        return bound

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

    def _check(self, name: str, root: Path) -> None:
        """Ошибки:
        ConfigError — страницы или шрифта песочницы нет в app_root.
        """
        for wanted in SandboxFile:
            path = wanted.under(root)
            if path.exists():
                continue

            msg = (
                f"[mcp.servers.{name}.apps]: the chat serves the app sandbox from "
                f"its app root [aid].root {root}, but {path} is missing; install "
                "the chat with its assets (in the sources the fonts come from "
                "make -C build web)"
            )
            raise ConfigError(msg)
