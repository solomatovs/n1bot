"""JSON API каталога на живом стенде chainlit: маршруты стоят раньше catch-all,
вход по cookie даёт субъекта с ролями стенда.
"""

from __future__ import annotations

import httpx
import pytest

from boba.stand.ui.stand import StandProcess

pytestmark = pytest.mark.ui

SNAPSHOT = "/api/catalog/snapshot"
DRAFTS = "/api/catalog/drafts"


def _client(stand: StandProcess, login: str) -> httpx.Client:
    """Клиент с cookie входа формой chainlit: тот же путь, что у пользователя."""
    credential = stand.config.credential(login)
    response = httpx.post(
        f"{stand.config.base_url}/login",
        data={"username": credential.login, "password": credential.password},
        timeout=30.0,
    )
    if response.status_code != 200:
        msg = f"login failed: {response.status_code} {response.text[:200]}"
        raise RuntimeError(msg)

    return httpx.Client(
        base_url=stand.config.base_url, cookies=response.cookies, timeout=30.0
    )


def test_anonymous_request_gets_json_401(stand: StandProcess) -> None:
    response = httpx.get(f"{stand.config.base_url}{SNAPSHOT}", timeout=30.0)

    assert response.status_code == 401
    assert response.headers["content-type"].startswith("application/json")


def test_stand_roles_reach_the_catalog(stand: StandProcess) -> None:
    with _client(stand, "dev") as dev:
        snapshot = dev.get(SNAPSHOT)
        assert snapshot.status_code == 200
        assert "layers" in snapshot.json()

        refused = dev.post(DRAFTS, json={"name": "dev draft"})
        assert refused.status_code == 403
        assert "no role to edit" in refused.json()["detail"]

    with _client(stand, "admin") as admin:
        created = admin.post(DRAFTS, json={"name": "admin draft"})
        assert created.status_code == 200
        draft_id = created.json()["id"]

        state = admin.get(f"{DRAFTS}/{draft_id}")
        assert state.status_code == 200
        assert state.json()["seq"] == 0

        discarded = admin.delete(f"{DRAFTS}/{draft_id}")
        assert discarded.status_code == 200
        assert discarded.json()["status"] == "discarded"
