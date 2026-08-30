"""Помощники ui-тестов studio: вход через API studio и cookie сессии."""

from __future__ import annotations

from uuid import UUID

import httpx
from playwright._impl._api_structures import SetCookieParam

from boba.identity.context import Scope
from boba.identity.sso import OwnRequest
from boba.messaging import LockToken, SignInRefreshRequested
from boba.runtime.bus import PgMessageBus
from boba.runtime.config import AppName, StudioRuntimeConfig
from boba.stand.ui.database import run_blocking
from boba.stand.ui.stand import StandProcess

BOOT_TIMEOUT_SEC = 120.0


def login_cookies(stand: StandProcess, login: str = "") -> list[SetCookieParam]:
    """Вход по паролю через API studio: cookie сессии — как у браузера."""
    credential = stand.config.credential(login)
    response = httpx.post(
        f"{stand.config.base_url}/api/v1/auth/login",
        json={"username": credential.login, "password": credential.password},
        headers={OwnRequest.HEADER.value: OwnRequest.VALUE.value},
        timeout=30.0,
    )
    if response.status_code >= 300:
        raise RuntimeError(
            f"login failed: {response.status_code} {response.text[:200]}"
        )

    cookies: list[SetCookieParam] = []
    for name, value in response.cookies.items():
        cookies.append(
            {"name": name, "value": value, "domain": "127.0.0.1", "path": "/"}
        )

    if not cookies:
        raise RuntimeError("login returned no cookies")

    return cookies


def publish_refresh(
    config: StudioRuntimeConfig, db_name: str, user_id: str, principal: str
) -> None:
    """Сигнал обновления входа в область пользователя — как из обвязки инструментов."""
    cfg = config.data_layer.postgres.model_copy(update={"dbname": db_name})
    # шина живёт в схеме [cluster], а не в схеме данных приложения
    bus = PgMessageBus(
        cfg, config.cluster.db_schema, "ui-test", AppName.STUDIO, config.cluster
    )

    async def run() -> None:
        await bus.setup()
        message = SignInRefreshRequested(principal=principal)
        await bus.publish(Scope.user(UUID(user_id)), message, LockToken.local())

    run_blocking(run())
