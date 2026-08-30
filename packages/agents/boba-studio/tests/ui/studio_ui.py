"""Помощники ui-тестов studio: вход через API studio и cookie сессии."""

from __future__ import annotations

import httpx
from playwright._impl._api_structures import SetCookieParam

from boba.stand.ui.stand import StandProcess

BOOT_TIMEOUT_SEC = 120.0


def login_cookies(stand: StandProcess, login: str = "") -> list[SetCookieParam]:
    """Вход по паролю через API studio: cookie сессии — как у браузера."""
    credential = stand.config.credential(login)
    response = httpx.post(
        f"{stand.config.base_url}/api/v1/auth/login",
        json={"username": credential.login, "password": credential.password},
        timeout=30.0,
    )
    if response.status_code >= 300:
        raise RuntimeError(f"login failed: {response.status_code} {response.text[:200]}")

    cookies: list[SetCookieParam] = []
    for name, value in response.cookies.items():
        cookies.append({"name": name, "value": value, "domain": "127.0.0.1", "path": "/"})

    if not cookies:
        raise RuntimeError("login returned no cookies")

    return cookies
