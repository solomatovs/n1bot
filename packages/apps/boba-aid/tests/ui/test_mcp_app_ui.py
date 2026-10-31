"""Приложение MCP-серверов в живом чате: команда `/mcp` и её сообщение
ленты глазами пользователя.

Стенд чата поднимается с двумя серверами профиля: обычным (standard_server.py
по HTTP) и вторым таким же за закрытым портом — его состояние `failed`.
Проверяется то, что видит пользователь: «/» показывает команду `mcp`, после
её выбора в ленте появляется сообщение с приложением — строки серверов и их
состояния; карточка сервера показывает инструменты; Reconnect переподключает
сервер и карточка снова на связи; после перезагрузки треда сообщение на
месте и приложение читает состояние серверов заново.

Ошибки: своих не выпускает; расхождение — падение теста.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import ClassVar

import pytest
from acp_stand import AppsHost, ClosedEndpoint
from chat_ui import BOOT_TIMEOUT_SEC, OpenChat
from playwright.sync_api import Locator, Page, expect

from boba.aid.chat.ui_text import UiText
from boba.mcp_client.client import McpServerConfig
from boba.stand.ui.chat_page import ChatPage, StepKind
from boba.stand.ui.socket_log import StepField
from boba.stand.ui.stand import (
    StandApp,
    StandConfig,
    StandPorts,
    StandProcess,
)

pytestmark = [pytest.mark.ui, pytest.mark.xdist_group("mcp-app-ui")]

LIVE = "live"
GONE = "gone"
"""Серверы профиля стенда: живой и закрытый порт."""
WAIT_MS = 60_000


@pytest.fixture(scope="module")
def live_host() -> Iterator[AppsHost]:
    """Сервер MCP стенда: стандартный сервер по HTTP без расширения команд."""
    host = AppsHost([])
    try:
        host.await_listening()
        yield host
    finally:
        host.stop()


@pytest.fixture(scope="module")
def live_address(live_host: AppsHost) -> str:
    """Адрес живого сервера так, как его называет карточка приложения."""
    config = McpServerConfig.model_validate(live_host.plain())

    return config.endpoint.label()


@pytest.fixture(scope="module")
def mcp_stand(
    stand_workdir: Path,
    llm_port: int,
    fake_llm: None,
    stand_database: str,
    live_host: AppsHost,
) -> Iterator[StandProcess]:
    config = StandConfig(
        workdir=stand_workdir / "chat-mcp-app",
        app=StandApp.AID,
        app_port=StandPorts().free(),
        llm_port=llm_port,
        db_name=stand_database,
        url_prefix="/boba-mcp-app",
        mcp_servers={
            LIVE: live_host.plain(),
            GONE: ClosedEndpoint().section(),
        },
    )
    process = StandProcess(config=config, log_path=stand_workdir / "mcp-app.log")
    try:
        process.start(boot_timeout_sec=BOOT_TIMEOUT_SEC)
        yield process
    finally:
        process.stop()


class McpAppPage:
    """Команда `/mcp` и её приложение в ленте глазами теста. Подписи берутся
    из переводов стенда: язык интерфейса задаёт конфиг приложения."""

    ITEM: ClassVar[str] = ".command-item"
    APP: ClassVar[str] = "div[data-mcp-servers]"
    SERVER: ClassVar[str] = "[data-mcp-server='{name}']"
    CARD: ClassVar[str] = "[data-mcp-card='{name}']"
    TOOLS: ClassVar[str] = "[data-mcp-tools]"
    COMMAND: ClassVar[str] = "mcp"
    TEXT: ClassVar[str] = "aid.mcp"
    DESCRIPTION: ClassVar[str] = "Manage MCP servers"

    def __init__(self, chat: ChatPage, texts: UiText) -> None:
        self.chat = chat
        self._texts = texts

    @property
    def page(self) -> Page:
        return self.chat.page

    def label(self, key: str) -> str:
        """Подпись приложения aid.mcp.<key> на языке интерфейса стенда."""
        return self._texts.text(f"{self.TEXT}.{key}")

    def item(self) -> Locator:
        """Пункт меню ровно команды `/mcp`, а не сервера с mcp в имени."""
        named = self.page.get_by_text(self.COMMAND, exact=True)
        item = self.page.locator(self.ITEM).filter(has=named).first
        item.wait_for(timeout=WAIT_MS)

        return item

    def run(self) -> None:
        """Команда mcp выбрана в меню «/» и отправлена."""
        field = self.page.locator("#chat-input")
        field.click()
        field.fill("")
        field.press_sequentially(f"/{self.COMMAND}")
        self.item().click()
        self.chat.log.clear()
        field.press("Enter")

    def app(self) -> Locator:
        app = self.page.locator(self.APP).last
        app.wait_for(timeout=WAIT_MS)

        return app

    def server(self, name: str) -> Locator:
        server = self.page.locator(self.SERVER.format(name=name)).last
        server.wait_for(timeout=WAIT_MS)

        return server

    def card(self, name: str) -> Locator:
        card = self.page.locator(self.CARD.format(name=name)).last
        card.wait_for(timeout=WAIT_MS)

        return card


class TestMcpApp:
    def test_slash_shows_the_mcp_command(
        self, mcp_stand: StandProcess, open_chat: OpenChat, app_texts: UiText
    ) -> None:
        """«/» в поле ввода — среди команд есть mcp с описанием."""
        view = McpAppPage(open_chat(mcp_stand, ""), app_texts)
        field = view.page.locator("#chat-input")
        field.click()
        field.press_sequentially("/mcp")

        expect(view.item()).to_contain_text(view.DESCRIPTION)

    def test_command_opens_the_app_with_server_states(
        self, mcp_stand: StandProcess, open_chat: OpenChat, app_texts: UiText
    ) -> None:
        """Выбран /mcp — в ленте сообщение с приложением: у живого сервера
        состояние Connected, у закрытого — Failed."""
        view = McpAppPage(open_chat(mcp_stand, ""), app_texts)

        view.run()

        app = view.app()
        expect(app).to_contain_text(view.label("title"))
        expect(view.server(LIVE)).to_contain_text(view.label("state.connected"))
        expect(view.server(GONE)).to_contain_text(view.label("state.failed"))

        questions = view.chat.steps_of(StepKind.USER.value)
        expect(questions).to_have_count(1)
        expect(questions.first).to_contain_text("/mcp")

    def test_card_shows_the_tools_and_reconnects(
        self,
        mcp_stand: StandProcess,
        open_chat: OpenChat,
        app_texts: UiText,
        live_address: str,
    ) -> None:
        """Карточка сервера показывает транспорт, адрес и инструменты по
        кнопке; Reconnect переподключает сервер и карточка снова на связи."""
        view = McpAppPage(open_chat(mcp_stand, ""), app_texts)
        view.run()

        view.server(LIVE).click()
        card = view.card(LIVE)
        expect(card).to_contain_text(live_address)
        expect(card).to_contain_text("streamable-http")

        card.get_by_text(view.label("showTools")).click()
        tools = card.locator(view.TOOLS)
        tools.wait_for(timeout=WAIT_MS)
        expect(tools).to_contain_text("add")

        card.get_by_text(view.label("reconnect")).click()

        expect(card.get_by_text(view.label("reconnect"))).to_be_enabled(
            timeout=WAIT_MS
        )
        expect(view.card(LIVE)).to_contain_text(view.label("state.connected"))

    def test_app_survives_reload_and_reads_the_state_again(
        self, mcp_stand: StandProcess, open_chat: OpenChat, app_texts: UiText
    ) -> None:
        """После перезагрузки треда сообщение приложения на месте из записи
        истории, и приложение снова читает состояние серверов."""
        first = McpAppPage(open_chat(mcp_stand, ""), app_texts)
        first.run()
        first.app()
        thread_id = first.chat.log.thread_id()
        shown = first.chat.log.last_step(StepKind.ASSISTANT.value)
        assert shown is not None, first.chat.log.describe()

        again = McpAppPage(open_chat(mcp_stand, ""), app_texts)
        again.chat.open_thread(thread_id, str(shown[StepField.ID.value]))

        app = again.app()
        expect(app).to_contain_text(again.label("title"))
        expect(again.server(LIVE)).to_contain_text(again.label("state.connected"))
