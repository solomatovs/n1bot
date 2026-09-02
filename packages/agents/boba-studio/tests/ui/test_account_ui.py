"""Вход через форму studio и личный кабинет: свои соединения создаются и удаляются."""

from __future__ import annotations

import re
from typing import ClassVar

import pytest
from playwright.sync_api import Browser, Page, expect

from boba.stand.ui.database import StandDatabase
from boba.stand.ui.stand import StandApp, StandProcess

pytestmark = pytest.mark.ui

PAGE_TIMEOUT_MS = 60_000


class Selector:
    """Селекторы страниц входа и кабинета."""

    LOGIN_FORM: ClassVar[str] = 'form[aria-label="sign in"]'
    LOGIN_NOTICE: ClassVar[str] = '[data-notice="login"]'
    ACCOUNT_LOGIN: ClassVar[str] = ".account__login"
    GEAR: ClassVar[str] = 'a[aria-label="Account"]'
    NEW_CONNECTION: ClassVar[str] = ".connections__list .list__new"
    CONNECTION_ITEM: ClassVar[str] = ".connections__list .item"


@pytest.fixture
def page(stand: StandProcess, browser: Browser) -> Page:
    context = browser.new_context()
    opened = context.new_page()
    opened.set_default_timeout(PAGE_TIMEOUT_MS)
    return opened


def _sign_in(page: Page, stand: StandProcess) -> None:
    credential = stand.config.credential()
    page.locator(Selector.LOGIN_FORM).locator('input[name="username"]').fill(
        credential.login
    )
    page.locator(Selector.LOGIN_FORM).locator('input[name="password"]').fill(
        credential.password
    )
    page.get_by_role("button", name="Sign in", exact=True).click()


def test_anonymous_is_sent_to_login_and_returns_after_sign_in(
    page: Page, stand: StandProcess
) -> None:
    page.goto(
        f"{stand.config.base_url}/workflow/account", wait_until="domcontentloaded"
    )

    # первый ответ 401 может ждать прогрева процесса: дольше стандартных 5 с
    expect(page).to_have_url(re.compile(r"/workflow/login$"), timeout=30_000)

    _sign_in(page, stand)

    expect(page).to_have_url(re.compile(r"/workflow/account$"))
    expect(page.locator(Selector.ACCOUNT_LOGIN)).to_have_text(
        stand.config.credential().login
    )


def test_wrong_password_is_reported(page: Page, stand: StandProcess) -> None:
    page.goto(f"{stand.config.base_url}/workflow/login", wait_until="domcontentloaded")
    page.locator(Selector.LOGIN_FORM).locator('input[name="username"]').fill("admin")
    page.locator(Selector.LOGIN_FORM).locator('input[name="password"]').fill("wrong")
    page.get_by_role("button", name="Sign in", exact=True).click()

    expect(page.locator(Selector.LOGIN_NOTICE)).to_be_visible()
    expect(page).to_have_url(re.compile(r"/workflow/login$"))


def test_gear_opens_account_and_own_connection_round_trips(
    page: Page, stand: StandProcess
) -> None:
    page.goto(f"{stand.config.base_url}/workflow/login", wait_until="domcontentloaded")
    _sign_in(page, stand)
    expect(page).to_have_url(re.compile(r"/workflow/workflow$"))

    page.locator(Selector.GEAR).click()
    expect(page).to_have_url(re.compile(r"/workflow/account$"))

    page.locator(Selector.NEW_CONNECTION).click()
    page.get_by_label("connection name").fill("ui-own")
    page.get_by_label("profile.kind", exact=True).select_option("web")
    page.get_by_label("profile.base_url", exact=True).fill(
        f"http://127.0.0.1:{stand.config.llm_port}/health"
    )
    # проверка черновика до сохранения: фейковый LLM стенда отвечает по /health
    page.get_by_role("button", name="Check", exact=True).click()
    expect(page.locator('[data-notice="probe"]')).to_contain_text("HTTP 200")
    # вложенный блок auth: вариант по method и его поля
    page.get_by_label("profile.auth.method", exact=True).select_option("basic")
    page.get_by_label("profile.auth.user", exact=True).fill("reader")
    page.get_by_label("profile.auth.password", exact=True).fill("secret")
    page.get_by_role("button", name="Save", exact=True).click()

    # после сохранения список перечитывается, форма открывается на новой строке
    own = page.locator(Selector.CONNECTION_ITEM).filter(has_text="ui-own")
    expect(own).to_have_count(1)
    expect(own).to_have_class(re.compile(r"item--on"))
    expect(page.get_by_label("connection name")).to_have_value("ui-own")

    # правка своего: другой kind перестраивает форму по схеме, PUT заменяет профиль
    page.get_by_label("profile.kind", exact=True).select_option("postgres")
    page.get_by_label("profile.host", exact=True).fill("db.test")
    page.get_by_label("profile.auth.method", exact=True).select_option("trust")
    page.get_by_label("profile.auth.user", exact=True).fill("reader")
    # dbname обязателен валидатором модели, не схемой: сервер отвечает 422 текстом
    page.get_by_role("button", name="Save", exact=True).click()
    expect(page.locator('[data-notice="connection"]')).to_contain_text("dbname")
    page.get_by_label("profile.dbname", exact=True).fill("boba")
    page.get_by_role("button", name="Save", exact=True).click()
    expect(own.locator(".item__meta")).to_have_text("postgres")

    page.get_by_role("button", name="Delete", exact=True).click()

    expect(
        page.locator(Selector.CONNECTION_ITEM).filter(has_text="ui-own")
    ).to_have_count(0)


def test_missing_type_connection_is_marked_and_deletable(
    page: Page, stand: StandProcess
) -> None:
    """Строка типа без пакета: пометка в списке, заглушка вместо формы, Delete."""
    page.goto(f"{stand.config.base_url}/workflow/login", wait_until="domcontentloaded")
    _sign_in(page, stand)
    page.locator(Selector.GEAR).click()

    page.locator(Selector.NEW_CONNECTION).click()
    page.get_by_label("connection name").fill("ui-broken")
    page.get_by_label("profile.kind", exact=True).select_option("web")
    page.get_by_label("profile.base_url", exact=True).fill("http://broken.test")
    page.get_by_role("button", name="Save", exact=True).click()
    expect(
        page.locator(Selector.CONNECTION_ITEM).filter(has_text="ui-broken")
    ).to_have_count(1)

    # пакет типа «удаляется»: строка получает kind, которого нет в реестре
    StandDatabase(StandApp.STUDIO, stand.config.db_name).break_connection_kind(
        "ui-broken", "vanished"
    )
    page.reload(wait_until="domcontentloaded")

    broken = page.locator(Selector.CONNECTION_ITEM).filter(has_text="ui-broken")
    expect(broken).to_have_count(1)
    expect(broken.locator(".item__meta")).to_contain_text("not installed")

    broken.click()
    expect(page.locator(".connections__missing")).to_contain_text("is not installed")

    page.get_by_role("button", name="Delete connection", exact=True).click()
    expect(
        page.locator(Selector.CONNECTION_ITEM).filter(has_text="ui-broken")
    ).to_have_count(0)
