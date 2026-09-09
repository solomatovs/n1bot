"""Страница соединений studio: свои соединения заводятся, проверяются,
правятся и удаляются на одной доске; тип без пакета помечен и удаляется."""

from __future__ import annotations

import re
from typing import ClassVar

import pytest
from playwright.sync_api import Browser, Locator, Page, expect

from boba.stand.ui.database import StandDatabase
from boba.stand.ui.stand import StandApp, StandProcess

pytestmark = pytest.mark.ui

PAGE_TIMEOUT_MS = 60_000


class Selector:
    """Селекторы входа и доски соединений."""

    LOGIN_FORM: ClassVar[str] = 'form[aria-label="sign in"]'
    PLUG: ClassVar[str] = 'a[aria-label="Connections"]'
    PAGE: ClassVar[str] = '[data-testid="connections-page"]'
    ADD: ClassVar[str] = '[data-testid="add-connection"]'
    FORM: ClassVar[str] = '[data-testid="connection-form"]'
    MINE_LIST: ClassVar[str] = '[data-testid="mine-list"]'
    DELETE_DIALOG: ClassVar[str] = '[data-dialog="connection-delete"]'


@pytest.fixture
def page(stand: StandProcess, browser: Browser) -> Page:
    context = browser.new_context()
    opened = context.new_page()
    opened.set_default_timeout(PAGE_TIMEOUT_MS)
    return opened


def _sign_in(page: Page, stand: StandProcess) -> None:
    credential = stand.config.credential()
    page.goto(f"{stand.config.base_url}/workflow/login", wait_until="domcontentloaded")
    page.locator(Selector.LOGIN_FORM).locator('input[name="username"]').fill(
        credential.login
    )
    page.locator(Selector.LOGIN_FORM).locator('input[name="password"]').fill(
        credential.password
    )
    page.get_by_role("button", name="Sign in", exact=True).click()
    expect(page).to_have_url(re.compile(r"/workflow/workflow$"))


def _row(page: Page, name: str) -> Locator:
    return page.locator(Selector.MINE_LIST).locator(f'li[data-connection="{name}"]')


def _delete(page: Page, name: str) -> None:
    page.get_by_role("button", name=f"delete {name}", exact=True).click()
    page.locator(Selector.DELETE_DIALOG).get_by_test_id("delete-connection").click()
    expect(_row(page, name)).to_have_count(0)


def test_plug_opens_connections_and_own_connection_round_trips(
    page: Page, stand: StandProcess
) -> None:
    _sign_in(page, stand)
    page.locator(Selector.PLUG).click()
    expect(page).to_have_url(re.compile(r"/workflow/connections$"))
    expect(page.locator(Selector.PAGE)).to_be_visible()

    page.locator(Selector.ADD).click()
    form = page.locator(Selector.FORM)
    form.get_by_label("connection name").fill("ui-own")
    form.get_by_label("profile.kind", exact=True).select_option("web")
    form.get_by_label("profile.scheme", exact=True).select_option("http")
    form.get_by_label("profile.host", exact=True).fill("127.0.0.1")
    form.get_by_label("profile.port", exact=True).fill(str(stand.config.llm_port))
    form.get_by_label("profile.path", exact=True).fill("/health")
    # проверка черновика до сохранения: фейковый LLM стенда отвечает по /health
    form.get_by_role("button", name="check", exact=True).click()
    expect(form.locator('[data-notice="probe"]')).to_contain_text("HTTP 200")
    # вложенный блок auth: вариант по method и его поля
    form.get_by_label("profile.auth.method", exact=True).select_option("basic")
    form.get_by_label("profile.auth.user", exact=True).fill("reader")
    form.get_by_label("profile.auth.password", exact=True).fill("secret")
    form.get_by_role("button", name="save", exact=True).click()

    # после сохранения список перечитывается: строка в группе «mine»
    own = _row(page, "ui-own")
    expect(own).to_have_count(1)
    expect(own).to_contain_text("web")

    # правка своего: другой kind перестраивает форму по схеме, PUT заменяет профиль
    page.get_by_role("button", name="edit ui-own", exact=True).click()
    form = page.locator(Selector.FORM)
    expect(form.get_by_label("connection name")).to_have_value("ui-own")
    form.get_by_label("profile.kind", exact=True).select_option("postgres")
    form.get_by_label("profile.host", exact=True).fill("db.test")
    form.get_by_label("profile.auth.method", exact=True).select_option("trust")
    form.get_by_label("profile.auth.user", exact=True).fill("reader")
    # dbname обязателен валидатором модели, не схемой: сервер отвечает 422 текстом
    form.get_by_role("button", name="save", exact=True).click()
    expect(form.locator('[data-notice="connection-error"]')).to_contain_text("dbname")
    form.get_by_label("profile.dbname", exact=True).fill("boba")
    form.get_by_role("button", name="save", exact=True).click()
    expect(form).to_have_count(0)
    expect(own).to_contain_text("postgres")

    _delete(page, "ui-own")


def test_missing_type_connection_is_marked_and_deletable(
    page: Page, stand: StandProcess
) -> None:
    """Строка типа без пакета: пометка в списке, без правки, удаляется."""
    _sign_in(page, stand)
    page.locator(Selector.PLUG).click()

    page.locator(Selector.ADD).click()
    form = page.locator(Selector.FORM)
    form.get_by_label("connection name").fill("ui-broken")
    form.get_by_label("profile.kind", exact=True).select_option("web")
    form.get_by_label("profile.host", exact=True).fill("broken.test")
    form.get_by_label("profile.port", exact=True).fill("443")
    form.get_by_role("button", name="save", exact=True).click()
    expect(_row(page, "ui-broken")).to_have_count(1)

    # пакет типа «удаляется»: строка получает kind, которого нет в реестре
    StandDatabase(StandApp.STUDIO, stand.config.db_name).break_connection_kind(
        "ui-broken", "vanished"
    )
    page.reload(wait_until="domcontentloaded")

    broken = _row(page, "ui-broken")
    expect(broken).to_have_count(1)
    expect(broken).to_have_attribute("data-available", "false")
    expect(broken.get_by_test_id("connection-missing")).to_have_text("not installed")
    edit = page.get_by_role("button", name="edit ui-broken", exact=True)
    expect(edit).to_have_count(0)

    _delete(page, "ui-broken")
