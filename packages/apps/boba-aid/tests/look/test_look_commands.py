"""Вид команд «/»: меню поля ввода с командами чата, агента и сервера,
приложение, которое открыла команда сервера, и таблица соединений команды
чата — в обеих темах и обоих размерах окна.

Стенд вида поднимается с хостом приложений (AppsHost): стандартный сервер
по HTTP, объявивший расширение com.boba/commands, с секцией apps и страницей
песочницы на своём источнике. Первый ход — обычный ответ фейковой модели:
с открытием сессии агент объявляет свои команды. Кадр меню — само меню над
полем ввода; кадры окон — карточка приложения и сообщение таблицы, вживую и
после перезагрузки треда: окна команд рисуются из записи истории агента
пиксель в пиксель так же.
"""

from __future__ import annotations

from collections.abc import Iterator
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
from playwright.sync_api import Error, FrameLocator, Locator, Page, WebSocket

from boba.stand.ui.fake_llm import ScenarioName
from boba.stand.ui.socket_log import SocketLog
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
        "карточка «cmds · orders_console» с кнопками, приложение показывает "
        "итог «the orders console is open for new orders»; модель не отвечала",
        (),
    )
    CONNECTIONS: ClassVar[LookState] = LookState(
        GROUP,
        "cm3-connections",
        "Команда чата connections: соединения пользователя во вкладке",
        "сообщение «Your connections» жирным и пометка курсивом, если соединений нет",
        (),
    )
    APP_AGAIN: ClassVar[LookState] = LookState(
        GROUP,
        "cm4-app-reloaded",
        "Карточка приложения команды после перезагрузки треда",
        "кадр тот же, что «Команда сервера открыла его приложение»: карточка "
        "собрана из записи истории, ресурс приложения у сервера не читался",
        (),
    )
    FEED: ClassVar[LookState] = LookState(
        GROUP,
        "cm6-feed",
        "Лента с командами: строки вызовов и их итоги",
        "строка вызова «/mcp__cmds__orders_console new» справа, под ней карточка "
        "приложения от имени Chat; ниже строка «/connections» и сообщение "
        "«Your connections»",
        (),
    )
    FEED_AGAIN: ClassVar[LookState] = LookState(
        GROUP,
        "cm7-feed-reloaded",
        "Та же лента после перезагрузки треда",
        "кадр тот же, что «Лента с командами»: строки вызовов и итоги пришли из "
        "истории агента",
        (),
    )
    CONNECTIONS_AGAIN: ClassVar[LookState] = LookState(
        GROUP,
        "cm5-connections-reloaded",
        "Таблица connections после перезагрузки треда",
        "кадр тот же, что «Команда чата connections»: сообщение из записи истории",
        (),
    )

    POPOVER: ClassVar[str] = "div.animate-slide-up:has(.command-item)"
    ITEM: ClassVar[str] = ".command-item"
    INPUT: ClassVar[str] = "#chat-input"
    CARD: ClassVar[str] = "[data-mcp-app]"
    LOGIN_USER: ClassVar[str] = "admin"
    WAIT_MS: ClassVar[float] = 120_000


class CommandsTab:
    """Вкладка вида глазами теста: меню «/», выбор команды, окна команд и
    тот же тред, открытый заново (тред — по журналу сокета). Ошибки
    страницы чата копятся с открытия."""

    def __init__(self, page: Page, base_url: str) -> None:
        self._page = page
        self._base_url = base_url
        self._log = SocketLog()
        self.errors: list[str] = []
        page.on("pageerror", self._failed)
        page.on("websocket", self._watch)

    def _failed(self, error: Error) -> None:
        self.errors.append(str(error))

    def _watch(self, socket: WebSocket) -> None:
        socket.on("framereceived", self._log.accept)

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

    def reload(self) -> None:
        """Тот же тред, открытый заново: лента собирается из истории агента."""
        thread_id = self._log.thread_id()
        self._page.goto(
            f"{self._base_url}/thread/{thread_id}", wait_until="domcontentloaded"
        )
        self._page.locator(CommandsLook.INPUT).wait_for(timeout=CommandsLook.WAIT_MS)
        self._page.wait_for_load_state("networkidle")

    def feed(self) -> Locator:
        """Лента, прокрученная к строке вызова команды сервера."""
        line = self._page.locator(
            '[data-step-type="user_message"]', has_text=f"/{CONSOLE} new"
        ).last
        line.wait_for(timeout=CommandsLook.WAIT_MS)
        line.evaluate(LookPages.TOP)

        return line.locator(LookPages.FEED)

    def blur(self) -> None:
        """Меню закрыто: поле ввода очищено."""
        self._page.locator(CommandsLook.INPUT).fill("")

    def check_clean(self) -> None:
        if self.errors:
            raise LookError(f"the chat page raised errors: {self.errors}")


@pytest.fixture(scope="module")
def commands_host() -> Iterator[AppsHost]:
    host = AppsHost(["--commands"])
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
            tab = CommandsTab(page, commands_stand.config.base_url)
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

            tab.blur()
            feed = tab.feed()
            look_pages.settle(page)
            look.frame(feed, CommandsLook.FEED, theme, size)

            tab.reload()
            card = tab.card()
            look_pages.settle(page)
            card.evaluate(LookPages.TOP)
            look.frame(card, CommandsLook.APP_AGAIN, theme, size)
            look.same_as(CommandsLook.APP_AGAIN, CommandsLook.APP, theme, size)

            table = tab.table()
            look_pages.settle(page)
            table.evaluate(LookPages.TOP)
            look.frame(table, CommandsLook.CONNECTIONS_AGAIN, theme, size)
            look.same_as(
                CommandsLook.CONNECTIONS_AGAIN, CommandsLook.CONNECTIONS, theme, size
            )

            feed = tab.feed()
            look_pages.settle(page)
            look.frame(feed, CommandsLook.FEED_AGAIN, theme, size)
            look.same_as(CommandsLook.FEED_AGAIN, CommandsLook.FEED, theme, size)
            tab.check_clean()

    look.verify()
