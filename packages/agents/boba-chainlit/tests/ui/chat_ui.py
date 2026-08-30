"""Помощники ui-тестов чата: логин формой chainlit, вкладки чата, журнал сокетов."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import ClassVar

import httpx
from playwright._impl._api_structures import SetCookieParam
from playwright.sync_api import Browser, BrowserContext, Page, ViewportSize, WebSocket

from boba.stand.ui.chat_page import ChatPage
from boba.stand.ui.fake_llm import FakeRoute
from boba.stand.ui.socket_log import SocketLog
from boba.stand.ui.stand import StandProcess, StandUrl

BOOT_TIMEOUT_SEC = 120.0
LlmMetaReader = Callable[[str], dict]
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
        raise RuntimeError(f"login failed: {response.status_code} {response.text[:200]}")

    cookies: list[SetCookieParam] = []
    for name, value in response.cookies.items():
        cookies.append({"name": name, "value": value, "domain": "127.0.0.1", "path": "/"})

    if not cookies:
        raise RuntimeError("login returned no cookies")

    return cookies


def watch_sockets(page: Page, log: SocketLog) -> None:
    def on_socket(socket: WebSocket) -> None:
        socket.on("framereceived", log.accept)

    page.on("websocket", on_socket)


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
