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
from chat_ui import BOOT_TIMEOUT_SEC
from playwright.sync_api import Browser, BrowserContext, Page, expect

from boba.auth.transport.http import SsoErrorCode
from boba.stand.site import Stand
from boba.stand.ui.config import StandOverlay
from boba.stand.ui.stand import (
    StandApp,
    StandAuth,
    StandConfig,
    StandPorts,
    StandProcess,
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
        msg = f"sso stand: [auth.kerberos] expects the service keytab at {keytab}"
        raise RuntimeError(msg)

    config = StandConfig(
        workdir=workdir,
        app=StandApp.AID,
        app_port=StandPorts().free(),
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


@pytest.fixture(scope="module")
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


@pytest.fixture(scope="module")
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


def _login_error(base_url: str, code: SsoErrorCode) -> str:
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
    assert page.locator("#realm").count() == 0
    expect(page.locator(LOGIN_FIELD)).to_have_attribute("placeholder", login_hint)
    expect(page.locator(PASSWORD_FIELD)).to_have_attribute("placeholder", password_hint)

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
    banner = _login_error(base_url, SsoErrorCode.TICKET)

    page = _login_page(anonymous, base_url)
    page.click(SSO_BUTTON)

    page.wait_for_url(SsoErrorCode.TICKET.login_url(f"{base_url}/login"))
    expect(page.get_by_text(banner)).to_be_visible()
    expect(page.locator(SSO_BUTTON)).to_be_visible()


class MultipleRealmConfig(StandConfig):
    """Настоящий чат с двумя парольными и двумя SSO-пространствами."""

    def _use_local_auth(self, overlay: StandOverlay) -> None:
        super()._use_local_auth(overlay)
        overlay.replace(
            ("auth", "partner_local"),
            {
                "type": "local",
                "identity": {"realm": "partner-local"},
                "users": {"admin": "partner-password"},
                "roles": {"local": {"mapping": {"admin": ["ADM"]}}},
            },
        )
        partner_sso = self.app.table("auth.kerberos")
        partner_sso["identity"] = {"realm": "partner"}
        partner_sso.pop("roles", None)
        overlay.replace(("auth", "partner_sso"), partner_sso)
        overlay.set(
            ("app", "auth"),
            [
                "${auth.local}",
                "${auth.partner_local}",
                "${auth.kerberos}",
                "${auth.partner_sso}",
            ],
        )


@pytest.fixture(scope="module")
def multiple_realms_stand(
    stand_workdir: Path,
    llm_port: int,
    fake_llm: None,
    stand_database: str,
) -> Iterator[StandProcess[MultipleRealmConfig]]:
    config = MultipleRealmConfig(
        workdir=stand_workdir / "multiple-realms",
        app=StandApp.AID,
        app_port=StandPorts().free(),
        llm_port=llm_port,
        db_name=stand_database,
        url_prefix="/boba-realms",
        auth=StandAuth.LOCAL_SSO,
        seed_servers=False,
    )
    process = StandProcess(config=config, log_path=config.workdir / "app.log")
    process.start(boot_timeout_sec=BOOT_TIMEOUT_SEC)
    try:
        yield process
    finally:
        process.stop()


def test_multiple_realms_choose_the_backend_and_preserve_the_identity(
    anonymous: BrowserContext,
    multiple_realms_stand: StandProcess[MultipleRealmConfig],
) -> None:
    base = multiple_realms_stand.config.base_url
    page = anonymous.new_page()
    page.goto(f"{base}/login")
    selector = page.locator("#realm")
    expect(selector).to_be_visible()
    expect(page.get_by_role("link", name="Sign in with corporate")).to_have_attribute(
        "href",
        f"{multiple_realms_stand.config.url_prefix}/auth/sso?realm=corporate",
    )
    expect(
        page.get_by_role("link", name="Sign in with partner", exact=True)
    ).to_have_attribute(
        "href",
        f"{multiple_realms_stand.config.url_prefix}/auth/sso?realm=partner",
    )
    selector.select_option("partner-local")
    page.locator(LOGIN_FIELD).fill("admin")
    page.locator(PASSWORD_FIELD).fill("partner-password")
    with page.expect_response(f"{base}/login") as submitted:
        page.locator(SUBMIT_BUTTON).click()
    assert submitted.value.status == 200
    page.wait_for_url(base)
    user = anonymous.request.get(f"{base}/user")
    assert user.status == 200
    assert user.json()["identifier"] == "partner-local:admin"
