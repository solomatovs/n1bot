"""Синхронизация источника на песочном стенде: настоящий pg_schema_snapshot
снимает базу стенда через подключение main. Через API — полный проход,
вторая версия после CREATE TABLE с diff, третья после DROP TABLE; по DOM —
диалог подключений (привязка, отвязка), диалог синхронизации, полоса
прогресса и итога, отмена, дерево новой версии, права читателя."""

from __future__ import annotations

import re
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any, ClassVar, Final

import httpx
import pytest
from catalog_ui import api_client, ok, ok_list
from chat_ui import login_cookies
from playwright.sync_api import Browser, BrowserContext, Page, ViewportSize, expect

from boba.stand.ui.database import StandDatabase
from boba.stand.ui.look import Tokens, no_horizontal_scroll
from boba.stand.ui.stand import (
    REPO_ROOT,
    StandApp,
    StandConfig,
    StandProcess,
    free_port,
)

pytestmark = pytest.mark.ui

WIDE: ViewportSize = {"width": 1400, "height": 900}
NARROW: ViewportSize = {"width": 640, "height": 800}
TOKENS_CSS = (
    REPO_ROOT / "packages/agents/boba-chainlit/web/catalog/src/styles/tokens.css"
)
BOOT_TIMEOUT_SEC = 240.0
SYNC_TIMEOUT_SEC = 120.0


class Probe:
    """Пробники модуля в базе стенда и имена источников; DDL — литералы,
    как их принимает psycopg."""

    SOURCE: ClassVar[str] = "src_sync_stand"
    UI_SOURCE: ClassVar[str] = "src_sync_page"
    TABLE: ClassVar[str] = "sync_probe"
    COLUMN: ClassVar[str] = "probe_note"
    CONNECTION: ClassVar[str] = "main"

    CREATE: Final = "create table public.sync_probe (id integer primary key, name text)"
    ALTER: Final = "alter table public.sync_probe add column probe_note text"
    DROP: Final = "drop table if exists public.sync_probe"


@pytest.fixture(scope="module")
def sync_stand(
    stand_workdir: Path,
    llm_port: int,
    fake_llm: None,
    stand_database: str,
    stand_db: StandDatabase,
) -> Iterator[StandProcess]:
    """Стенд с песочницей: pg-инструменты идут через зиготы, как в проде."""
    config = StandConfig(
        workdir=stand_workdir / "sync",
        app=StandApp.CHAINLIT,
        app_port=free_port(),
        llm_port=llm_port,
        db_name=stand_database,
        url_prefix="/boba-sync",
        sandbox=True,
    )
    process = StandProcess(config=config, log_path=stand_workdir / "sync-app.log")
    process.start(boot_timeout_sec=BOOT_TIMEOUT_SEC)
    try:
        stand_db.seed_connections(llm_port)
        stand_db.ddl(Probe.DROP)
        yield process
    finally:
        stand_db.ddl(Probe.DROP)
        process.stop()


class SyncApi:
    """JSON API синхронизации от имени учётки стенда."""

    def __init__(self, client: httpx.Client) -> None:
        self.client = client

    def connection_id(self, kind: str, name: str) -> str:
        entries = ok_list(
            self.client.get("/api/catalog/connections", params={"kind": kind})
        )
        for entry in entries:
            if entry["name"] == name:
                return str(entry["id"])

        raise AssertionError(
            f"connection {name!r} of kind {kind!r} is not visible: {entries}"
        )

    def create_source(self, name: str) -> str:
        body = {"kind": "postgres", "name": name, "manual": False, "description": ""}
        return str(ok(self.client.post("/api/catalog/sources", json=body))["id"])

    def bind(self, source_id: str, connection_id: str) -> None:
        ok(
            self.client.post(
                f"/api/catalog/sources/{source_id}/connections",
                json={"connection_id": connection_id},
            )
        )

    def start(
        self, source_id: str, connection_id: str, schemas: list[str]
    ) -> dict[str, Any]:
        body = {
            "connection_id": connection_id,
            "scope": {"schemas": schemas, "batch_size": 50, "pause_ms": 0},
        }
        return ok(
            self.client.post(f"/api/catalog/sources/{source_id}/syncs", json=body)
        )

    def wait(self, sync_id: str) -> dict[str, Any]:
        deadline = time.monotonic() + SYNC_TIMEOUT_SEC
        while True:
            sync = ok(self.client.get(f"/api/catalog/syncs/{sync_id}"))
            if sync["status"] != "running":
                return sync

            if time.monotonic() > deadline:
                raise AssertionError(f"sync {sync_id} is still running: {sync}")

            time.sleep(0.5)

    def synced(
        self, source_id: str, connection_id: str, schemas: list[str]
    ) -> dict[str, Any]:
        started = self.start(source_id, connection_id, schemas)
        finished = self.wait(str(started["id"]))
        if finished["status"] != "done":
            raise AssertionError(f"sync failed: {finished}")

        return finished

    def tree(
        self, source_id: str, version: int, path: list[str]
    ) -> list[dict[str, Any]]:
        query = httpx.QueryParams({"version": str(version)})
        for segment in path:
            query = query.add("path", segment)

        return ok_list(
            self.client.get(f"/api/catalog/sources/{source_id}/tree", params=query)
        )

    def diff(self, source_id: str, old: int, new: int) -> list[dict[str, Any]]:
        response = self.client.get(
            f"/api/catalog/sources/{source_id}/diff", params={"old": old, "new": new}
        )
        return list(ok(response)["entries"])

    def delete_source(self, source_id: str) -> None:
        ok(self.client.delete(f"/api/catalog/sources/{source_id}"))

    def cleanup(self) -> None:
        for source in ok_list(self.client.get("/api/catalog/sources")):
            if str(source["name"]).startswith("src_sync"):
                self.delete_source(str(source["id"]))


@pytest.fixture(scope="module")
def sync_api(sync_stand: StandProcess) -> Iterator[SyncApi]:
    with api_client(sync_stand, "admin") as admin:
        api = SyncApi(admin)
        api.cleanup()
        try:
            yield api
        finally:
            api.cleanup()


class Tabs:
    def __init__(self, browser: Browser, stand: StandProcess) -> None:
        self.browser = browser
        self.stand = stand
        self.contexts: list[BrowserContext] = []

    def page(self, login: str, viewport: ViewportSize = WIDE) -> Page:
        context = self.browser.new_context(viewport=viewport)
        context.add_cookies(login_cookies(self.stand, login))
        self.contexts.append(context)
        return context.new_page()

    def close(self) -> None:
        for context in self.contexts:
            context.close()

        self.contexts.clear()


@pytest.fixture
def tabs(browser: Browser, sync_stand: StandProcess) -> Iterator[Tabs]:
    opened = Tabs(browser, sync_stand)
    try:
        yield opened
    finally:
        opened.close()


def _open_source(page: Page, stand: StandProcess, source_id: str) -> None:
    page.goto(f"{stand.config.base_url}/catalog/sources/{source_id}")
    expect(page.get_by_test_id("source-page")).to_be_visible()


def _labels(nodes: list[dict[str, Any]]) -> list[str]:
    labels: list[str] = []
    for node in nodes:
        labels.append(str(node["label"]))

    return labels


class TestSyncApi:
    """Настоящий pg_schema_snapshot против базы стенда через API."""

    def test_full_sync_then_alter_and_drop(
        self, sync_api: SyncApi, stand_db: StandDatabase, stand_database: str
    ) -> None:
        connection_id = sync_api.connection_id("postgres", Probe.CONNECTION)
        source_id = sync_api.create_source(Probe.SOURCE)
        sync_api.bind(source_id, connection_id)

        stand_db.ddl(Probe.CREATE)
        first = sync_api.synced(source_id, connection_id, ["public"])
        assert first["version"] == 1
        assert first["objects_total"] is not None
        assert first["objects_done"] == first["objects_total"]

        roots = sync_api.tree(source_id, 1, [])
        assert _labels(roots) == [stand_database]
        schemas = sync_api.tree(source_id, 1, [stand_database])
        assert _labels(schemas) == ["public"]
        groups = sync_api.tree(source_id, 1, [stand_database, "public"])
        assert "tables" in _labels(groups)
        tables = sync_api.tree(source_id, 1, [stand_database, "public", "tables"])
        assert Probe.TABLE in _labels(tables)

        stand_db.ddl(Probe.ALTER)
        second = sync_api.synced(source_id, connection_id, ["public"])
        assert second["version"] == 2

        changed = sync_api.diff(source_id, 1, 2)
        modified = [entry for entry in changed if entry["status"] == "modified"]
        assert [entry["ref"]["path"][-1] for entry in modified] == [Probe.TABLE]
        added_columns: list[str] = []
        for part in modified[0]["parts"]:
            if part["status"] == "added":
                added_columns.append(str(part["name"]))

        assert Probe.COLUMN in added_columns

        stand_db.ddl(Probe.DROP)
        third = sync_api.synced(source_id, connection_id, ["public"])
        assert third["version"] == 3

        removed = [
            entry
            for entry in sync_api.diff(source_id, 2, 3)
            if entry["status"] == "removed"
        ]
        assert [entry["ref"]["path"][-1] for entry in removed] == [Probe.TABLE]

        listed = ok_list(sync_api.client.get(f"/api/catalog/sources/{source_id}/syncs"))
        assert [item["version"] for item in listed] == [3, 2, 1]

    def test_reader_cannot_sync(
        self, sync_stand: StandProcess, sync_api: SyncApi
    ) -> None:
        connection_id = sync_api.connection_id("postgres", Probe.CONNECTION)
        source_id = sync_api.create_source(f"{Probe.SOURCE}_reader")
        sync_api.bind(source_id, connection_id)

        with api_client(sync_stand, "dev") as reader:
            response = reader.post(
                f"/api/catalog/sources/{source_id}/syncs",
                json={"connection_id": connection_id},
            )
            assert response.status_code == 403

            syncs = reader.get(f"/api/catalog/sources/{source_id}/syncs")
            assert syncs.status_code == 200


class TestSyncPage:
    """Страница источника: подключения, диалог синхронизации, прогресс."""

    def test_bind_sync_and_browse_the_new_version(
        self,
        tabs: Tabs,
        sync_stand: StandProcess,
        sync_api: SyncApi,
        stand_database: str,
    ) -> None:
        connection_id = sync_api.connection_id("postgres", Probe.CONNECTION)
        source_id = sync_api.create_source(Probe.UI_SOURCE)
        page = tabs.page("admin")
        _open_source(page, sync_stand, source_id)
        expect(page.get_by_test_id("source-sync")).to_be_visible()

        page.get_by_test_id("source-connections").click()
        dialog = page.locator('[data-dialog="source-connections"]')
        expect(dialog.get_by_test_id("no-connections")).to_be_visible()
        dialog.get_by_label("connection to bind").select_option(connection_id)
        dialog.get_by_test_id("bind-connection").click()
        expect(dialog.get_by_test_id("bound-connections")).to_contain_text(
            Probe.CONNECTION
        )
        dialog.get_by_role("button", name="close", exact=True).click()
        expect(page.get_by_test_id("source-connections")).to_contain_text(
            "connections · 1"
        )

        page.get_by_test_id("source-sync").click()
        sync_dialog = page.locator('[data-dialog="source-sync"]')
        expect(sync_dialog.get_by_label("sync connection")).to_have_value(
            re.compile(r".+")
        )
        sync_dialog.get_by_label("sync schemas").fill("public")
        sync_dialog.get_by_label("sync batch size").fill("25")
        sync_dialog.get_by_test_id("start-sync").click()

        progress = page.get_by_test_id("sync-progress")
        expect(progress).to_be_visible()
        expect(progress).to_have_attribute(
            "data-status", "done", timeout=SYNC_TIMEOUT_SEC * 1000
        )
        expect(progress).to_contain_text(f"synced v1 via {Probe.CONNECTION}")
        expect(page.get_by_label("source version")).to_have_value("1")
        expect(
            page.locator(f'[data-testid="tree-node"][data-path="{stand_database}"]')
        ).to_be_visible()
        expect(page.get_by_test_id("source-sync")).to_be_enabled()

    def test_cancel_stops_a_slow_sync(
        self, tabs: Tabs, sync_stand: StandProcess, sync_api: SyncApi
    ) -> None:
        connection_id = sync_api.connection_id("postgres", Probe.CONNECTION)
        source_id = sync_api.create_source(f"{Probe.UI_SOURCE}_slow")
        sync_api.bind(source_id, connection_id)
        page = tabs.page("admin")
        _open_source(page, sync_stand, source_id)

        page.get_by_test_id("source-sync").click()
        sync_dialog = page.locator('[data-dialog="source-sync"]')
        sync_dialog.get_by_label("sync batch size").fill("1")
        sync_dialog.get_by_label("sync pause").fill("3000")
        sync_dialog.get_by_test_id("start-sync").click()

        progress = page.get_by_test_id("sync-progress")
        expect(progress).to_have_attribute("data-status", "running")
        expect(page.get_by_test_id("source-sync")).to_be_disabled()
        page.get_by_test_id("cancel-sync").click()
        expect(progress).to_have_attribute("data-status", "cancelled", timeout=30_000)
        expect(progress).to_contain_text("cancelled by the user")
        expect(page.get_by_test_id("source-sync")).to_be_enabled()
        expect(page.get_by_label("source version")).to_have_value("0")

    def test_reader_sees_connections_without_controls(
        self, tabs: Tabs, sync_stand: StandProcess, sync_api: SyncApi
    ) -> None:
        connection_id = sync_api.connection_id("postgres", Probe.CONNECTION)
        source_id = sync_api.create_source(f"{Probe.UI_SOURCE}_reader")
        sync_api.bind(source_id, connection_id)
        page = tabs.page("dev")
        _open_source(page, sync_stand, source_id)

        expect(page.get_by_test_id("source-sync")).to_have_count(0)
        page.get_by_test_id("source-connections").click()
        dialog = page.locator('[data-dialog="source-connections"]')
        expect(dialog.get_by_test_id("bound-connections")).to_contain_text(
            Probe.CONNECTION
        )
        expect(dialog.get_by_test_id("bind-connection")).to_have_count(0)
        expect(dialog.get_by_role("button", name=re.compile("^unbind"))).to_have_count(
            0
        )


@pytest.fixture(scope="module")
def tokens() -> Tokens:
    return Tokens.load(TOKENS_CSS)


class TestSyncLook:
    """Вид новых виджетов: диалоги подключений и синхронизации, полоса итога
    по токенам, узкий экран без горизонтальной прокрутки."""

    def test_dialogs_and_status_bar_follow_tokens(
        self, tabs: Tabs, sync_stand: StandProcess, sync_api: SyncApi, tokens: Tokens
    ) -> None:
        connection_id = sync_api.connection_id("postgres", Probe.CONNECTION)
        source_id = sync_api.create_source(f"{Probe.UI_SOURCE}_look")
        sync_api.bind(source_id, connection_id)
        sync_api.synced(source_id, connection_id, ["public"])

        page = tabs.page("admin")
        _open_source(page, sync_stand, source_id)
        status = page.locator('[data-notice="sync-status"]')
        expect(status).to_be_visible()
        expect(status).to_have_css("border-left-color", tokens.rgb("signal"))
        expect(status.get_by_test_id("sync-progress")).to_have_attribute(
            "data-status", "done"
        )

        page.get_by_test_id("source-connections").click()
        dialog = page.locator('[data-dialog="source-connections"] [role="dialog"]')
        expect(dialog).to_be_visible()
        expect(dialog.get_by_test_id("bound-connections")).to_contain_text(
            Probe.CONNECTION
        )
        expect(dialog.get_by_label("connection to bind")).to_be_disabled()
        expect(dialog.get_by_test_id("bind-connection")).to_be_disabled()
        page.keyboard.press("Escape")
        expect(dialog).to_have_count(0)

        page.get_by_test_id("source-sync").click()
        sync_dialog = page.locator('[data-dialog="source-sync"] [role="dialog"]')
        expect(sync_dialog).to_be_visible()
        sync_dialog.get_by_label("sync batch size").fill("0")
        expect(sync_dialog.get_by_test_id("start-sync")).to_be_disabled()
        expect(sync_dialog.locator(".field--invalid")).to_have_count(1)
        sync_dialog.get_by_label("sync batch size").fill("10")
        expect(sync_dialog.get_by_test_id("start-sync")).to_be_enabled()
        expect(sync_dialog.locator(".field--invalid")).to_have_count(0)

    def test_narrow_screen_keeps_the_page_without_horizontal_scroll(
        self, tabs: Tabs, sync_stand: StandProcess, sync_api: SyncApi
    ) -> None:
        connection_id = sync_api.connection_id("postgres", Probe.CONNECTION)
        source_id = sync_api.create_source(f"{Probe.UI_SOURCE}_narrow")
        sync_api.bind(source_id, connection_id)
        sync_api.synced(source_id, connection_id, ["public"])

        page = tabs.page("admin", NARROW)
        _open_source(page, sync_stand, source_id)
        expect(page.locator('[data-notice="sync-status"]')).to_be_visible()
        assert no_horizontal_scroll(page)

        page.get_by_test_id("source-sync").click()
        dialog = page.locator('[data-dialog="source-sync"] [role="dialog"]')
        expect(dialog).to_be_visible()
        box = dialog.bounding_box()
        assert box is not None
        assert box["x"] >= 0
        assert box["x"] + box["width"] <= NARROW["width"]
        assert no_horizontal_scroll(page)
