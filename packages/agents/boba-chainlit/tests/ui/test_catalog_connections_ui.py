"""Страницы подключений по DOM: список с версией снимка и кнопками check,
sync, edit, delete (форма подключения по схеме api), страница подключения с
деревом любой глубины и пометками изменений, родными карточками Postgres и
ClickHouse, выбором версии, diff и «forget versions»; права читателя."""

from __future__ import annotations

import re
from collections.abc import Iterator

import pytest
from catalog_ui import Api, ConnectionSeed
from chat_ui import login_cookies
from playwright.sync_api import (
    Browser,
    BrowserContext,
    Locator,
    Page,
    ViewportSize,
    expect,
)

from boba.db.postgres.snapshot_sample import PgSample
from boba.stand.ui.look import no_horizontal_scroll
from boba.stand.ui.stand import StandProcess

pytestmark = pytest.mark.ui

WIDE: ViewportSize = {"width": 1400, "height": 900}
NODE = '[data-testid="tree-node"]'


class Tabs:
    def __init__(self, browser: Browser, stand: StandProcess) -> None:
        self.browser = browser
        self.stand = stand
        self.contexts: list[BrowserContext] = []

    def page(self, login: str) -> Page:
        context = self.browser.new_context(viewport=WIDE)
        context.add_cookies(login_cookies(self.stand, login))
        self.contexts.append(context)
        return context.new_page()

    def close(self) -> None:
        for context in self.contexts:
            context.close()

        self.contexts.clear()


@pytest.fixture
def tabs(browser: Browser, stand: StandProcess) -> Iterator[Tabs]:
    opened = Tabs(browser, stand)
    try:
        yield opened
    finally:
        opened.close()


def _open_connection(
    page: Page, stand: StandProcess, connection_id: str, query: str = ""
) -> None:
    page.goto(f"{stand.config.base_url}/catalog/connections/{connection_id}{query}")
    expect(page.get_by_test_id("connection-page")).to_be_visible()


def _node(page: Page, path: str) -> Locator:
    return page.locator(f'{NODE}[data-path="{path}"]')


def _expand(page: Page, path: str, label: str) -> None:
    _node(page, path).get_by_role("button", name=f"expand {label}").click()
    expect(_node(page, path)).to_have_attribute("data-open", "true")


class TestConnectionsList:
    def test_list_shows_kind_and_snapshot_version(
        self, tabs: Tabs, stand: StandProcess, connection_seed: ConnectionSeed
    ) -> None:
        page = tabs.page("admin")
        page.goto(f"{stand.config.base_url}/catalog/connections")
        listing = page.get_by_test_id("connections-list")
        prod = listing.locator(f'li[data-connection="{ConnectionSeed.PROD}"]')
        expect(prod).to_contain_text("postgres")
        expect(prod.get_by_test_id("connection-version")).to_have_text("v2")
        expect(prod).to_have_attribute("data-synced", "true")
        empty = listing.locator(f'li[data-connection="{ConnectionSeed.EMPTY}"]')
        expect(empty.get_by_test_id("connection-version")).to_have_text("not synced")
        expect(empty).to_have_attribute("data-synced", "false")
        # кнопки строки общего подключения: check и sync; edit и delete — у
        # владельца, а про источники ничего нет
        for action in ("check", "sync"):
            expect(
                prod.get_by_role("button", name=f"{action} {ConnectionSeed.PROD}")
            ).to_be_visible()

        expect(prod).to_contain_text("shared")
        expect(page.get_by_role("button", name=re.compile("^assign "))).to_have_count(0)
        expect(page.get_by_test_id("sources-list")).to_have_count(0)

        prod.get_by_role("link", name=ConnectionSeed.PROD).click()
        expect(page.get_by_test_id("connection-page")).to_have_attribute(
            "data-connection", ConnectionSeed.PROD
        )

    def test_connection_dialog_creates_checks_and_deletes(
        self,
        tabs: Tabs,
        stand: StandProcess,
        catalog_api: Api,
        connection_seed: ConnectionSeed,
    ) -> None:
        """Подключение заводится формой по схеме api: вид, поля профиля,
        проверка; строка появляется в списке и удаляется. У вида без снимка
        нет кнопки sync и чипа версии."""
        page = tabs.page("admin")
        page.goto(f"{stand.config.base_url}/catalog/connections")
        page.get_by_test_id("add-connection").click()
        form = page.get_by_test_id("connection-form")
        expect(form.get_by_test_id("save-connection")).to_be_disabled()
        form.get_by_label("connection name").fill("src_page_web")
        form.get_by_label("profile.kind").select_option("web")
        form.get_by_label("profile.base_url").fill(stand.config.base_url)
        form.get_by_test_id("check-connection").click()
        expect(form.locator('[data-notice="probe"]')).to_be_visible(timeout=30_000)
        form.get_by_test_id("save-connection").click()
        expect(form).to_have_count(0)

        row = page.locator(
            '[data-testid="connections-list"] li[data-connection="src_page_web"]'
        )
        expect(row).to_be_visible()
        expect(row).to_contain_text("web")
        expect(row.get_by_test_id("connection-version")).to_have_count(0)
        expect(row.get_by_role("button", name="sync src_page_web")).to_have_count(0)
        names = {str(c["name"]) for c in catalog_api.connections()}
        assert "src_page_web" in names

        row.get_by_role("button", name="delete src_page_web").click()
        page.locator('[data-dialog="connection-delete"]').get_by_test_id(
            "delete-connection"
        ).click()
        expect(row).to_have_count(0)

    def test_forget_versions_clears_the_snapshot_of_a_connection(
        self,
        tabs: Tabs,
        stand: StandProcess,
        catalog_api: Api,
        connection_seed: ConnectionSeed,
    ) -> None:
        """Версия снимка у пустого подключения: в списке чип версии, на его
        странице «forget versions» убирает версии, список снова «not synced»."""
        snapshot = PgSample().snapshot().model_dump(mode="json")
        catalog_api.write_connection_version(connection_seed.empty, snapshot)
        page = tabs.page("admin")
        page.goto(f"{stand.config.base_url}/catalog/connections")
        listing = page.get_by_test_id("connections-list")
        row = listing.locator(f'li[data-connection="{ConnectionSeed.EMPTY}"]')
        expect(row.get_by_test_id("connection-version")).to_have_text("v1")

        _open_connection(page, stand, connection_seed.empty)
        expect(page.locator(".topbar__hint")).to_have_text("1 version(s)")
        page.get_by_test_id("forget-versions").click()
        page.locator('[data-dialog="forget-versions"]').get_by_test_id(
            "forget-versions-confirm"
        ).click()
        expect(page.locator(".topbar__hint")).to_have_text("not synced yet")
        expect(page.get_by_test_id("forget-versions")).to_have_count(0)
        assert all(
            s["connection_id"] != connection_seed.empty for s in catalog_api.synced()
        )

        page.goto(f"{stand.config.base_url}/catalog/connections")
        expect(row.get_by_test_id("connection-version")).to_have_text("not synced")

    def test_reader_sees_the_list_without_sync_and_edit(
        self,
        tabs: Tabs,
        stand: StandProcess,
        catalog_api: Api,
        connection_seed: ConnectionSeed,
    ) -> None:
        page = tabs.page("dev")
        page.goto(f"{stand.config.base_url}/catalog/connections")
        expect(page.get_by_test_id("connections-page")).to_have_attribute(
            "data-can-edit", "false"
        )
        expect(page.get_by_test_id("add-connection")).to_be_visible()
        expect(page.get_by_role("button", name=re.compile("^sync "))).to_have_count(0)
        expect(
            page.get_by_role("button", name=f"check {ConnectionSeed.PROD}")
        ).to_be_visible()


class TestPostgresTree:
    def test_tree_expands_level_by_level_down_to_partitions(
        self, tabs: Tabs, stand: StandProcess, connection_seed: ConnectionSeed
    ) -> None:
        page = tabs.page("admin")
        _open_connection(page, stand, connection_seed.prod)
        expect(page.get_by_test_id("connection-page")).to_have_attribute(
            "data-version", "2"
        )

        prod = _node(page, "prod")
        expect(prod).to_have_attribute("data-kind", "database")
        expect(prod).to_have_attribute("data-status", "modified")
        _expand(page, "prod", "prod")

        expect(_node(page, "prod/etl")).to_have_attribute("data-status", "modified")
        expect(_node(page, "prod/public")).to_have_attribute("data-status", "modified")
        _expand(page, "prod/public", "public")

        groups = page.locator(f'{NODE}[data-kind="group"]')
        expect(groups).to_have_count(4)
        expect(_node(page, "prod/public/tables")).to_contain_text("2")
        _expand(page, "prod/public/tables", "tables")

        expect(_node(page, "prod/public/tables/orders")).to_have_attribute(
            "data-status", "modified"
        )
        expect(_node(page, "prod/public/tables/returns")).to_have_attribute(
            "data-status", "added"
        )
        expect(_node(page, "prod/public/tables/orders")).to_contain_text("partitioned")
        _expand(page, "prod/public/tables/orders", "orders")
        expect(_node(page, "prod/public/tables/orders/orders_2026")).to_be_visible()
        expect(_node(page, "prod/public/tables/orders/orders_2026")).to_contain_text(
            "FOR VALUES"
        )

        _expand(page, "prod/etl", "etl")
        _expand(page, "prod/etl/functions", "functions")
        expect(_node(page, "prod/etl/functions/hash_key(text)")).to_be_visible()
        expect(_node(page, "prod/etl/functions/hash_key(text, text)")).to_be_visible()
        assert no_horizontal_scroll(page)

    def test_relation_card_shows_native_fields(
        self, tabs: Tabs, stand: StandProcess, connection_seed: ConnectionSeed
    ) -> None:
        page = tabs.page("admin")
        _open_connection(page, stand, connection_seed.prod)
        _expand(page, "prod", "prod")
        _expand(page, "prod/public", "public")
        _expand(page, "prod/public/tables", "tables")
        _node(page, "prod/public/tables/orders").get_by_role(
            "button", name="orders partitioned"
        ).click()

        card = page.get_by_test_id("object-card")
        expect(card).to_have_attribute("data-card", "pg_relation")
        expect(card.get_by_test_id("panel-name")).to_have_text("orders")
        expect(card.get_by_test_id("card-comment")).to_have_text("Заказы")
        facts = card.get_by_test_id("card-facts")
        expect(facts).to_contain_text("partition key")
        expect(facts).to_contain_text("RANGE (created_at)")
        expect(facts).to_contain_text("rows")

        rows = card.get_by_test_id("card-columns").locator("tbody tr")
        expect(rows).to_have_count(4)
        amount = rows.filter(has_text="amount")
        expect(amount.locator('[data-col="type"]')).to_have_text("numeric(12,2)")
        expect(amount.locator('[data-col="null"]')).to_have_text("not null")
        expect(amount.locator('[data-col="comment"]')).to_have_text("Сумма")
        expect(
            rows.filter(has_text="created_at").locator('[data-col="extra"]')
        ).to_contain_text("default now()")
        expect(
            rows.filter(has_text="id").first.locator("td.table__icon svg")
        ).to_have_count(1)

        primary = card.get_by_test_id("card-constraints").locator(
            'tr[data-kind="primary"]'
        )
        expect(primary).to_contain_text("primary")
        expect(primary.locator('[data-col="detail"]')).to_have_text("(id, created_at)")
        created_idx = card.get_by_test_id("card-indexes").locator(
            'tr[data-index="orders_created_idx"]'
        )
        expect(created_idx).to_contain_text("btree")
        expect(created_idx.locator('[data-col="detail"]')).to_contain_text(
            "(created_at)"
        )
        expect(card.get_by_test_id("card-partitions")).to_contain_text("orders_2026")
        expect(_node(page, "prod/public/tables/orders")).to_have_attribute(
            "data-selected", "true"
        )
        assert "ref=" in page.url

    def test_routine_card_and_version_switch(
        self, tabs: Tabs, stand: StandProcess, connection_seed: ConnectionSeed
    ) -> None:
        page = tabs.page("admin")
        _open_connection(page, stand, connection_seed.prod)
        _expand(page, "prod", "prod")
        _expand(page, "prod/etl", "etl")
        _expand(page, "prod/etl/procedures", "procedures")
        _node(page, "prod/etl/procedures/load_orders(date)").get_by_role(
            "button"
        ).click()

        card = page.get_by_test_id("object-card")
        expect(card).to_have_attribute("data-card", "pg_routine")
        expect(card.get_by_test_id("panel-name")).to_have_text("load_orders(date)")
        expect(card.get_by_test_id("card-facts")).to_contain_text("plpgsql")
        expect(card.get_by_test_id("card-arguments").locator("tbody tr")).to_have_count(
            1
        )
        expect(card.get_by_test_id("card-body")).to_contain_text("load_orders_v2")

        page.get_by_label("snapshot version").select_option("1")
        expect(page.get_by_test_id("connection-page")).to_have_attribute(
            "data-version", "1"
        )
        expect(card.get_by_test_id("card-body")).to_contain_text(
            "INSERT INTO public.orders"
        )
        expect(page.locator(f'{NODE}[data-status="modified"]')).to_have_count(0)
        expect(page.get_by_role("button", name=re.compile("diff with"))).to_have_count(
            0
        )

    def test_diff_panel_lists_changes_of_the_version(
        self, tabs: Tabs, stand: StandProcess, connection_seed: ConnectionSeed
    ) -> None:
        page = tabs.page("admin")
        _open_connection(page, stand, connection_seed.prod)
        page.get_by_role("button", name="diff with v1").click()

        diff = page.get_by_test_id("source-diff")
        expect(diff).to_have_attribute("data-entries", "4")
        expect(
            diff.locator(
                '[data-testid="diff-entry"][data-path="prod/public/customers"]'
            )
        ).to_have_attribute("data-status", "removed")
        orders = diff.locator(
            '[data-testid="diff-entry"][data-path="prod/public/orders"]'
        )
        expect(orders).to_have_attribute("data-status", "modified")
        expect(
            orders.locator('[data-part="column"][data-name="amount"]')
        ).to_contain_text("numeric(10,2) → numeric(12,2)")
        expect(
            orders.locator('[data-part="column"][data-name="note"]')
        ).to_have_attribute("data-status", "added")
        procedure = diff.locator(
            '[data-testid="diff-entry"][data-path="prod/etl/load_orders/date"]'
        )
        expect(procedure.locator('[data-field="body"]')).to_be_visible()
        assert "mode=diff" in page.url

        page.get_by_role("button", name="diff with v1").click()
        expect(diff).to_have_count(0)


class TestClickHouseTree:
    def test_table_and_dictionary_cards(
        self, tabs: Tabs, stand: StandProcess, connection_seed: ConnectionSeed
    ) -> None:
        page = tabs.page("dev")
        _open_connection(page, stand, connection_seed.dwh)
        expect(page.get_by_test_id("connection-page")).to_have_attribute(
            "data-can-edit", "false"
        )
        expect(page.get_by_test_id("forget-versions")).to_have_count(0)
        expect(page.get_by_test_id("connection-sync")).to_have_count(0)
        _expand(page, "dwh", "dwh")
        expect(page.locator(f'{NODE}[data-kind="group"]')).to_have_count(4)
        _expand(page, "dwh/tables", "tables")
        _node(page, "dwh/tables/events").get_by_role("button").click()

        card = page.get_by_test_id("object-card")
        expect(card).to_have_attribute("data-card", "ch_table")
        facts = card.get_by_test_id("card-facts")
        expect(facts).to_contain_text("MergeTree ORDER BY (ts, user_id)")
        expect(facts).to_contain_text("toYYYYMM(ts)")
        rows = card.get_by_test_id("card-columns").locator("tbody tr")
        expect(rows).to_have_count(3)
        expect(
            rows.filter(has_text="payload").locator('[data-col="extra"]')
        ).to_contain_text("codec ZSTD(3)")
        expect(
            rows.filter(has_text="ts").first.locator("td.table__icon svg")
        ).to_have_count(1)
        expect(card.get_by_test_id("card-create-query")).to_contain_text(
            "CREATE TABLE dwh.events"
        )

        _expand(page, "dwh/dictionaries", "dictionaries")
        _node(page, "dwh/dictionaries/users").get_by_role("button").click()
        expect(card).to_have_attribute("data-card", "ch_dictionary")
        expect(card.get_by_test_id("card-facts")).to_contain_text("Hashed")
        expect(
            card.get_by_test_id("card-attributes").locator("tbody tr")
        ).to_have_count(1)
