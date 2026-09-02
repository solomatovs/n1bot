"""Сигнал обновления входа доходит до страницы: она шлёт POST refresh со своей меткой.

Браузер стенда не умеет Negotiate, поэтому сам обмен здесь перехватывается: проверяется
путь сигнал → сокет → страница → запрос; обмен на стороне сервера покрыт test_sso_api.
"""

from __future__ import annotations

import time

import pytest
from playwright.sync_api import Browser, Request, Route, expect
from studio_ui import login_cookies, publish_refresh

from boba.identity.sso import OwnRequest
from boba.runtime.config import StudioRuntimeConfig
from boba.stand.ui.stand import StandProcess

pytestmark = pytest.mark.ui

REFRESH_PATH = "/api/v1/auth/refresh"


def test_refresh_signal_makes_the_page_post_refresh_with_its_mark(
    stand: StandProcess,
    browser: Browser,
    studio_config: StudioRuntimeConfig,
    stand_database: str,
) -> None:
    context = browser.new_context()
    context.add_cookies(login_cookies(stand))
    page = context.new_page()

    seen: list[Request] = []

    def intercept(route: Route, request: Request) -> None:
        seen.append(request)
        route.fulfill(status=204)

    page.route(f"**{REFRESH_PATH}", intercept)
    page.goto(
        f"{stand.config.base_url}/workflow/workflow", wait_until="domcontentloaded"
    )
    expect(page.locator(".lamp--connected")).to_be_visible(timeout=30_000)

    me = page.request.get(f"{stand.config.base_url}/api/v1/me").json()
    publish_refresh(studio_config, stand_database, str(me["id"]), str(me["login"]))

    deadline = time.monotonic() + 20.0
    while not seen and time.monotonic() < deadline:
        page.wait_for_timeout(200)

    assert seen, "the page must answer the signal with a refresh request"
    assert seen[0].method == "POST"
    assert seen[0].headers.get(OwnRequest.HEADER.value) == OwnRequest.VALUE.value
    context.close()
