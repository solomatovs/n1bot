"""Помощники ui-тестов чата: логин формой chainlit, вкладки чата, журнал сокетов,
консоль и сокеты вкладки."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import ClassVar

import httpx
from playwright._impl._api_structures import SetCookieParam
from playwright.sync_api import (
    Browser,
    BrowserContext,
    ConsoleMessage,
    Error,
    Page,
    Request,
    ViewportSize,
    WebSocket,
)

from boba.stand.ui.chat_page import ChatPage
from boba.stand.ui.fake_llm import FakeRoute
from boba.stand.ui.socket_log import SocketLog
from boba.stand.ui.stand import StandProcess, StandUrl

BOOT_TIMEOUT_SEC = 300.0
OpenChat = Callable[[StandProcess, str], ChatPage]


def login_cookies(stand: StandProcess, login: str = "") -> list[SetCookieParam]:
    """Логин формой chainlit: тест ходит той же дорогой, что и пользователь."""
    credential = stand.config.credential(login)
    response = httpx.post(
        f"{stand.config.base_url}/login",
        data={"username": credential.login, "password": credential.password},
        timeout=30.0,
    )
    if response.status_code != 200:
        raise RuntimeError(
            f"login failed: {response.status_code} {response.text[:200]}"
        )

    cookies: list[SetCookieParam] = []
    for name, value in response.cookies.items():
        cookies.append(
            {"name": name, "value": value, "domain": "127.0.0.1", "path": "/"}
        )

    if not cookies:
        msg = f"POST {stand.config.base_url}/login for {login!r} returned no cookies"
        raise RuntimeError(msg)

    return cookies


def watch_sockets(page: Page, log: SocketLog) -> None:
    def on_socket(socket: WebSocket) -> None:
        socket.on("framereceived", log.accept)

    page.on("websocket", on_socket)


class ConsoleLevel(StrEnum):
    """Уровни сообщений консоли, которые пользователь замечает."""

    ERROR = "error"
    WARNING = "warning"


class PageWatch:
    """Консоль и сокеты вкладки чата глазами пользователя.

    Копит то, что пользователь увидел бы в консоли браузера: ошибки страницы
    и сообщения уровней error и warning, включая сетевые сообщения самого
    браузера (неудавшийся WebSocket). Сокеты считаются по открытым
    WebSocket-соединениям: лишнее переподключение видно по их числу.
    """

    def __init__(self, page: Page) -> None:
        self.complaints: list[str] = []
        self.sockets: list[str] = []
        self._levels = list(ConsoleLevel)
        page.on("console", self._logged)
        page.on("pageerror", self._failed)
        page.on("websocket", self._opened)

    def _logged(self, message: ConsoleMessage) -> None:
        if message.type not in self._levels:
            return

        where = message.location.get("url", "")
        self.complaints.append(f"{message.type} {where}: {message.text}")

    def _failed(self, error: Error) -> None:
        self.complaints.append(f"page error: {error.message}\n{error.stack}")

    def _opened(self, socket: WebSocket) -> None:
        self.sockets.append(socket.url)


@dataclass
class ChatOpener:
    """Открывает вкладки чата и закрывает их разом: свой стенд и логин на каждую."""

    browser: Browser
    llm_port: int
    contexts: list[BrowserContext] = field(default_factory=list)

    VIEWPORT: ClassVar[ViewportSize] = {"width": 1280, "height": 900}

    def open(self, stand: StandProcess, login: str = "") -> ChatPage:
        httpx.post(StandUrl.of(self.llm_port, FakeRoute.RESET.value), timeout=5.0)
        context = self.browser.new_context(viewport=self.VIEWPORT)
        self.contexts.append(context)
        context.add_cookies(login_cookies(stand, login))
        page: Page = context.new_page()
        log = SocketLog()
        watch_sockets(page, log)
        chat_page = ChatPage(page=page, log=log, base_url=stand.config.base_url)
        chat_page.open()
        return chat_page

    def close(self) -> None:
        for context in self.contexts:
            context.close()

        self.contexts.clear()


class AppReads:
    """Запросы вкладки к серверной части чата, которые просят ресурс
    приложения (действие mcp_app_read) — по сокету и по HTTP; ведётся с
    открытия треда."""

    ACTION: ClassVar[str] = "mcp_app_read"

    def __init__(self, page: Page) -> None:
        self.seen: list[str] = []
        page.on("websocket", self._socket)
        page.on("request", self._request)

    def _socket(self, socket: WebSocket) -> None:
        socket.on("framesent", self._frame)

    def _frame(self, payload: str | bytes) -> None:
        text = str(payload)
        if self.ACTION in text:
            self.seen.append(text[:200])

    def _request(self, request: Request) -> None:
        body = request.post_data
        if body is None:
            return

        if self.ACTION in body:
            self.seen.append(f"{request.method} {request.url}: {body[:200]}")
