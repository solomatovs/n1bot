"""Страница логина со включённым SSO: кнопка и подсказки полей.

Фронт chainlit ставит placeholder только полю логина; у пароля его
подставляет sso.js из переводов, поэтому проверка идёт через браузер.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Browser, expect

from ui.conftest import BOOT_TIMEOUT_SEC
from ui.stand import REPO_ROOT, StandConfig, StandPaths, StandProcess, free_port

pytestmark = pytest.mark.ui

SSO_BUTTON = "#sso-login-btn"
LOGIN_FIELD = "#email"
PASSWORD_FIELD = "#password"


@pytest.fixture(scope="session")
def sso_stand(
    stand_workdir: Path,
    llm_port: int,
    fake_llm: None,
    stand_database: str,
) -> Iterator[StandProcess]:
    """Стенд с [auth.kerberos]: без keytab на хосте его не собрать."""
    keytab = StandPaths.KEYTAB.under(REPO_ROOT)
    if not keytab.is_file():
        pytest.skip(f"no service keytab at {keytab}")

    config = StandConfig(
        workdir=stand_workdir / "sso",
        app_port=free_port(),
        llm_port=llm_port,
        db_name=stand_database,
        url_prefix="/boba-sso",
        sso=True,
    )
    process = StandProcess(config=config, log_path=stand_workdir / "sso-app.log")
    process.start(boot_timeout_sec=BOOT_TIMEOUT_SEC)
    try:
        yield process
    finally:
        process.stop()


def _placeholders(base_url: str) -> tuple[str, str]:
    """Подсказки логина и пароля из переводов, которые отдаёт сам сервер."""
    response = httpx.get(f"{base_url}/project/translations", timeout=10.0)
    response.raise_for_status()

    form = response.json()["translation"]["auth"]["login"]["form"]
    return form["email"]["placeholder"], form["password"]["placeholder"]


def test_password_field_has_placeholder(
    browser: Browser, sso_stand: StandProcess, stand_workdir: Path
) -> None:
    login_hint, password_hint = _placeholders(sso_stand.config.base_url)
    assert login_hint
    assert password_hint

    context = browser.new_context(viewport={"width": 1280, "height": 900})
    try:
        page = context.new_page()
        page.goto(f"{sso_stand.config.base_url}/login")

        expect(page.locator(SSO_BUTTON)).to_be_visible()
        expect(page.locator(LOGIN_FIELD)).to_have_attribute("placeholder", login_hint)
        expect(page.locator(PASSWORD_FIELD)).to_have_attribute(
            "placeholder", password_hint
        )

        page.screenshot(path=str(stand_workdir / "login.png"))
    finally:
        context.close()
