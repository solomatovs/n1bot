"""Вид приложения MCP-серверов в ленте чата: сообщение «/mcp» со списком
серверов в трёх состояниях и карточка сервера — в обеих темах и обоих
размерах окна.

Стенд вида поднимается с тремя серверами профиля: стандартный сервер по
HTTP (AppsHost), сервер на закрытом порту и сервер с отказом авторизации.
Список показывает разом все три состояния: «Требуется вход», «Подключён» и
«Ошибка». Карточка живого сервера называет транспорт, адрес и происхождение
и раскрывает инструменты; карточка упавшего показывает причину строкой.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import ClassVar

import pytest
from acp_stand import AppsHost, ClosedEndpoint
from aid_look import (
    LookCheck,
    LookError,
    LookPages,
    LookSize,
    LookStand,
    LookState,
    LookTheme,
)
from playwright.sync_api import Error, Locator, Page

from boba.stand.ui.fake_llm import ScenarioName
from boba.stand.ui.stand import StandProcess

pytestmark = [pytest.mark.ui, pytest.mark.look]

LIVE = "live"
GONE = "gone"
DENIED = "denied"
"""Серверы профиля стенда: живой, закрытый порт и отказ авторизации."""


class ServersLook:
    """Состояния группы mcp-servers и селекторы приложения."""

    GROUP: ClassVar[str] = "mcp-servers"
    SERVER_LIST: ClassVar[LookState] = LookState(
        GROUP,
        "ms1-list",
        "Приложение в ленте: список серверов профиля в трёх состояниях",
        "сообщение ленты: «MCP-серверы» с числом серверов и карточка со "
        "строками через разделитель — цветная точка, имя, пометка «профиль», "
        "справа состояние и стрелка; denied — «Требуется вход» и стоит "
        "первой, live — «Подключён · инструментов: N», gone — «Ошибка»; "
        "подписи справа выровнены по правому краю",
        (),
    )
    LIVE_CARD: ClassVar[LookState] = LookState(
        GROUP,
        "ms2-card",
        "Карточка сервера: транспорт, адрес, инструменты",
        "«← К списку»; карточка: в шапке имя live, пометка «профиль», "
        "значок «Подключён» и справа небольшая кнопка «Переподключить»; под "
        "разделителем пары «Транспорт / streamable-http», «Адрес» моноширинным "
        "и «Инструменты» с числом; кнопка «Скрыть инструменты» и список "
        "инструментов в рамке с разделителями, у add — пометка «чтение»",
        (),
    )
    FAILED_CARD: ClassVar[LookState] = LookState(
        GROUP,
        "ms3-card-failed",
        "Карточка сервера не на связи: причина плашкой",
        "в шапке «gone», значок «Ошибка» и «Переподключить»; пары транспорта "
        "и адреса закрытого порта; красная плашка «Не удалось подключиться» "
        "со значком и текстом ошибки соединения моноширинным; списка "
        "инструментов нет",
        (),
    )

    APP: ClassVar[str] = "div[data-mcp-servers],div[data-mcp-card]"
    SERVER: ClassVar[str] = "[data-mcp-server='{name}']"
    CARD: ClassVar[str] = "[data-mcp-card='{name}']"
    TOOLS: ClassVar[str] = "[data-mcp-tools]"
    ISSUE: ClassVar[str] = "[data-mcp-issue]"
    TOGGLE: ClassVar[str] = "button[aria-expanded]"
    BACK: ClassVar[str] = "К списку"
    ITEM: ClassVar[str] = ".command-item"
    INPUT: ClassVar[str] = "#chat-input"
    LOGIN_USER: ClassVar[str] = "admin"
    WAIT_MS: ClassVar[float] = 120_000


class ServersTab:
    """Вкладка вида глазами теста: команда «/mcp» и её приложение в ленте.
    Ошибки страницы чата копятся с открытия."""

    def __init__(self, page: Page) -> None:
        self._page = page
        self.errors: list[str] = []
        page.on("pageerror", self._failed)

    def _failed(self, error: Error) -> None:
        self.errors.append(str(error))

    def turn(self) -> None:
        """Ход с обычным ответом: агент открыл сессию и объявил команды."""
        field = self._page.locator(ServersLook.INPUT)
        field.fill(ScenarioName.ANSWER.value)
        field.press("Enter")
        self._page.locator('[data-step-type="assistant_message"]').last.wait_for(
            timeout=ServersLook.WAIT_MS
        )

    def run(self) -> None:
        """Команда mcp выбрана в меню «/» и отправлена."""
        field = self._page.locator(ServersLook.INPUT)
        field.click()
        field.fill("")
        field.press_sequentially("/mcp")
        item = self._page.locator(ServersLook.ITEM, has_text="mcp").first
        item.wait_for(timeout=ServersLook.WAIT_MS)
        item.click()
        field.press("Enter")

    def list(self) -> Locator:
        shown = self._page.locator(ServersLook.APP).last
        shown.wait_for(timeout=ServersLook.WAIT_MS)

        return shown

    def server(self, name: str) -> Locator:
        return self._page.locator(ServersLook.SERVER.format(name=name)).last

    def card(self, name: str) -> Locator:
        card = self._page.locator(ServersLook.CARD.format(name=name)).last
        card.wait_for(timeout=ServersLook.WAIT_MS)

        return card

    def open_card(self, name: str) -> Locator:
        """Строка сервера нажата: открыта его карточка."""
        self.server(name).click()
        return self.card(name)

    def back(self) -> None:
        """Из карточки назад в список."""
        self._page.get_by_text(ServersLook.BACK).last.click()
        self.server(LIVE).wait_for(timeout=ServersLook.WAIT_MS)

    def show_tools(self, card: Locator) -> None:
        """Раскрыт список инструментов карточки."""
        card.locator(ServersLook.TOGGLE).first.click()
        card.locator(ServersLook.TOOLS).wait_for(timeout=ServersLook.WAIT_MS)

    def check_clean(self) -> None:
        if self.errors:
            raise LookError(f"the chat page raised errors: {self.errors}")


@pytest.fixture(scope="module")
def live_host() -> Iterator[AppsHost]:
    host = AppsHost([])
    try:
        host.await_listening()
        yield host
    finally:
        host.stop()


@pytest.fixture(scope="module")
def denied_host() -> Iterator[AppsHost]:
    """Сервер с отказом авторизации: в приложении он needs_auth."""
    host = AppsHost(["--denied"])
    try:
        host.await_listening()
        yield host
    finally:
        host.stop()


@pytest.fixture(scope="module")
def servers_stand(
    look_stands: LookStand, live_host: AppsHost, denied_host: AppsHost
) -> StandProcess:
    return look_stands.served(
        "mcp-servers",
        {
            LIVE: live_host.plain(),
            GONE: ClosedEndpoint().section(),
            DENIED: denied_host.plain(),
        },
        LookStand.FULL_WINDOW,
    )


def test_mcp_servers_app_of_the_chat(
    look: LookCheck,
    look_pages: LookPages,
    servers_stand: StandProcess,
) -> None:
    for size in LookSize:
        for theme in LookTheme:
            page = look_pages.open(theme, size)
            tab = ServersTab(page)
            look_pages.sign_in(page, servers_stand, ServersLook.LOGIN_USER)
            tab.turn()

            tab.run()
            shown = tab.list()
            tab.server(GONE).wait_for(timeout=ServersLook.WAIT_MS)
            look_pages.settle(page)
            look.frame(shown, ServersLook.SERVER_LIST, theme, size)

            card = tab.open_card(LIVE)
            tab.show_tools(card)
            look_pages.settle(page)
            look.frame(shown, ServersLook.LIVE_CARD, theme, size)

            tab.back()
            failed = tab.open_card(GONE)
            failed.locator(ServersLook.ISSUE).wait_for(timeout=ServersLook.WAIT_MS)
            look_pages.settle(page)
            look.frame(shown, ServersLook.FAILED_CARD, theme, size)
            tab.check_clean()

    look.verify()
