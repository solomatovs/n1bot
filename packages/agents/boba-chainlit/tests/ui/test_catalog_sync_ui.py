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
from catalog_ui import Api, api_client, ok, ok_list, settled_box
from chat_ui import login_cookies
from playwright.sync_api import (
    Browser,
    BrowserContext,
    Locator,
    Page,
    ViewportSize,
    expect,
)

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
    CATALOG_SCHEMA: ClassVar[str] = "catalog"

    CREATE: Final = "create table public.sync_probe (id integer primary key, name text)"
    ALTER: Final = "alter table public.sync_probe add column probe_note text"
    DROP: Final = "drop table if exists public.sync_probe"


class ProbeSql:
    """Две таблицы сквозного сценария: сырые заказы и их витрина."""

    RAW: Final = "e2e_orders_raw"
    STG: Final = "e2e_orders_stg"
    CREATE_RAW: Final = (
        "create table public.e2e_orders_raw (id bigint primary key, amount numeric)"
    )
    CREATE_STG: Final = (
        "create table public.e2e_orders_stg (id bigint primary key, amount numeric)"
    )
    PROCESS: Final = "src_sync_process"
    DROP_RAW: Final = "drop table if exists public.e2e_orders_raw"
    DROP_STG: Final = "drop table if exists public.e2e_orders_stg"


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
    """JSON API синхронизации от имени учётки стенда: на каждый сценарий в
    базу стенда кладётся своё подключение `src_sync…`, синхронизация идёт
    по нему."""

    PREFIX: ClassVar[str] = "src_sync"

    def __init__(self, client: httpx.Client, stand_db: StandDatabase) -> None:
        self.client = client
        self.stand_db = stand_db
        self.api = Api(client, stand_db)

    def add_connection(self, name: str) -> str:
        """Подключение стенда postgres по имени; вернёт его id."""
        return str(self.stand_db.add_connection(name, "postgres"))

    def start(self, connection_id: str, schemas: list[str]) -> dict[str, Any]:
        body = {"schemas": schemas, "batch_size": 50, "pause_ms": 0}
        return ok(
            self.client.post(
                f"/api/catalog/connections/{connection_id}/syncs", json=body
            )
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

    def synced(self, connection_id: str, schemas: list[str]) -> dict[str, Any]:
        started = self.start(connection_id, schemas)
        finished = self.wait(str(started["id"]))
        if finished["status"] != "done":
            raise AssertionError(f"sync failed: {finished}")

        return finished

    def tree(
        self, connection_id: str, version: int, path: list[str]
    ) -> list[dict[str, Any]]:
        query = httpx.QueryParams({"version": str(version)})
        for segment in path:
            query = query.add("path", segment)

        return ok_list(
            self.client.get(
                f"/api/catalog/connections/{connection_id}/tree", params=query
            )
        )

    def diff(self, connection_id: str, old: int, new: int) -> list[dict[str, Any]]:
        response = self.client.get(
            f"/api/catalog/connections/{connection_id}/diff",
            params={"old": old, "new": new},
        )
        return list(ok(response)["entries"])

    def cleanup(self) -> None:
        """Снос своего: процессы с префиксом стенда, версии подключений
        стенда, сами подключения."""
        for process in self.api.processes():
            if str(process["name"]).startswith(self.PREFIX):
                self.api.delete_process(str(process["id"]))

        for synced in self.api.synced():
            if str(synced["name"]).startswith(self.PREFIX):
                self.api.forget_versions(str(synced["connection_id"]))

        self.stand_db.remove_connections(self.PREFIX)


@pytest.fixture(scope="module")
def sync_api(sync_stand: StandProcess, stand_db: StandDatabase) -> Iterator[SyncApi]:
    with api_client(sync_stand, "admin") as admin:
        api = SyncApi(admin, stand_db)
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


def _open_connection(page: Page, stand: StandProcess, connection_id: str) -> None:
    page.goto(f"{stand.config.base_url}/catalog/connections/{connection_id}")
    expect(page.get_by_test_id("connection-page")).to_be_visible()


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
        connection_id = sync_api.add_connection(Probe.SOURCE)

        stand_db.ddl(Probe.CREATE)
        first = sync_api.synced(connection_id, ["public"])
        assert first["connection_name"] == Probe.SOURCE
        assert first["version"] == 1
        assert first["objects_total"] is not None
        assert first["objects_done"] == first["objects_total"]

        roots = sync_api.tree(connection_id, 1, [])
        assert _labels(roots) == [stand_database]
        schemas = sync_api.tree(connection_id, 1, [stand_database])
        assert _labels(schemas) == ["public"]
        groups = sync_api.tree(connection_id, 1, [stand_database, "public"])
        assert "tables" in _labels(groups)
        tables = sync_api.tree(connection_id, 1, [stand_database, "public", "tables"])
        assert Probe.TABLE in _labels(tables)

        stand_db.ddl(Probe.ALTER)
        second = sync_api.synced(connection_id, ["public"])
        assert second["version"] == 2

        changed = sync_api.diff(connection_id, 1, 2)
        modified = [entry for entry in changed if entry["status"] == "modified"]
        assert [entry["ref"]["path"][-1] for entry in modified] == [Probe.TABLE]
        added_columns: list[str] = []
        for part in modified[0]["parts"]:
            if part["status"] == "added":
                added_columns.append(str(part["name"]))

        assert Probe.COLUMN in added_columns

        stand_db.ddl(Probe.DROP)
        third = sync_api.synced(connection_id, ["public"])
        assert third["version"] == 3

        removed = [
            entry
            for entry in sync_api.diff(connection_id, 2, 3)
            if entry["status"] == "removed"
        ]
        assert [entry["ref"]["path"][-1] for entry in removed] == [Probe.TABLE]

        listed = ok_list(
            sync_api.client.get(f"/api/catalog/connections/{connection_id}/syncs")
        )
        assert [item["version"] for item in listed] == [3, 2, 1]
        synced = {item["name"]: item for item in sync_api.api.synced()}
        assert synced[Probe.SOURCE]["latest_version"] == 3

    def test_repeated_sync_of_an_unchanged_database_has_no_diff(
        self, sync_api: SyncApi, stand_database: str
    ) -> None:
        """Две синхронизации подряд без изменений в базе: версии равны, хотя
        таблицы самого каталога между ними выросли — число строк и размер не
        считаются изменением структуры. Схема каталога исключена: в ней на
        время синхронизации живёт её же staging-таблица."""
        connection_id = sync_api.add_connection(f"{Probe.SOURCE}_twice")

        probe = sync_api.synced(connection_id, [])
        assert probe["version"] == 1
        schemas: list[str] = []
        for node in sync_api.tree(connection_id, 1, [stand_database]):
            if node["label"] != Probe.CATALOG_SCHEMA:
                schemas.append(str(node["label"]))

        assert Probe.CATALOG_SCHEMA not in schemas
        assert len(schemas) > 1

        second = sync_api.synced(connection_id, schemas)
        third = sync_api.synced(connection_id, schemas)
        assert second["version"] == 2
        assert third["version"] == 3

        assert sync_api.diff(connection_id, 2, 3) == []
        roots = sync_api.tree(connection_id, 3, [])
        assert [node["status"] for node in roots] == ["unchanged"]
        statuses = {
            str(node["label"]): str(node["status"])
            for node in sync_api.tree(connection_id, 3, [stand_database])
        }
        assert set(statuses.values()) == {"unchanged"}, statuses

    def test_reader_cannot_sync(
        self, sync_stand: StandProcess, sync_api: SyncApi
    ) -> None:
        connection_id = sync_api.add_connection(f"{Probe.SOURCE}_reader")

        with api_client(sync_stand, "dev") as reader:
            response = reader.post(
                f"/api/catalog/connections/{connection_id}/syncs", json={}
            )
            assert response.status_code == 403

            syncs = reader.get(f"/api/catalog/connections/{connection_id}/syncs")
            assert syncs.status_code == 200


class TestSyncPage:
    """Страница подключения: диалог синхронизации, прогресс, отмена."""

    def test_sync_from_the_list_fills_the_connection_page(
        self,
        tabs: Tabs,
        sync_stand: StandProcess,
        sync_api: SyncApi,
        stand_database: str,
    ) -> None:
        """Кнопка sync в строке списка открывает диалог, старт ведёт на
        страницу подключения, ход виден полосой, первая версия даёт дерево
        базы; в списке у подключения появляется чип версии."""
        name = Probe.UI_SOURCE
        sync_api.add_connection(name)
        page = tabs.page("admin")
        page.goto(f"{sync_stand.config.base_url}/catalog/connections")
        expect(page.get_by_test_id("connections-page")).to_be_visible()

        row = page.locator(
            f'[data-testid="connections-list"] li[data-connection="{name}"]'
        )
        expect(row.get_by_test_id("connection-version")).to_have_text("not synced")
        row.get_by_role("button", name=f"sync {name}").click()
        sync_dialog = page.locator('[data-dialog="connection-sync"]')
        sync_dialog.get_by_label("sync schemas").fill("public")
        sync_dialog.get_by_label("sync batch size").fill("25")
        sync_dialog.get_by_test_id("start-sync").click()

        page.wait_for_url(
            re.compile(r"/catalog/connections/[0-9a-f-]{36}$"), timeout=30_000
        )
        expect(page.get_by_test_id("page-title")).to_have_text(name)
        expect(page.locator(".topbar")).to_contain_text("postgres")
        progress = page.get_by_test_id("sync-progress")
        expect(progress).to_be_visible()
        expect(progress).to_have_attribute(
            "data-status", "done", timeout=SYNC_TIMEOUT_SEC * 1000
        )
        expect(progress).to_contain_text("synced v1")
        expect(page.get_by_label("snapshot version")).to_have_value("1")
        expect(
            page.locator(f'[data-testid="tree-node"][data-path="{stand_database}"]')
        ).to_be_visible()
        expect(page.get_by_test_id("connection-sync")).to_be_enabled()

        page.goto(f"{sync_stand.config.base_url}/catalog/connections")
        expect(row.get_by_test_id("connection-version")).to_have_text("v1")

    def test_cancel_stops_a_slow_sync(
        self, tabs: Tabs, sync_stand: StandProcess, sync_api: SyncApi
    ) -> None:
        connection_id = sync_api.add_connection(f"{Probe.UI_SOURCE}_slow")
        page = tabs.page("admin")
        _open_connection(page, sync_stand, connection_id)

        page.get_by_test_id("connection-sync").click()
        sync_dialog = page.locator('[data-dialog="connection-sync"]')
        sync_dialog.get_by_label("sync batch size").fill("1")
        sync_dialog.get_by_label("sync pause").fill("3000")
        sync_dialog.get_by_test_id("start-sync").click()

        progress = page.get_by_test_id("sync-progress")
        expect(progress).to_have_attribute("data-status", "running")
        expect(page.get_by_test_id("connection-sync")).to_be_disabled()
        page.get_by_test_id("cancel-sync").click()
        expect(progress).to_have_attribute("data-status", "cancelled", timeout=30_000)
        expect(progress).to_contain_text("cancelled by the user")
        expect(page.get_by_test_id("connection-sync")).to_be_enabled()
        expect(page.get_by_label("snapshot version")).to_have_value("0")

    def test_reader_sees_the_page_without_controls(
        self, tabs: Tabs, sync_stand: StandProcess, sync_api: SyncApi
    ) -> None:
        connection_id = sync_api.add_connection(f"{Probe.UI_SOURCE}_reader")
        sync_api.synced(connection_id, ["public"])
        page = tabs.page("dev")
        _open_connection(page, sync_stand, connection_id)

        expect(page.get_by_test_id("connection-sync")).to_have_count(0)
        expect(page.get_by_test_id("forget-versions")).to_have_count(0)
        expect(page.get_by_test_id("cancel-sync")).to_have_count(0)
        expect(page.get_by_label("snapshot version")).to_have_value("1")


@pytest.fixture(scope="module")
def tokens() -> Tokens:
    return Tokens.load(TOKENS_CSS)


class TestSyncLook:
    """Вид виджетов синхронизации: диалог, полоса итога по токенам, узкий
    экран без горизонтальной прокрутки."""

    def test_dialog_and_status_bar_follow_tokens(
        self, tabs: Tabs, sync_stand: StandProcess, sync_api: SyncApi, tokens: Tokens
    ) -> None:
        connection_id = sync_api.add_connection(f"{Probe.UI_SOURCE}_look")
        sync_api.synced(connection_id, ["public"])

        page = tabs.page("admin")
        _open_connection(page, sync_stand, connection_id)
        status = page.locator('[data-notice="sync-status"]')
        expect(status).to_be_visible()
        expect(status).to_have_css("border-left-color", tokens.rgb("signal"))
        expect(status.get_by_test_id("sync-progress")).to_have_attribute(
            "data-status", "done"
        )

        page.get_by_test_id("connection-sync").click()
        sync_dialog = page.locator('[data-dialog="connection-sync"] [role="dialog"]')
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
        connection_id = sync_api.add_connection(f"{Probe.UI_SOURCE}_narrow")
        sync_api.synced(connection_id, ["public"])

        page = tabs.page("admin", NARROW)
        _open_connection(page, sync_stand, connection_id)
        expect(page.locator('[data-notice="sync-status"]')).to_be_visible()
        assert no_horizontal_scroll(page)

        page.get_by_test_id("connection-sync").click()
        dialog = page.locator('[data-dialog="connection-sync"] [role="dialog"]')
        expect(dialog).to_be_visible()
        box = dialog.bounding_box()
        assert box is not None
        assert box["x"] >= 0
        assert box["x"] + box["width"] <= NARROW["width"]
        assert no_horizontal_scroll(page)


class FlowBuilder:
    """Действия пользователя на странице процесса стенда: открыть черновик,
    дойти до таблиц источника, поставить их в слой, соединить потоком."""

    def __init__(self, page: Page, stand: StandProcess, database: str, tokens: Tokens):
        self.page = page
        self.stand = stand
        self.database = database
        self.tokens = tokens

    def new_process_and_draft(self, process: str) -> None:
        """«process» со входа заводит черновик нового процесса: страница
        открывает пустой черновик с именем будущего процесса."""
        page = self.page
        page.goto(f"{self.stand.config.base_url}/catalog/")
        page.get_by_test_id("new-process").click()
        form = page.get_by_test_id("new-process-form")
        form.get_by_label("process name").fill(process)
        form.get_by_role("button", name="create").click()
        page.wait_for_url(re.compile(r"/catalog/drafts/[0-9a-f-]{36}"), timeout=30_000)
        expect(page.get_by_test_id("catalog-page")).to_have_attribute(
            "data-editable", "true"
        )
        expect(page.get_by_test_id("page-title")).to_have_text(process)
        expect(page.locator('[data-notice="draft-bar"]')).to_contain_text(process)
        # холст есть сразу, пустой и готовый принимать таблицы
        page.wait_for_selector(
            '[data-testid="canvas"][data-ready="true"]', timeout=30_000
        )
        expect(page.locator('[data-testid="catalog-node"]')).to_have_count(0)
        row = page.get_by_test_id("processes-list").locator(
            f'li[data-draft="{process}"]'
        )
        expect(row).to_contain_text("new process")

    def open_tables(self, connection: str) -> Locator:
        """Вкладка подключений; дерево раскрывается до таблиц."""
        page = self.page
        pane = page.get_by_test_id("left-pane")
        pane.get_by_role("tab", name="connections").click()
        expect(pane).to_have_attribute("data-tab", "connections")
        branch = pane.locator(
            f'[data-testid="connection-branch"][data-connection="{connection}"]'
        )
        branch.get_by_role("button", name=re.compile("^expand connection")).click()
        db = self.database
        for path in (db, f"{db}/public", f"{db}/public/tables"):
            item = branch.locator(f'[data-testid="tree-node"][data-path="{path}"]')
            expect(item).to_be_visible(timeout=15_000)
            row = item.locator(".tree__row").first
            row.get_by_role("button", name="expand").click()

        return branch

    def add_table(self, branch: Locator, table: str, at: tuple[float, float]) -> None:
        """Таблица из дерева перетаскивается на холст в точку at (смещение от
        левого верхнего угла холста)."""
        page = self.page
        path = f"{self.database}/public/tables/{table}"
        source = branch.locator(
            f'[data-testid="tree-node"][data-path="{path}"]'
        ).locator(".tree__label")
        canvas = page.get_by_test_id("canvas")
        start = source.bounding_box()
        end = canvas.bounding_box()
        assert start is not None
        assert end is not None
        page.mouse.move(
            start["x"] + start["width"] / 2, start["y"] + start["height"] / 2
        )
        page.mouse.down()
        page.mouse.move(end["x"] + at[0], end["y"] + at[1], steps=8)
        page.mouse.move(end["x"] + at[0] + 1, end["y"] + at[1] + 1, steps=2)
        page.mouse.up()
        ref = f"{self.database}/public/{table}"
        expect(
            page.locator(f'[data-testid="catalog-node"][data-node="{ref}"]')
        ).to_be_visible(timeout=15_000)

    def connect(self, source: str, target: str, column: str) -> None:
        """Линия мышью от колонки одной карточки к той же колонке другой: пара
        уходит в поток без формы."""
        page = self.page
        source_row = page.locator(
            f'[data-testid="catalog-node"][data-node="{self.database}/public/{source}"]'
        ).locator(f'[data-column="{column}"]')
        target_row = page.locator(
            f'[data-testid="catalog-node"][data-node="{self.database}/public/{target}"]'
        ).locator(f'[data-column="{column}"]')
        source_row.hover()
        start = settled_box(page, source_row.locator(".react-flow__handle.source"))
        page.mouse.move(
            start["x"] + start["width"] / 2, start["y"] + start["height"] / 2
        )
        page.mouse.down()
        target_row.hover()
        end = settled_box(page, target_row.locator(".react-flow__handle.target"))
        page.mouse.move(
            end["x"] + end["width"] / 2, end["y"] + end["height"] / 2, steps=12
        )
        page.mouse.up()


@pytest.fixture
def flow_user(
    tabs: Tabs, sync_stand: StandProcess, stand_database: str, tokens: Tokens
) -> FlowBuilder:
    return FlowBuilder(tabs.page("admin"), sync_stand, stand_database, tokens)


class TestEndToEnd:
    """Путь пользователя целиком: подключение → синхронизация → новый процесс
    → черновик → слой → две настоящие таблицы на холсте → поток между ними с
    парами колонок → публикация. Именно ради этого каталог и существует."""

    def test_flow_between_two_real_tables(
        self,
        flow_user: FlowBuilder,
        sync_api: SyncApi,
        stand_db: StandDatabase,
        stand_database: str,
    ) -> None:
        stand_db.ddl(ProbeSql.DROP_STG)
        stand_db.ddl(ProbeSql.DROP_RAW)
        stand_db.ddl(ProbeSql.CREATE_RAW)
        stand_db.ddl(ProbeSql.CREATE_STG)
        connection = f"{Probe.SOURCE}_e2e"
        connection_id = sync_api.add_connection(connection)
        sync_api.synced(connection_id, ["public"])

        user = flow_user
        page = user.page
        user.new_process_and_draft(ProbeSql.PROCESS)
        branch = user.open_tables(connection)

        # обе таблицы встают на холст кнопкой панели объекта
        user.add_table(branch, ProbeSql.RAW, (60, 80))
        user.add_table(branch, ProbeSql.STG, (400, 80))
        # панель узла после броска не открывается: сцена не сжимается
        expect(page.get_by_test_id("detail-panel")).to_have_count(0)

        # линии между колонками: id → id заводит поток, amount → amount добавляет пару
        edges = page.locator('[data-testid="flow-edge-label"]')
        user.connect(ProbeSql.RAW, ProbeSql.STG, "id")
        expect(edges.filter(has_text="1 col")).to_have_count(1)
        page.get_by_role("tab", name="all fields").click()
        user.connect(ProbeSql.RAW, ProbeSql.STG, "amount")
        expect(edges.filter(has_text="2 cols")).to_have_count(1)
        expect(page.get_by_test_id("flow-form")).to_have_count(0)

        page.get_by_test_id("left-pane").get_by_role("tab", name="process").click()
        page.get_by_test_id("publish-button").click()

        # публикация создала процесс и увела на него: те же таблицы и поток
        page.wait_for_url(
            re.compile(r"/catalog/processes/[0-9a-f-]{36}$"), timeout=30_000
        )
        process_id = sync_api.api.process_id_of(ProbeSql.PROCESS)
        assert page.url.rstrip("/").endswith(process_id)
        expect(page.get_by_test_id("catalog-page")).to_have_attribute(
            "data-source", "published"
        )
        expect(page.get_by_test_id("version-chip")).to_have_text("v1")
        for table in (ProbeSql.RAW, ProbeSql.STG):
            ref = f"{stand_database}/public/{table}"
            expect(
                page.locator(f'[data-testid="catalog-node"][data-node="{ref}"]')
            ).to_be_visible(timeout=15_000)

        expect(edges.filter(has_text="2 cols")).to_have_count(1)
