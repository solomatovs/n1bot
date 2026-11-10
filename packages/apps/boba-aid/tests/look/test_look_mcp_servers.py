"""Вид приложения MCP-серверов в ленте чата: сообщение «/mcp» со списком
серверов в трёх состояниях и карточка сервера — в обеих темах и обоих
размерах окна.

Стенд вида поднимается с тремя серверами, выданными роли: стандартный сервер по
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
from playwright.sync_api import Error, Locator, Page, expect

from boba.stand.ui.fake_llm import ScenarioName
from boba.stand.ui.stand import StandProcess

pytestmark = [pytest.mark.ui, pytest.mark.look]

LIVE = "live"
GONE = "gone"
DENIED = "denied"
"""Серверы роли стенда: живой, закрытый порт и отказ авторизации."""


class ServersLook:
    """Состояния группы mcp-servers и селекторы приложения."""

    GROUP: ClassVar[str] = "mcp-servers"
    SERVER_LIST: ClassVar[LookState] = LookState(
        GROUP,
        "ms1-list",
        "Приложение в ленте: список серверов роли в трёх состояниях",
        "заголовок с числом серверов, поиск по имени и кнопка добавления; "
        "отдельные карточки с иконкой сервера и цветной точкой состояния, "
        "именем, пометкой «От роли», состоянием и кнопками переподключения "
        "и просмотра настроек; denied — «Требуется вход» и стоит первой",
        (),
    )
    LIVE_CARD: ClassVar[LookState] = LookState(
        GROUP,
        "ms2-card",
        "Карточка сервера: транспорт, адрес, вход и сроки",
        "список остаётся на месте; строка live раскрыта стрелкой слева; "
        "в шапке пометка «От роли», состояние «Подключён», заблокированный "
        "чекбокс и справа кнопка «Переподключить»; под "
        "разделителем пары «Транспорт / streamable-http», «Адрес» моноширинным "
        "и «Инструменты» с числом; кнопка «Показать инструменты»",
        (),
    )
    LIVE_TOOLS: ClassVar[LookState] = LookState(
        GROUP,
        "ms2-tools",
        "Инструменты подключённого сервера",
        "список в рамке с разделителями, описаниями и пометкой «чтение» у add",
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

    ADD_FORM: ClassVar[LookState] = LookState(
        GROUP,
        "ms4-add",
        "Форма нового MCP-сервера",
        "диалог с именем и URL, переключателями транспорта и входа, "
        "свёрнутыми дополнительными заголовками и кнопками отмены и создания",
        (),
    )
    EDIT_FORM: ClassVar[LookState] = LookState(
        GROUP,
        "ms5-edit",
        "Форма своего MCP-сервера",
        "диалог с сохранёнными именем и адресом, пустыми значениями секретов, "
        "кнопками сохранения и удаления",
        (),
    )
    REMOVE_FORM: ClassVar[LookState] = LookState(
        GROUP,
        "ms6-remove",
        "Подтверждение удаления MCP-сервера",
        "поле подтверждения имени, неактивная кнопка удаления и отмена",
        (),
    )
    ROLE_FORM: ClassVar[LookState] = LookState(
        GROUP,
        "ms7-role",
        "Параметры MCP-сервера роли",
        "серые заблокированные поля и выбранные варианты входа и транспорта, "
        "пояснение о выдаче роли, нет сохранения и удаления",
        (),
    )

    APP: ClassVar[str] = "div[data-mcp-servers]"
    SERVER: ClassVar[str] = "[data-mcp-server='{name}']"
    CARD: ClassVar[str] = "[data-mcp-card='{name}']"
    TOOLS: ClassVar[str] = "[data-mcp-tools]"
    ISSUE: ClassVar[str] = "[data-mcp-issue]"
    TOGGLE: ClassVar[str] = "[data-mcp-tools-toggle]"
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
        """Строка сервера раскрывает сведения под своей шапкой."""
        self.server(name).click()
        card = self.card(name)
        card.locator("dl").wait_for(timeout=ServersLook.WAIT_MS)

        return card

    def collapse(self, name: str) -> None:
        """Сведения скрываются, шапка остаётся в списке."""
        self.server(name).click()
        expect(self.server(name)).to_have_attribute("aria-expanded", "false")

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
            look_pages.settle(page)
            look.frame(shown, ServersLook.LIVE_CARD, theme, size)

            tab.show_tools(card)
            look_pages.settle(page)
            look.frame(
                card.locator(ServersLook.TOOLS), ServersLook.LIVE_TOOLS, theme, size
            )

            tab.collapse(LIVE)
            failed = tab.open_card(GONE)
            failed.locator(ServersLook.ISSUE).wait_for(timeout=ServersLook.WAIT_MS)
            look_pages.settle(page)
            look.frame(shown, ServersLook.FAILED_CARD, theme, size)
            tab.check_clean()

    look.verify()


def test_mcp_server_forms(
    look: LookCheck,
    look_pages: LookPages,
    servers_stand: StandProcess,
    live_host: AppsHost,
) -> None:
    endpoint = live_host.plain()["endpoint"]
    location = endpoint["service"]
    url = f"{location['scheme']}://{location['host']}:{location['port']}{endpoint['path']}"
    for size in LookSize:
        for theme in LookTheme:
            page = look_pages.open(theme, size)
            tab = ServersTab(page)
            look_pages.sign_in(page, servers_stand, ServersLook.LOGIN_USER)
            tab.turn()
            tab.run()
            tab.list()
            role_card = tab.open_card(LIVE)
            role_card.locator("[data-mcp-settings]").click()
            role_form = page.locator("[data-mcp-readonly]").last
            expect(role_form.locator('[name="mcp-name"]')).to_have_value(LIVE)
            expect(role_form.locator('[name="mcp-url"]')).to_be_disabled()
            role_form.scroll_into_view_if_needed()
            look_pages.settle(page)
            # Chainlit проверяет прокрутку с задержкой 500 мс после изменения вида.
            page.wait_for_timeout(1000)
            role_fields = role_form.locator("form").locator("..")
            look.frame(role_fields, ServersLook.ROLE_FORM, theme, size)
            role_form.locator("[data-mcp-form-close]").click()
            tab.collapse(LIVE)
            tab.list().locator("[data-mcp-add]").click()
            form = page.locator("[data-mcp-form]").last
            form.locator('[name="mcp-name"]').fill("personal")
            form.locator('[name="mcp-url"]').fill("https://mcp.example.com/mcp")
            look_pages.settle(page)
            look.frame(form, ServersLook.ADD_FORM, theme, size)
            form.locator('[name="mcp-url"]').fill(url)
            form.locator('button[type="submit"]').click()
            card = page.locator('[data-mcp-card="personal"]').last
            card.locator("[data-mcp-edit]").click()
            form = page.locator("[data-mcp-form]").last
            form.locator('[name="mcp-name"][value="personal"]').wait_for()
            form.locator('[name="mcp-url"]').fill("https://mcp.example.com/mcp")
            look_pages.settle(page)
            look.frame(form, ServersLook.EDIT_FORM, theme, size)
            form.locator("[data-mcp-remove]").click()
            confirm = form.locator("[data-mcp-remove-confirm]")
            look_pages.settle(page)
            look.frame(form, ServersLook.REMOVE_FORM, theme, size)
            confirm.get_by_role("textbox").fill("personal")
            confirm.locator("button").first.click()
            tab.list().wait_for()
            tab.check_clean()

    look.verify()
