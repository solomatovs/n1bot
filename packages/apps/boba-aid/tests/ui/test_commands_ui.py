"""Команды «/» в живом чате: меню поля ввода и окна команд глазами
пользователя.

Стенд чата поднимается с обычным сервером MCP (standard_server.py) по HTTP,
который объявил расширение com.boba/commands, — с секцией apps и песочницей
на своём источнике (AppsHost). Проверяется то, что видит пользователь:
набранная «/» только что открытого чата открывает меню с командами чата,
агента и сервера (подсказка аргументов — в описании); выбранная команда
сервера — строка вызова и карточка приложения без хода модели; после
перезагрузки треда та же карточка рисуется из записи истории, вкладка не
читает ресурс приложения у сервера; команда чата — строка вызова и таблица
соединений, и после перезагрузки тоже; команда агента `/compact` — строка
вызова и ход с шагом сжатия.

Ошибки: своих не выпускает; расхождение — падение теста.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path
from typing import ClassVar

import httpx
import pytest
from acp_stand import AppsHost
from chat_ui import BOOT_TIMEOUT_SEC, AppReads, OpenChat
from playwright.sync_api import FrameLocator, Locator, Page, expect

from boba.stand.ui.chat_page import ChatPage, Selector, StepKind
from boba.stand.ui.fake_llm import (
    FakeRoute,
    ReplyBookClient,
    ScenarioName,
    SummaryBook,
)
from boba.stand.ui.socket_log import StepField
from boba.stand.ui.stand import (
    StandApp,
    StandConfig,
    StandPorts,
    StandProcess,
    StandUrl,
)

pytestmark = [pytest.mark.ui, pytest.mark.xdist_group("commands-ui")]

SERVER = "cmds"
CONSOLE = f"mcp__{SERVER}__orders_console"


@pytest.fixture(scope="module")
def commands_host(stand_workdir: Path) -> Iterator[AppsHost]:
    host = AppsHost(stand_workdir / "commands-host", ["--commands"])
    try:
        host.await_listening()
        yield host
    finally:
        host.stop()


@pytest.fixture(scope="module")
def commands_stand(
    stand_workdir: Path,
    llm_port: int,
    fake_llm: None,
    stand_database: str,
    commands_host: AppsHost,
) -> Iterator[StandProcess]:
    config = StandConfig(
        workdir=stand_workdir / "chat-commands",
        app=StandApp.AID,
        app_port=StandPorts().free(),
        llm_port=llm_port,
        db_name=stand_database,
        url_prefix="/boba-commands",
        mcp_servers={SERVER: commands_host.drawn()},
    )
    process = StandProcess(config=config, log_path=stand_workdir / "commands-app.log")
    try:
        process.start(boot_timeout_sec=BOOT_TIMEOUT_SEC)
        yield process
    finally:
        process.stop()


class CommandMenu:
    """Меню «/» поля ввода chainlit глазами теста: набор «/» с началом
    имени, пункты меню, выбор команды и отправка её аргументов."""

    ITEM: ClassVar[str] = ".command-item"
    CARD: ClassVar[str] = "[data-mcp-app]"
    WAIT_MS: ClassVar[float] = 60_000

    def __init__(self, chat: ChatPage) -> None:
        self.chat = chat

    @property
    def page(self) -> Page:
        return self.chat.page

    def typed(self, start: str) -> Locator:
        """Меню после набора `/<start>`: его пункты."""
        field = self.page.locator("#chat-input")
        field.click()
        field.fill("")
        field.press_sequentially(f"/{start}")

        return self.page.locator(self.ITEM)

    def item(self, name: str) -> Locator:
        return self.page.locator(self.ITEM, has_text=name).first

    def run(self, name: str, arguments: str) -> None:
        """Команда name выбрана в меню, её аргументы набраны и отправлены;
        журнал вкладки очищен, как перед вопросом: в нём только этот ход."""
        self.typed(name)
        self.item(name).click()
        field = self.page.locator("#chat-input")
        field.fill(arguments)
        self.chat.log.clear()
        field.press("Enter")

    def card(self) -> Locator:
        card = self.page.locator(self.CARD).last
        card.wait_for(timeout=self.WAIT_MS)

        return card

    def app(self, card: Locator) -> FrameLocator:
        return card.frame_locator("iframe").first.frame_locator("iframe")


def model_requests(llm_port: int) -> int:
    """Сколько запросов получила фейковая модель."""
    reply = httpx.get(StandUrl.of(llm_port, FakeRoute.REQUESTS.value), timeout=5.0)

    return len(reply.json()["requests"])


class TestCommandMenu:
    CONSOLE_OPEN: ClassVar[str] = "the orders console is open for new orders"
    COMPACTING: ClassVar[re.Pattern[str]] = re.compile(
        "Сжимаю контекст|Compacting the conversation"
    )
    COMPACTED: ClassVar[re.Pattern[str]] = re.compile("Контекст сжат|Context compacted")

    def test_slash_opens_the_commands_of_the_chat_the_agent_and_the_server(
        self, commands_stand: StandProcess, open_chat: OpenChat
    ) -> None:
        """«/» в поле ввода только что открытого чата — меню: команда чата,
        команды агента и промпты сервера с подсказкой аргументов в
        описании."""
        menu = CommandMenu(open_chat(commands_stand, ""))

        items = menu.typed("")
        menu.item("connections").wait_for(timeout=CommandMenu.WAIT_MS)
        console = menu.typed("orders_console")
        menu.item(CONSOLE).wait_for(timeout=CommandMenu.WAIT_MS)
        described = console.first.inner_text()

        menu.typed("compact")
        menu.item("compact").wait_for(timeout=CommandMenu.WAIT_MS)
        compact = menu.item("compact").inner_text()

        assert items.count() > 0
        assert "Opens the orders console" in described, described
        assert "— status" in described, described
        assert "optional instructions for the summary" in compact, compact

    def test_app_command_opens_the_app_in_the_tab_without_the_model(
        self, commands_stand: StandProcess, open_chat: OpenChat, llm_port: int
    ) -> None:
        """Выбранная команда сервера с расширением — строка вызова и карточка
        приложения сразу с итогом вызова; модель вызова не видела. Тред,
        открытый заново другой вкладкой, рисует ту же карточку с тем же
        итогом из записи истории — без запроса ресурса приложения."""
        menu = CommandMenu(open_chat(commands_stand, ""))
        menu.typed("orders_console")
        menu.item(CONSOLE).wait_for(timeout=CommandMenu.WAIT_MS)
        asked = model_requests(llm_port)

        menu.run(CONSOLE, "new")
        card = menu.card()
        result = menu.app(card).locator("#result")
        result.get_by_text(self.CONSOLE_OPEN).wait_for(timeout=CommandMenu.WAIT_MS)

        questions = menu.chat.steps_of(StepKind.USER.value)
        expect(questions).to_have_count(1)
        expect(questions.first).to_contain_text(f"/{CONSOLE} new")
        assert model_requests(llm_port) == asked

        thread_id = menu.chat.log.thread_id()
        shown = menu.chat.log.last_step(StepKind.ASSISTANT.value)
        assert shown is not None, menu.chat.log.describe()
        again = open_chat(commands_stand, "")
        reads = AppReads(again.page)
        again.open_thread(thread_id, str(shown[StepField.ID.value]))
        reopened = CommandMenu(again)
        replayed = reopened.app(reopened.card()).locator("#result")
        replayed.get_by_text(self.CONSOLE_OPEN).wait_for(timeout=CommandMenu.WAIT_MS)

        lines = again.steps_of(StepKind.USER.value)
        expect(lines).to_have_count(1)
        expect(lines.first).to_contain_text(f"/{CONSOLE} new")
        assert reads.seen == [], reads.seen

    def test_chat_command_shows_the_connections_of_the_user(
        self, commands_stand: StandProcess, open_chat: OpenChat
    ) -> None:
        """connections — строка вызова и таблица соединений пользователя; тред,
        открытый заново, показывает их же."""
        menu = CommandMenu(open_chat(commands_stand, ""))
        menu.typed("connections")
        menu.item("connections").wait_for(timeout=CommandMenu.WAIT_MS)

        menu.run("connections", "")
        shown = menu.page.get_by_text("Your connections")
        shown.wait_for(timeout=CommandMenu.WAIT_MS)

        questions = menu.chat.steps_of(StepKind.USER.value)
        expect(questions).to_have_count(1)
        expect(questions.first).to_contain_text("/connections")

        thread_id = menu.chat.log.thread_id()
        table = menu.chat.log.last_step(StepKind.ASSISTANT.value)
        assert table is not None, menu.chat.log.describe()
        again = open_chat(commands_stand, "")
        again.open_thread(thread_id, str(table[StepField.ID.value]))
        again.page.get_by_text("Your connections").wait_for(timeout=CommandMenu.WAIT_MS)
        lines = again.steps_of(StepKind.USER.value)
        expect(lines).to_have_count(1)
        expect(lines.first).to_contain_text("/connections")

    def test_compact_command_shows_its_line_and_compacts(
        self, commands_stand: StandProcess, open_chat: OpenChat, llm_port: int
    ) -> None:
        """`/compact` из меню — строка вызова и ход агента: пока агент
        пересказывает, контейнер хода подписан «сжимаю», после — шаг
        «контекст сжат»."""
        book = ReplyBookClient(StandUrl.of(llm_port))
        menu = CommandMenu(open_chat(commands_stand, ""))
        menu.chat.ask(ScenarioName.ANSWER.value)
        menu.chat.await_idle(timeout_sec=120.0)
        menu.chat.ask(ScenarioName.ANSWER.value)
        menu.chat.await_idle(timeout_sec=120.0)
        menu.typed("compact")
        menu.item("compact").wait_for(timeout=CommandMenu.WAIT_MS)

        book.hold_summary()
        try:
            menu.run("compact", "")
            book.await_held(SummaryBook.HOLD_KEY, 60.0)
            container = menu.page.locator(Selector.of_type(StepKind.RUN.value)).last
            expect(container).to_contain_text(self.COMPACTING)
        finally:
            book.release(SummaryBook.HOLD_KEY)

        menu.chat.await_idle(timeout_sec=120.0)
        menu.chat.expand_last_run()
        step = menu.page.locator(Selector.of_type(StepKind.TOOL.value)).last
        expect(step).to_contain_text(self.COMPACTED)

        questions = menu.chat.steps_of(StepKind.USER.value)
        expect(questions).to_have_count(3)
        expect(questions.last).to_contain_text("/compact")
