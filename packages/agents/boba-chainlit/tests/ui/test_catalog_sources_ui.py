"""Страницы источников по DOM: список и форма нового источника, дерево любой
глубины с пометками изменений, родные карточки Postgres и ClickHouse, выбор
версии и diff, ручной источник: черновик, форма объекта, правка, удаление,
публикация; права читателя."""

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
    def test_list_shows_kind_manual_and_version(
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
            listing.locator(f'li[data-source="{SourceSeed.PLANNED}"]')
        ).to_contain_text("manual")
        expect(
            listing.locator(f'li[data-source="{SourceSeed.PLANNED}"]')
        ).to_contain_text("no versions")
        expect(page.get_by_test_id("new-source")).to_be_visible()

        page.goto(f"{stand.config.base_url}/catalog/")
        page.get_by_test_id("index-sources").get_by_role(
            "link", name="metadata sources"
        ).click()
        expect(page.get_by_test_id("sources-page")).to_be_visible()

    def test_new_source_form_creates_and_opens_the_source(
        self, tabs: Tabs, stand: StandProcess, catalog_api: Api, source_seed: SourceSeed
    ) -> None:
        page = tabs.page("admin")
        page.goto(f"{stand.config.base_url}/catalog/sources")
        form = page.get_by_test_id("new-source")
        expect(form.get_by_role("button", name="source")).to_be_disabled()
        form.get_by_label("source kind").select_option("clickhouse")
        form.get_by_label("source name").fill("src_new")
        form.get_by_label("source description").fill("made from the page")
        form.get_by_label("manual source").check()
        form.get_by_role("button", name="source").click()

        page.wait_for_url(
            re.compile(r"/catalog/sources/[0-9a-f-]{36}$"), timeout=30_000
        )
        expect(page.get_by_test_id("page-title")).to_have_text("src_new")
        expect(page.locator(".topbar")).to_contain_text("clickhouse")
        expect(page.locator(".topbar")).to_contain_text("manual")
        expect(page.get_by_text("no versions yet")).to_be_visible()
        names = {str(s["name"]) for s in catalog_api.sources()}
        assert "src_new" in names

    def test_reader_sees_the_list_without_the_form(
        self, tabs: Tabs, stand: StandProcess, catalog_api: Api, source_seed: SourceSeed
    ) -> None:
        page = tabs.page("dev")
        page.goto(f"{stand.config.base_url}/catalog/sources")
        expect(page.get_by_test_id("sources-page")).to_have_attribute(
            "data-can-edit", "false"
        )
        expect(page.get_by_test_id("new-source")).to_have_count(0)
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
        expect(card.locator(".detail__name")).to_have_text("orders")
        expect(card.locator(".detail__description--head")).to_have_text("Заказы")
        facts = card.get_by_test_id("card-facts")
        expect(facts).to_contain_text("partition key")
        expect(facts).to_contain_text("RANGE (created_at)")
        expect(facts).to_contain_text("rows")

        rows = card.get_by_test_id("card-columns").locator("tbody tr")
        expect(rows).to_have_count(4)
        amount = rows.filter(has_text="amount")
        expect(amount.locator(".detail__col-type")).to_have_text("numeric(12,2)")
        expect(amount.locator(".detail__col-null")).to_have_text("not null")
        expect(amount.locator(".detail__col-comment")).to_have_text("Сумма")
        expect(
            rows.filter(has_text="created_at").locator(".detail__col-extra")
        ).to_contain_text("default now()")
        expect(
            rows.filter(has_text="id").first.locator(".detail__icon svg")
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
        expect(card.locator(".detail__name")).to_have_text("load_orders(date)")
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
            rows.filter(has_text="payload").locator(".detail__col-extra")
        ).to_contain_text("codec ZSTD(3)")
        expect(
            rows.filter(has_text="ts").first.locator(".detail__icon svg")
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


def _new_draft(page: Page, stand: StandProcess, source_id: str, name: str) -> str:
    """Черновик ручного источника из шапки страницы; возвращает его id."""
    _open_source(page, stand, source_id)
    page.get_by_test_id("new-source-draft").click()
    prompt = page.locator('[data-dialog="source-draft-name"]')
    prompt.get_by_role("textbox").fill(name)
    prompt.get_by_role("button", name="save").click()
    page.wait_for_url(re.compile(r"/drafts/[0-9a-f-]{36}$"), timeout=30_000)

    return page.url.rsplit("/", 1)[1]


def _add_object(page: Page, path: list[str], columns: list[tuple[str, str]]) -> None:
    """Объект через форму: путь по ступеням, колонки именем и типом."""
    page.get_by_test_id("add-object").click()
    form = page.get_by_test_id("object-form")
    labels = ["object database", "object schema", "object name"]
    for label, step in zip(labels[-len(path) :], path, strict=True):
        form.get_by_label(label).fill(step)

    for name, kind in columns:
        form.get_by_role("button", name="column", exact=True).click()
        form.get_by_label("column name").last.fill(name)
        form.get_by_label("column type").last.fill(kind)

    form.get_by_role("button", name="save object").click()
    expect(form).to_have_count(0)


class TestManualSource:
    def test_draft_adds_and_edits_an_object(
        self, tabs: Tabs, stand: StandProcess, source_seed: SourceSeed
    ) -> None:
        page = tabs.page("admin")
        _open_source(page, stand, source_seed.planned)
        expect(page.get_by_text("no versions yet")).to_be_visible()
        _new_draft(page, stand, source_seed.planned, "first shapes")
        draft_page = page.get_by_test_id("source-draft-page")
        expect(draft_page).to_have_attribute("data-seq", "0")

        page.get_by_test_id("add-object").click()
        form = page.get_by_test_id("object-form")
        expect(form.get_by_role("button", name="save object")).to_be_disabled()
        form.get_by_label("object database").fill("planned")
        form.get_by_label("object schema").fill("dm")
        form.get_by_label("object name").fill("sales")
        form.get_by_label("object comment").fill("Витрина продаж")
        form.get_by_role("button", name="column", exact=True).click()
        form.get_by_label("column name").last.fill("day")
        form.get_by_label("column type").last.fill("date")
        form.get_by_label("nullable day").uncheck()
        form.get_by_role("button", name="column", exact=True).click()
        form.get_by_label("column name").last.fill("total")
        form.get_by_label("column type").last.fill("numeric(14,2)")
        form.get_by_label("column comment").last.fill("Сумма")
        form.get_by_role("button", name="save object").click()

        expect(draft_page).to_have_attribute("data-seq", "1", timeout=15_000)
        expect(form).to_have_count(0)
        card = page.get_by_test_id("object-card")
        expect(card).to_have_attribute("data-path", "planned/dm/sales")
        expect(card.locator(".detail__description--head")).to_have_text(
            "Витрина продаж"
        )
        rows = card.get_by_test_id("card-columns").locator("tbody tr")
        expect(rows).to_have_count(2)
        expect(rows.filter(has_text="day").locator(".detail__col-null")).to_have_text(
            "not null"
        )
        expect(
            rows.filter(has_text="total").locator(".detail__col-comment")
        ).to_have_text("Сумма")
        expect(_node(page, "planned")).to_have_attribute("data-status", "modified")
        _expand(page, "planned", "planned")
        _expand(page, "planned/dm", "dm")
        _expand(page, "planned/dm/tables", "tables")
        expect(_node(page, "planned/dm/tables/sales")).to_have_attribute(
            "data-status", "added"
        )

        page.get_by_role("button", name="edit object").click()
        form = page.get_by_test_id("object-form")
        expect(form.get_by_label("object name")).to_be_disabled()
        form.get_by_role("button", name="remove column total").click()
        form.get_by_label("object kind").select_option("view")
        form.get_by_role("button", name="save object").click()
        expect(draft_page).to_have_attribute("data-seq", "2", timeout=15_000)
        expect(card.get_by_test_id("card-columns").locator("tbody tr")).to_have_count(1)
        expect(card.locator(".detail__head")).to_contain_text("view")

    def test_draft_removes_an_object_and_publishes(
        self, tabs: Tabs, stand: StandProcess, catalog_api: Api, source_seed: SourceSeed
    ) -> None:
        page = tabs.page("admin")
        draft_id = _new_draft(page, stand, source_seed.planned, "publish me")
        draft_page = page.get_by_test_id("source-draft-page")
        card = page.get_by_test_id("object-card")

        _add_object(page, ["planned", "dm", "sales"], [("day", "date")])
        expect(draft_page).to_have_attribute("data-seq", "1", timeout=15_000)
        _add_object(page, ["planned", "dm", "customers"], [("id", "bigint")])
        expect(draft_page).to_have_attribute("data-seq", "2", timeout=15_000)
        expect(card).to_have_attribute("data-path", "planned/dm/customers")

        page.get_by_role("button", name="remove object").click()
        expect(draft_page).to_have_attribute("data-seq", "3", timeout=15_000)
        expect(page.get_by_test_id("object-card")).to_have_count(0)

        state = catalog_api.source_draft_state(draft_id)
        assert state["seq"] == 3
        assert [r["name"] for r in state["snapshot"]["relations"]] == ["sales"]

        page.get_by_test_id("publish-source-draft").click()
        page.wait_for_url(
            re.compile(r"/catalog/sources/[0-9a-f-]{36}\?v=\d+$"), timeout=30_000
        )
        expect(page.locator('.toast[data-tone="success"]')).to_contain_text(
            "published as v"
        )
        expect(page.locator('[data-notice="source-drafts"]')).to_have_count(0)
        _expand(page, "planned", "planned")
        _expand(page, "planned/dm", "dm")
        expect(_node(page, "planned/dm/tables")).to_contain_text("1")

    def test_discard_and_reader_restrictions(
        self, tabs: Tabs, stand: StandProcess, source_seed: SourceSeed
    ) -> None:
        admin = tabs.page("admin")
        _new_draft(admin, stand, source_seed.planned, "doomed")
        draft_url = admin.url

        admin.get_by_test_id("discard-source-draft").click()
        dialog = admin.locator('[data-dialog="source-draft-discard"]')
        dialog.get_by_role("button", name="keep editing").click()
        expect(dialog).to_have_count(0)

        reader = tabs.page("dev")
        reader.goto(draft_url)
        expect(reader.get_by_test_id("source-draft-page")).to_have_attribute(
            "data-editable", "false"
        )
        expect(reader.get_by_test_id("add-object")).to_have_count(0)
        expect(reader.get_by_test_id("publish-source-draft")).to_have_count(0)

        admin.get_by_test_id("discard-source-draft").click()
        dialog.get_by_role("button", name="discard the draft").click()
        expect(admin.get_by_test_id("source-page")).to_be_visible()
        expect(admin.locator('[data-notice="source-drafts"]')).not_to_contain_text(
            "doomed"
        )

        reader.reload()
        expect(reader.locator('[data-notice="source-draft-closed"]')).to_contain_text(
            "discarded"
        )
