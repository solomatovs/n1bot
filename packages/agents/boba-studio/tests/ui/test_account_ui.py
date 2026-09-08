"""Вход через форму studio и личный кабинет: анонима уводит на вход, неверный
пароль виден."""

from __future__ import annotations

import re
from typing import ClassVar

import pytest
from playwright.sync_api import Browser, Page, expect

from boba.stand.ui.stand import StandProcess

pytestmark = pytest.mark.ui

PAGE_TIMEOUT_MS = 60_000


class Selector:
    """Селекторы страниц входа и кабинета."""

    LOGIN_FORM: ClassVar[str] = 'form[aria-label="sign in"]'
    LOGIN_NOTICE: ClassVar[str] = '[data-notice="login"]'
    ACCOUNT_LOGIN: ClassVar[str] = ".account__login"


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
