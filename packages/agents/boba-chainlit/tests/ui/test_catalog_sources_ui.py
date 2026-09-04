"""Страницы источников по DOM: подключения и источники (форма подключения по
схеме api, пометка источником, отвязка, удаление), дерево любой глубины с
пометками изменений, родные карточки Postgres и ClickHouse, выбор версии и
diff; права читателя."""

from __future__ import annotations

import re
from collections.abc import Iterator

import pytest
from catalog_ui import Api, SourceSeed
from chat_ui import login_cookies
from playwright.sync_api import (
    Browser,
    BrowserContext,
    Locator,
    Page,
    ViewportSize,
    expect,
)

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


def _open_source(
    page: Page, stand: StandProcess, source_id: str, query: str = ""
) -> None:
    page.goto(f"{stand.config.base_url}/catalog/sources/{source_id}{query}")
    expect(page.get_by_test_id("source-page")).to_be_visible()


def _node(page: Page, path: str) -> Locator:
    return page.locator(f'{NODE}[data-path="{path}"]')


def _expand(page: Page, path: str, label: str) -> None:
    _node(page, path).get_by_role("button", name=f"expand {label}").click()
    expect(_node(page, path)).to_have_attribute("data-open", "true")


class TestSourcesList:
    def test_list_shows_kind_description_and_version(
        self, tabs: Tabs, stand: StandProcess, source_seed: SourceSeed
    ) -> None:
        page = tabs.page("admin")
        page.goto(f"{stand.config.base_url}/catalog/sources")
        listing = page.get_by_test_id("sources-list")
        expect(listing.locator(f'li[data-source="{SourceSeed.PROD}"]')).to_contain_text(
            "postgres"
        )
        expect(listing.locator(f'li[data-source="{SourceSeed.PROD}"]')).to_contain_text(
            "v2"
        )
        expect(listing.locator(f'li[data-source="{SourceSeed.PROD}"]')).to_contain_text(
            "Prod database"
        )
        expect(
            listing.locator(f'li[data-source="{SourceSeed.EMPTY}"]')
        ).to_contain_text("no versions")
        connections = page.get_by_test_id("connections-list")
        expect(
            connections.locator(
                f'li[data-connection="{Api.connection_name(SourceSeed.PROD)}"]'
            ).get_by_test_id("connection-source")
        ).to_have_text(SourceSeed.PROD)

        page.goto(f"{stand.config.base_url}/catalog/")
        expect(page.get_by_test_id("catalog-page")).to_be_visible(timeout=30_000)
        pane = page.get_by_test_id("left-pane")
        pane.get_by_role("tab", name="sources").click()
        expect(
            pane.locator(
                f'[data-testid="source-branch"][data-source="{SourceSeed.PROD}"]'
            )
        ).to_contain_text("v2")
        pane.get_by_test_id("sources-link").click()
        expect(page.get_by_test_id("sources-page")).to_be_visible()

    def test_connection_dialog_creates_checks_and_deletes(
        self, tabs: Tabs, stand: StandProcess, catalog_api: Api, source_seed: SourceSeed
    ) -> None:
        """Подключение заводится формой по схеме api: вид, поля профиля,
        проверка; строка появляется в списке без источника и удаляется."""
        page = tabs.page("admin")
        page.goto(f"{stand.config.base_url}/catalog/sources")
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
        expect(row.get_by_test_id("connection-source")).to_have_text("no source")
        expect(row).to_contain_text("web")
        # у вида без снимка нет кнопки «в источник»
        expect(row.get_by_role("button", name=re.compile("^assign"))).to_have_count(0)
        names = {str(c["name"]) for c in catalog_api.connections()}
        assert "src_page_web" in names

        row.get_by_role("button", name="delete src_page_web").click()
        page.locator('[data-dialog="connection-delete"]').get_by_test_id(
            "delete-connection"
        ).click()
        expect(row).to_have_count(0)

    def test_assign_puts_a_connection_into_an_existing_source(
        self, tabs: Tabs, stand: StandProcess, catalog_api: Api, source_seed: SourceSeed
    ) -> None:
        """Второе подключение того же вида помечается существующим источником
        через диалог, отвязывается кнопкой; привязанное подключение удалить
        нельзя."""
        replica = "src_prod_replica"
        catalog_api.stand_db.add_connection(replica, "postgres")
        try:
            page = tabs.page("admin")
            page.goto(f"{stand.config.base_url}/catalog/sources")
            row = page.locator(
                f'[data-testid="connections-list"] li[data-connection="{replica}"]'
            )
            expect(row.get_by_test_id("connection-source")).to_have_text("no source")
            row.get_by_role("button", name=f"assign {replica} to a source").click()
            dialog = page.locator('[data-dialog="assign-source"]')
            dialog.get_by_label("assign to source").select_option(label=SourceSeed.PROD)
            dialog.get_by_test_id("assign-submit").click()
            expect(dialog).to_have_count(0)
            expect(row.get_by_test_id("connection-source")).to_have_text(
                SourceSeed.PROD
            )
            prod = page.locator(
                f'[data-testid="sources-list"] li[data-source="{SourceSeed.PROD}"]'
            )
            expect(prod).to_contain_text("2 connections")

            # общее подключение (по роли) удалить нельзя: кнопки нет
            expect(row.get_by_role("button", name=f"delete {replica}")).to_have_count(0)

            row.get_by_role("button", name=f"unassign {replica}").click()
            expect(row.get_by_test_id("connection-source")).to_have_text("no source")
            expect(prod).to_contain_text("1 connection")
        finally:
            catalog_api.stand_db.remove_connections(replica)

    def test_reader_sees_the_lists_without_assignment(
        self, tabs: Tabs, stand: StandProcess, catalog_api: Api, source_seed: SourceSeed
    ) -> None:
        page = tabs.page("dev")
        page.goto(f"{stand.config.base_url}/catalog/sources")
        expect(page.get_by_test_id("sources-page")).to_have_attribute(
            "data-can-edit", "false"
        )
        expect(page.get_by_test_id("add-connection")).to_be_visible()
        expect(page.get_by_role("button", name=re.compile("^assign "))).to_have_count(0)
        expect(page.get_by_role("button", name=re.compile("^unassign "))).to_have_count(
            0
        )
        expect(page.get_by_test_id("sources-list").locator("li")).to_have_count(
            len(catalog_api.sources())
        )


class TestPostgresTree:
    def test_tree_expands_level_by_level_down_to_partitions(
        self, tabs: Tabs, stand: StandProcess, source_seed: SourceSeed
    ) -> None:
        page = tabs.page("admin")
        _open_source(page, stand, source_seed.prod)
        expect(page.get_by_test_id("source-page")).to_have_attribute(
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
        self, tabs: Tabs, stand: StandProcess, source_seed: SourceSeed
    ) -> None:
        page = tabs.page("admin")
        _open_source(page, stand, source_seed.prod)
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

        expect(card.get_by_test_id("card-constraints")).to_contain_text(
            "PRIMARY KEY (id, created_at)"
        )
        expect(card.get_by_test_id("card-indexes")).to_contain_text(
            "orders_created_idx"
        )
        expect(card.get_by_test_id("card-partitions")).to_contain_text("orders_2026")
        expect(_node(page, "prod/public/tables/orders")).to_have_attribute(
            "data-selected", "true"
        )
        assert "ref=" in page.url

    def test_routine_card_and_version_switch(
        self, tabs: Tabs, stand: StandProcess, source_seed: SourceSeed
    ) -> None:
        page = tabs.page("admin")
        _open_source(page, stand, source_seed.prod)
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

        page.get_by_label("source version").select_option("1")
        expect(page.get_by_test_id("source-page")).to_have_attribute(
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
        self, tabs: Tabs, stand: StandProcess, source_seed: SourceSeed
    ) -> None:
        page = tabs.page("admin")
        _open_source(page, stand, source_seed.prod)
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
        self, tabs: Tabs, stand: StandProcess, source_seed: SourceSeed
    ) -> None:
        page = tabs.page("dev")
        _open_source(page, stand, source_seed.dwh)
        expect(page.get_by_test_id("source-page")).to_have_attribute(
            "data-can-edit", "false"
        )
        expect(page.get_by_role("button", name="delete source")).to_have_count(0)
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
