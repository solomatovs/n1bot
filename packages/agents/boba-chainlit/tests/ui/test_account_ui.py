"""Вход через форму studio и личный кабинет: свои соединения создаются и удаляются."""

from __future__ import annotations

import re
from typing import ClassVar

import pytest
from playwright.sync_api import Browser, Page, expect

from ui.stand import StandProcess

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

    expect(page).to_have_url(re.compile(r"/workflow/login$"))

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
    expect(page).to_have_url(re.compile(r"/workflow/observe$"))

    page.locator(Selector.GEAR).click()
    expect(page).to_have_url(re.compile(r"/workflow/account$"))

    page.locator(Selector.NEW_CONNECTION).click()
    page.get_by_label("connection name").fill("ui-own")
    page.get_by_label("profile.kind", exact=True).select_option("web")
    page.get_by_label("profile.base_url", exact=True).fill("https://own.test")
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
