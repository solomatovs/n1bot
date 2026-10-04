"""JSON API каталога на живом стенде studio: вход по cookie даёт субъекта с
ролями стенда, без входа — 401 JSON.
"""

from __future__ import annotations

import httpx
import pytest
from catalog_ui import Api, ApiPath, CatalogPage, Tabs, api_client
from playwright.sync_api import Browser, expect

from boba.stand.ui.stand import StandProcess
from boba.studio.catalog.api import CatalogUrl

pytestmark = pytest.mark.ui

PROCESSES = ApiPath.catalog(CatalogUrl.PROCESSES)
DRAFTS = ApiPath.catalog(CatalogUrl.DRAFTS)


def test_anonymous_request_gets_json_401(stand: StandProcess) -> None:
    response = httpx.get(f"{stand.config.base_url}{PROCESSES}", timeout=30.0)

    assert response.status_code == 401
    assert response.headers["content-type"].startswith("application/json")


def test_stand_roles_reach_the_catalog(stand: StandProcess) -> None:
    with api_client(stand, "admin") as admin:
        created = admin.post(PROCESSES, json={"name": "api_ui_process"})
        assert created.status_code == 200, created.text
        process_id = created.json()["id"]

    try:
        with api_client(stand, "dev") as dev:
            snapshot = dev.get(
                ApiPath.catalog(CatalogUrl.PROCESS_SNAPSHOT, process_id=process_id)
            )
            assert snapshot.status_code == 200
            assert "groups" in snapshot.json()

            refused = dev.post(
                DRAFTS, json={"process_id": process_id, "name": "dev draft"}
            )
            assert refused.status_code == 403
            assert "no role to edit" in refused.json()["detail"]

        with api_client(stand, "admin") as admin:
            draft = admin.post(
                DRAFTS, json={"process_id": process_id, "name": "admin draft"}
            )
            assert draft.status_code == 200
            draft_id = draft.json()["id"]

            state = admin.get(ApiPath.catalog(CatalogUrl.DRAFT, draft_id=draft_id))
            assert state.status_code == 200
            assert state.json()["seq"] == 0

            discarded = admin.delete(
                ApiPath.catalog(CatalogUrl.DRAFT, draft_id=draft_id)
            )
            assert discarded.status_code == 200
            assert discarded.json()["status"] == "discarded"
    finally:
        with api_client(stand, "admin") as admin:
            admin.delete(ApiPath.catalog(CatalogUrl.PROCESS, process_id=process_id))


def test_socket_delivers_catalog_changes_to_the_page(
    browser: Browser, stand: StandProcess, catalog_api: Api
) -> None:
    """Событие каталога идёт страницей по socket.io: процесс, созданный через
    API, появляется в списке открытой вкладки без перезагрузки."""
    tabs = Tabs(browser, stand)
    try:
        page = tabs.page("admin")
        CatalogPage.HOME.open(page, stand)
        listed = page.get_by_test_id("processes-list")
        expect(listed).to_be_visible()

        name = "api_ui_events"
        process_id = catalog_api.create_process(name)
        try:
            row = listed.locator(f'li[data-process="{name}"]')
            expect(row).to_be_visible(timeout=15_000)
        finally:
            catalog_api.delete_process(process_id)

        expect(listed.locator(f'li[data-process="{name}"]')).to_have_count(
            0, timeout=15_000
        )
    finally:
        tabs.close()
