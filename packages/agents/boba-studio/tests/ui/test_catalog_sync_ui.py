"""Синхронизация источника на песочном стенде: настоящий pg_schema_snapshot
снимает базу стенда через подключение main. Через API — полный проход,
вторая версия после CREATE TABLE с diff, третья после DROP TABLE; по DOM —
диалог подключений (привязка, отвязка), диалог синхронизации, полоса
прогресса и итога, отмена, дерево новой версии, права читателя."""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any, ClassVar, Final

import pytest
from catalog_ui import (
    Api,
    ApiPath,
    CatalogPage,
    Tabs,
    Viewport,
    api_client,
    settled_box,
)
from playwright.sync_api import Browser, Locator, Page, expect

from boba.stand.ui.database import StandDatabase
from boba.stand.ui.look import Tokens, no_horizontal_scroll
from boba.stand.ui.stand import (
    StandApp,
    StandConfig,
    StandProcess,
    free_port,
)
from boba.studio.catalog.api import CatalogUrl

pytestmark = pytest.mark.ui

BOOT_TIMEOUT_SEC = 240.0


class Probe:
    """Пробники модуля в базе стенда и имена источников; DDL — литералы,
    как их принимает psycopg."""

    PREFIX: ClassVar[str] = "src_sync"
    SOURCE: ClassVar[str] = "src_sync_stand"
    UI_SOURCE: ClassVar[str] = "src_sync_page"
    TABLE: ClassVar[str] = "sync_probe"
    COLUMN: ClassVar[str] = "probe_note"
    CONNECTION: ClassVar[str] = "main"
    CATALOG_SCHEMAS: ClassVar[tuple[str, ...]] = ("catalog", "automation")
    """Схемы самого каталога в базе стенда: домен и приложение (в нём на
    время синхронизации живут staging-таблицы)."""

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
        app=StandApp.STUDIO,
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


@pytest.fixture(scope="module")
def sync_api(sync_stand: StandProcess, stand_db: StandDatabase) -> Iterator[Api]:
    """JSON API от имени admin: на каждый сценарий в базу стенда кладётся
    своё подключение src_sync…, синхронизация идёт по нему."""
    with api_client(sync_stand, "admin") as admin:
        api = Api(admin, stand_db)
        api.cleanup_prefix(Probe.PREFIX)
        try:
            yield api
        finally:
            api.cleanup_prefix(Probe.PREFIX)


@pytest.fixture
def tabs(browser: Browser, sync_stand: StandProcess) -> Iterator[Tabs]:
    opened = Tabs(browser, sync_stand)
    try:
        yield opened
    finally:
        opened.close()


def _open_connection(page: Page, stand: StandProcess, connection_id: str) -> None:
    CatalogPage.CONNECTION.open(page, stand, connection_id=connection_id)
    expect(page.get_by_test_id("connection-page")).to_be_visible()


def _labels(nodes: list[dict[str, Any]]) -> list[str]:
    labels: list[str] = []
    for node in nodes:
        labels.append(str(node["label"]))

    return labels


class TestSyncApi:
    """Настоящий pg_schema_snapshot против базы стенда через API."""

    def test_full_sync_then_alter_and_drop(
        self, sync_api: Api, stand_db: StandDatabase, stand_database: str
    ) -> None:
        connection_id = sync_api.add_connection(Probe.SOURCE, "postgres")

        stand_db.ddl(Probe.CREATE)
        first = sync_api.sync(connection_id, ["public"])
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
        second = sync_api.sync(connection_id, ["public"])
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
        third = sync_api.sync(connection_id, ["public"])
        assert third["version"] == 3

        removed = [
            entry
            for entry in sync_api.diff(connection_id, 2, 3)
            if entry["status"] == "removed"
        ]
        assert [entry["ref"]["path"][-1] for entry in removed] == [Probe.TABLE]

        listed = sync_api.syncs(connection_id)
        assert [item["version"] for item in listed] == [3, 2, 1]
        synced = {item["name"]: item for item in sync_api.synced()}
        assert synced[Probe.SOURCE]["latest_version"] == 3

    def test_repeated_sync_of_an_unchanged_database_has_no_diff(
        self, sync_api: Api, stand_database: str
    ) -> None:
        """Две синхронизации подряд без изменений в базе: версии равны, хотя
        таблицы самого каталога между ними выросли — число строк и размер не
        считаются изменением структуры. Схема каталога исключена: в ней на
        время синхронизации живёт её же staging-таблица."""
        connection_id = sync_api.add_connection(f"{Probe.SOURCE}_twice", "postgres")

        probe = sync_api.sync(connection_id, [])
        assert probe["version"] == 1
        schemas: list[str] = []
        for node in sync_api.tree(connection_id, 1, [stand_database]):
            if node["label"] not in Probe.CATALOG_SCHEMAS:
                schemas.append(str(node["label"]))

        assert not set(Probe.CATALOG_SCHEMAS) & set(schemas)
        assert len(schemas) > 1

        second = sync_api.sync(connection_id, schemas)
        third = sync_api.sync(connection_id, schemas)
        assert second["version"] == 2
        assert third["version"] == 3

        assert sync_api.diff(connection_id, 2, 3) == []
        # дерево третьей версии — те же схемы, что во второй
        roots = sync_api.tree(connection_id, 3, [])
        assert [node["label"] for node in roots] == [stand_database]
        third_schemas = [
            str(node["label"])
            for node in sync_api.tree(connection_id, 3, [stand_database])
        ]
        assert third_schemas == schemas

    def test_reader_cannot_sync(self, sync_stand: StandProcess, sync_api: Api) -> None:
        connection_id = sync_api.add_connection(f"{Probe.SOURCE}_reader", "postgres")

        with api_client(sync_stand, "dev") as reader:
            url = ApiPath.catalog(
                CatalogUrl.CONNECTION_SYNCS, connection_id=connection_id
            )
            response = reader.post(url, json={})
            assert response.status_code == 403

            syncs = reader.get(url)
            assert syncs.status_code == 200


class TestSyncPage:
    """Страница подключения: диалог синхронизации, прогресс, отмена."""

    def test_sync_from_the_list_fills_the_connection_page(
        self,
        tabs: Tabs,
        sync_stand: StandProcess,
        sync_api: Api,
        stand_database: str,
    ) -> None:
        """Кнопка sync в строке списка открывает диалог, старт ведёт на
        страницу подключения, ход виден полосой, первая версия даёт дерево
        базы; в списке у подключения появляется чип версии."""
        name = Probe.UI_SOURCE
        sync_api.add_connection(name, "postgres")
        page = tabs.page("admin")
        CatalogPage.CONNECTIONS.open(page, sync_stand)
        expect(page.get_by_test_id("connections-page")).to_be_visible()

        row = page.locator(
            f'[data-testid="connections-list"] li[data-connection="{name}"]'
        )
        expect(row.get_by_test_id("connection-version")).to_have_text("not synced")
        row.get_by_role("button", name=f"sync {name}").click()
        sync_dialog = page.locator('[data-dialog="connection-sync"]')
        sync_dialog.get_by_label("sync schemas").fill("public")
        sync_dialog.get_by_test_id("start-sync").click()

        page.wait_for_url(
            re.compile(r"/catalog/connections/[0-9a-f-]{36}$"), timeout=30_000
        )
        expect(page.get_by_test_id("page-title")).to_have_text(name)
        expect(page.locator(".topbar")).to_contain_text("postgres")
        progress = page.get_by_test_id("sync-progress")
        expect(progress).to_be_visible()
        expect(progress).to_have_attribute(
            "data-status", "done", timeout=Api.SYNC_TIMEOUT_SEC * 1000
        )
        expect(progress).to_contain_text("synced v1")
        expect(page.get_by_label("snapshot version")).to_have_value("1")
        expect(
            page.locator(f'[data-testid="tree-node"][data-path="{stand_database}"]')
        ).to_be_visible()
        expect(page.get_by_test_id("connection-sync")).to_be_enabled()

        CatalogPage.CONNECTIONS.open(page, sync_stand)
        expect(row.get_by_test_id("connection-version")).to_have_text("v1")

    def test_cancel_stops_a_slow_sync(
        self, tabs: Tabs, sync_stand: StandProcess, sync_api: Api
    ) -> None:
        connection_id = sync_api.add_connection(f"{Probe.UI_SOURCE}_slow", "postgres")
        page = tabs.page("admin")
        _open_connection(page, sync_stand, connection_id)

        page.get_by_test_id("connection-sync").click()
        sync_dialog = page.locator('[data-dialog="connection-sync"]')
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
        self, tabs: Tabs, sync_stand: StandProcess, sync_api: Api
    ) -> None:
        connection_id = sync_api.add_connection(f"{Probe.UI_SOURCE}_reader", "postgres")
        sync_api.sync(connection_id, ["public"])
        page = tabs.page("dev")
        _open_connection(page, sync_stand, connection_id)

        expect(page.get_by_test_id("connection-sync")).to_have_count(0)
        expect(page.get_by_test_id("forget-versions")).to_have_count(0)
        expect(page.get_by_test_id("cancel-sync")).to_have_count(0)
        expect(page.get_by_label("snapshot version")).to_have_value("1")


@pytest.fixture(scope="module")
def tokens() -> Tokens:
    return Tokens.load()


class TestSyncLook:
    """Вид виджетов синхронизации: диалог, полоса итога по токенам, узкий
    экран без горизонтальной прокрутки."""

    def test_dialog_and_status_bar_follow_tokens(
        self, tabs: Tabs, sync_stand: StandProcess, sync_api: Api, tokens: Tokens
    ) -> None:
        connection_id = sync_api.add_connection(f"{Probe.UI_SOURCE}_look", "postgres")
        sync_api.sync(connection_id, ["public"])

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
        expect(sync_dialog.get_by_label("sync schemas")).to_be_visible()
        expect(sync_dialog.get_by_test_id("start-sync")).to_be_enabled()

    def test_narrow_screen_keeps_the_page_without_horizontal_scroll(
        self, tabs: Tabs, sync_stand: StandProcess, sync_api: Api
    ) -> None:
        connection_id = sync_api.add_connection(f"{Probe.UI_SOURCE}_narrow", "postgres")
        sync_api.sync(connection_id, ["public"])

        page = tabs.page("admin", Viewport.NARROW)
        _open_connection(page, sync_stand, connection_id)
        expect(page.locator('[data-notice="sync-status"]')).to_be_visible()
        assert no_horizontal_scroll(page)

        page.get_by_test_id("connection-sync").click()
        dialog = page.locator('[data-dialog="connection-sync"] [role="dialog"]')
        expect(dialog).to_be_visible()
        box = dialog.bounding_box()
        assert box is not None
        assert box["x"] >= 0
        assert box["x"] + box["width"] <= Viewport.NARROW["width"]
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
        CatalogPage.HOME.open(page, self.stand)
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
        sync_api: Api,
        stand_db: StandDatabase,
        stand_database: str,
    ) -> None:
        stand_db.ddl(ProbeSql.DROP_STG)
        stand_db.ddl(ProbeSql.DROP_RAW)
        stand_db.ddl(ProbeSql.CREATE_RAW)
        stand_db.ddl(ProbeSql.CREATE_STG)
        connection = f"{Probe.SOURCE}_e2e"
        connection_id = sync_api.add_connection(connection, "postgres")
        sync_api.sync(connection_id, ["public"])

        user = flow_user
        page = user.page
        user.new_process_and_draft(ProbeSql.PROCESS)
        branch = user.open_tables(connection)

        # обе таблицы встают на холст кнопкой панели объекта
        user.add_table(branch, ProbeSql.RAW, (60, 80))
        user.add_table(branch, ProbeSql.STG, (400, 80))
        # панель узла после броска не открывается: сцена не сжимается
        expect(page.get_by_test_id("detail-panel")).to_have_count(0)

        # линии между колонками: id → id заводит поток, amount → amount добавляет
        # пару; описания у потока нет — нет и ярлыка, считаем линии
        edges = page.locator(".react-flow__edge")
        user.connect(ProbeSql.RAW, ProbeSql.STG, "id")
        expect(edges).to_have_count(1)
        page.get_by_role("tab", name="all fields").click()
        user.connect(ProbeSql.RAW, ProbeSql.STG, "amount")
        expect(edges).to_have_count(2)
        expect(page.locator('[data-testid="flow-edge-label"]')).to_have_count(0)
        expect(page.get_by_test_id("flow-form")).to_have_count(0)

        page.get_by_test_id("left-pane").get_by_role("tab", name="process").click()
        page.get_by_test_id("publish-button").click()

        # публикация создала процесс и увела на него: те же таблицы и поток
        page.wait_for_url(
            re.compile(r"/catalog/processes/[0-9a-f-]{36}$"), timeout=30_000
        )
        process_id = sync_api.process_id_of(ProbeSql.PROCESS)
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

        expect(edges).to_have_count(2)
