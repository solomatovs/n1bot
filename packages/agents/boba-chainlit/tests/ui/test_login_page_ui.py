"""Страница логина со включённым SSO: кнопка, подсказки полей, обязательный вход.

Фронт chainlit ставит placeholder только полю логина и рисует форму пароля лишь
при password-колбэке; без него вход всё равно обязателен, а кнопку SSO ставит
sso.js — поэтому проверка идёт через браузер.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Browser, BrowserContext, Page, expect
from stand_site import Stand

from boba.chainlit.auth.kerberos import SsoLoginError
from ui.conftest import BOOT_TIMEOUT_SEC
from ui.stand import (
    StandAuth,
    StandConfig,
    StandProcess,
    free_port,
)

pytestmark = pytest.mark.ui

SSO_BUTTON = "#sso-login-btn"
LOGIN_FIELD = "#email"
PASSWORD_FIELD = "#password"
SUBMIT_BUTTON = 'form button[type="submit"]'


def _sso_stand(
    workdir: Path, llm_port: int, db_name: str, auth: StandAuth, prefix: str
) -> Iterator[StandProcess]:
    """Стенд с [auth.kerberos]: без keytab на хосте его не собрать."""
    keytab = Path(Stand.required().krb_http_keytab)
    if not keytab.is_file():
        pytest.skip(f"no service keytab at {keytab}")

    config = StandConfig(
        workdir=workdir,
        app_port=free_port(),
        llm_port=llm_port,
        db_name=db_name,
        url_prefix=prefix,
        auth=auth,
    )
    process = StandProcess(config=config, log_path=workdir / "app.log")
    process.start(boot_timeout_sec=BOOT_TIMEOUT_SEC)
    try:
        yield process
    finally:
        process.stop()


@pytest.fixture(scope="session")
def sso_stand(
    stand_workdir: Path,
    llm_port: int,
    fake_llm: None,
    stand_database: str,
) -> Iterator[StandProcess]:
    yield from _sso_stand(
        stand_workdir / "sso",
        llm_port,
        stand_database,
        StandAuth.LOCAL_SSO,
        "/boba-sso",
    )


@pytest.fixture(scope="session")
def sso_only_stand(
    stand_workdir: Path,
    llm_port: int,
    fake_llm: None,
    stand_database: str,
) -> Iterator[StandProcess]:
    yield from _sso_stand(
        stand_workdir / "sso-only",
        llm_port,
        stand_database,
        StandAuth.SSO,
        "/boba-sso-only",
    )


@pytest.fixture
def anonymous(browser: Browser) -> Iterator[BrowserContext]:
    context = browser.new_context(viewport={"width": 1280, "height": 900})
    try:
        yield context
    finally:
        context.close()


def _placeholders(base_url: str) -> tuple[str, str]:
    """Подсказки логина и пароля из переводов, которые отдаёт сам сервер."""
    response = httpx.get(f"{base_url}/project/translations", timeout=10.0)
    response.raise_for_status()

    form = response.json()["translation"]["auth"]["login"]["form"]
    return form["email"]["placeholder"], form["password"]["placeholder"]


def _login_error(base_url: str, code: SsoLoginError) -> str:
    """Текст баннера для кода ошибки из переводов сервера."""
    response = httpx.get(f"{base_url}/project/translations", timeout=10.0)
    response.raise_for_status()

    errors = response.json()["translation"]["auth"]["login"]["errors"]
    return errors[code.value]


def _login_page(context: BrowserContext, base_url: str) -> Page:
    page = context.new_page()
    page.goto(f"{base_url}/login")
    expect(page.locator(SSO_BUTTON)).to_be_visible()
    return page


def test_password_form_with_sso_button(
    anonymous: BrowserContext, sso_stand: StandProcess, stand_workdir: Path
) -> None:
    base_url = sso_stand.config.base_url
    login_hint, password_hint = _placeholders(base_url)
    assert login_hint
    assert password_hint

    page = _login_page(anonymous, base_url)

    expect(page.locator(SUBMIT_BUTTON)).to_be_visible()
    expect(page.locator(LOGIN_FIELD)).to_have_attribute("placeholder", login_hint)
    expect(page.locator(PASSWORD_FIELD)).to_have_attribute(
        "placeholder", password_hint
    )

    page.screenshot(path=str(stand_workdir / "login.png"))


def test_sso_only_keeps_login_required(
    anonymous: BrowserContext, sso_only_stand: StandProcess, stand_workdir: Path
) -> None:
    base_url = sso_only_stand.config.base_url

    auth_config = httpx.get(f"{base_url}/auth/config", timeout=10.0).json()
    assert auth_config["requireLogin"] is True
    assert auth_config["passwordAuth"] is False

    user = httpx.get(f"{base_url}/user", timeout=10.0)
    assert user.status_code == 401

    page = _login_page(anonymous, base_url)

    assert page.locator(SUBMIT_BUTTON).count() == 0
    assert page.locator(LOGIN_FIELD).count() == 0
    assert page.locator(PASSWORD_FIELD).count() == 0

    page.screenshot(path=str(stand_workdir / "login-sso-only.png"))

    page.goto(f"{base_url}/")
    page.wait_for_url(f"{base_url}/login**")
    expect(page.locator(SSO_BUTTON)).to_be_visible()


def test_sso_without_ticket_returns_to_login(
    anonymous: BrowserContext, sso_only_stand: StandProcess
) -> None:
    """Браузер без Kerberos-билета: 401 Negotiate уводит на логин с кодом ошибки."""
    base_url = sso_only_stand.config.base_url
    banner = _login_error(base_url, SsoLoginError.TICKET)

    page = _login_page(anonymous, base_url)
    page.click(SSO_BUTTON)

    page.wait_for_url(SsoLoginError.TICKET.login_url(f"{base_url}/login"))
    expect(page.get_by_text(banner)).to_be_visible()
    expect(page.locator(SSO_BUTTON)).to_be_visible()
