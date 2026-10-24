"""Вид команд «/»: меню поля ввода с командами чата, агента и сервера,
приложение, которое открыла команда сервера, и таблица соединений команды
чата — в обеих темах и обоих размерах окна.

Стенд вида поднимается с хостом приложений (AppsHost): стандартный сервер
по HTTP, объявивший расширение com.boba/commands, с секцией apps и страницей
песочницы на своём источнике. Первый ход — обычный ответ фейковой модели:
с открытием сессии агент объявляет свои команды. Кадр меню — само меню над
полем ввода; кадры окон — карточка приложения и сообщение таблицы.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import ClassVar

import pytest
from acp_stand import AppsHost
from aid_look import (
    LookCheck,
    LookError,
    LookPages,
    LookSize,
    LookStand,
    LookState,
    LookTheme,
)
from playwright.sync_api import Error, FrameLocator, Locator, Page

from boba.stand.ui.fake_llm import ScenarioName
from boba.stand.ui.stand import StandProcess

pytestmark = [pytest.mark.ui, pytest.mark.look]

SERVER = "cmds"
CONSOLE = f"mcp__{SERVER}__orders_console"


class CommandsLook:
    """Состояния группы commands и селекторы меню и ленты."""

    GROUP: ClassVar[str] = "commands"
    MENU: ClassVar[LookState] = LookState(
        GROUP,
        "cm1-menu",
        "Меню «/» над полем ввода: команды чата, агента и сервера",
        "первой — connections чата, затем compact агента, затем промпты "
        "сервера под полными именами mcp__cmds__…; у каждой значок владельца "
        "и описание, подсказка аргументов — после тире («— status»)",
        (),
    )
    APP: ClassVar[LookState] = LookState(
        GROUP,
        "cm2-app",
        "Команда сервера открыла его приложение карточкой во вкладке",
        "строки вопроса «new» в ленте нет; карточка «cmds · orders_console» с "
        "кнопками, приложение показывает итог «the orders console is open for "
        "new orders»; модель не отвечала",
        (),
    )
    CONNECTIONS: ClassVar[LookState] = LookState(
        GROUP,
        "cm3-connections",
        "Команда чата connections: соединения пользователя во вкладке",
        "сообщение «Your connections» жирным и пометка курсивом, если "
        "соединений нет; строки вопроса в ленте нет",
        (),
    )

    POPOVER: ClassVar[str] = "div.animate-slide-up:has(.command-item)"
    ITEM: ClassVar[str] = ".command-item"
    INPUT: ClassVar[str] = "#chat-input"
    CARD: ClassVar[str] = "[data-mcp-app]"
    LOGIN_USER: ClassVar[str] = "admin"
    WAIT_MS: ClassVar[float] = 120_000


class CommandsTab:
    """Вкладка вида глазами теста: меню «/», выбор команды и окна команд.
    Ошибки страницы чата копятся с открытия."""

    def __init__(self, page: Page) -> None:
        self._page = page
        self.errors: list[str] = []
        page.on("pageerror", self._failed)

    def _failed(self, error: Error) -> None:
        self.errors.append(str(error))

    def turn(self) -> None:
        """Ход с обычным ответом: агент открыл сессию и объявил команды."""
        field = self._page.locator(CommandsLook.INPUT)
        field.fill(ScenarioName.ANSWER.value)
        field.press("Enter")
        self._page.locator('[data-step-type="assistant_message"]').last.wait_for(
            timeout=CommandsLook.WAIT_MS
        )

    def menu(self, start: str, wanted: str) -> Locator:
        """Меню после набора `/<start>`, в котором есть команда wanted."""
        field = self._page.locator(CommandsLook.INPUT)
        field.click()
        field.fill("")
        field.press_sequentially(f"/{start}")
        item = self._page.locator(CommandsLook.ITEM, has_text=wanted).first
        item.wait_for(timeout=CommandsLook.WAIT_MS)

        return self._page.locator(CommandsLook.POPOVER)

    def run(self, name: str, arguments: str) -> None:
        self.menu(name, name)
        self._page.locator(CommandsLook.ITEM, has_text=name).first.click()
        field = self._page.locator(CommandsLook.INPUT)
        field.fill(arguments)
        field.press("Enter")

    def card(self) -> Locator:
        card = self._page.locator(CommandsLook.CARD).last
        card.wait_for(timeout=CommandsLook.WAIT_MS)
        self.app(card).locator("#result").get_by_text(
            "the orders console is open for new orders"
        ).wait_for(timeout=CommandsLook.WAIT_MS)

        return card

    def app(self, card: Locator) -> FrameLocator:
        return card.frame_locator("iframe").first.frame_locator("iframe")

    def table(self) -> Locator:
        """Сообщение таблицы соединений."""
        shown = self._page.locator(
            '[data-step-type="assistant_message"]', has_text="Your connections"
        ).last
        shown.wait_for(timeout=CommandsLook.WAIT_MS)

        return shown

    def blur(self) -> None:
        """Меню закрыто: поле ввода очищено."""
        self._page.locator(CommandsLook.INPUT).fill("")

    def check_clean(self) -> None:
        if self.errors:
            raise LookError(f"the chat page raised errors: {self.errors}")


@pytest.fixture(scope="module")
def commands_host(tmp_path_factory: pytest.TempPathFactory) -> Iterator[AppsHost]:
    host = AppsHost(Path(tmp_path_factory.mktemp("look-commands")), ["--commands"])
    try:
        host.await_listening()
        yield host
    finally:
        host.stop()


@pytest.fixture(scope="module")
def commands_stand(look_stands: LookStand, commands_host: AppsHost) -> StandProcess:
    return look_stands.served(
        "commands", {SERVER: commands_host.drawn()}, LookStand.FULL_WINDOW
    )


def test_commands_menu_and_windows(
    look: LookCheck,
    look_pages: LookPages,
    commands_stand: StandProcess,
) -> None:
    for size in LookSize:
        for theme in LookTheme:
            page = look_pages.open(theme, size)
            tab = CommandsTab(page)
            look_pages.sign_in(page, commands_stand, CommandsLook.LOGIN_USER)
            tab.turn()

            menu = tab.menu("", "compact")
            tab.menu("", CONSOLE)
            look_pages.settle(page)
            look.frame(menu, CommandsLook.MENU, theme, size)
            tab.blur()

            tab.run(CONSOLE, "new")
            card = tab.card()
            look_pages.settle(page)
            card.evaluate(LookPages.TOP)
            look.frame(card, CommandsLook.APP, theme, size)

            tab.run("connections", "")
            table = tab.table()
            look_pages.settle(page)
            table.evaluate(LookPages.TOP)
            look.frame(table, CommandsLook.CONNECTIONS, theme, size)
            tab.check_clean()

    look.verify()
