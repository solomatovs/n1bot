"""Виды каталога на странице: создание из индекса, фильтр по слоям и наборам,
сохранение раскладки перетаскиванием, шаринг роли и просмотр расшаренного вида
учёткой без прав на каталог, удаление вида.

Учётки стенда: admin (ADM) правит каталог и владеет видами, dev (DEV) читает
каталог, guest (GST) видит только то, что ему расшарили.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator

import pytest
from catalog_ui import Api, Ed, Seed, Selector
from chat_ui import login_cookies
from playwright.sync_api import (
    Browser,
    BrowserContext,
    Locator,
    Page,
    ViewportSize,
    expect,
)

from boba.stand.ui.stand import StandProcess

pytestmark = pytest.mark.ui

WIDE: ViewportSize = {"width": 1400, "height": 900}
LIVE_TIMEOUT_MS = 15_000
TRANSLATE = re.compile(r"translate\(\s*(-?[\d.]+)px,\s*(-?[\d.]+)px\)")


class Browsers:
    """Вкладки под разными учётками стенда; закрываются разом."""

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
def browsers(browser: Browser, stand: StandProcess) -> Iterator[Browsers]:
    opened = Browsers(browser, stand)
    try:
        yield opened
    finally:
        opened.close()


def _open_index(page: Page, stand: StandProcess) -> None:
    page.goto(f"{stand.config.base_url}/catalog/")
    expect(page.get_by_test_id("index-page")).to_be_visible()


def _open_view(page: Page, stand: StandProcess, view_id: str) -> None:
    page.goto(f"{stand.config.base_url}/catalog/views/{view_id}")
    page.wait_for_selector(Selector.READY, timeout=30_000)


def _create_view(page: Page, stand: StandProcess, name: str) -> str:
    """Вид из формы индекса; адрес страницы отдаёт его id."""
    _open_index(page, stand)
    form = page.get_by_test_id("new-view")
    form.get_by_role("textbox").fill(name)
    form.get_by_role("button", name="view").click()
    page.wait_for_url(re.compile(r"/catalog/views/[0-9a-f-]{36}$"), timeout=30_000)
    page.wait_for_selector(Selector.READY, timeout=30_000)

    return page.url.rsplit("/", 1)[1]


def _relayout(page: Page, action: Callable[[], None]) -> None:
    """Действие меняет раскладку: ждём следующую раскладку, а не прежнюю готовность."""
    canvas = page.get_by_test_id("canvas")
    before = canvas.get_attribute("data-layouts") or "0"
    action()
    expect(canvas).not_to_have_attribute("data-layouts", before, timeout=30_000)
    page.wait_for_selector(Selector.READY, timeout=30_000)


def _save_view_form(page: Page) -> None:
    form = page.get_by_test_id("view-form")

    def save() -> None:
        form.get_by_role("button", name="save view").click()
        expect(form).to_have_count(0)

    _relayout(page, save)


def _restrict_to_layers(page: Page, layers: list[str]) -> None:
    """Через форму вида оставить только перечисленные слои."""
    page.get_by_role("button", name="edit view").click()
    form = page.get_by_test_id("view-form")
    choices = form.get_by_test_id("view-layers")
    expect(choices).to_be_visible()
    for layer in layers:
        choices.get_by_label(layer, exact=True).check()

    _save_view_form(page)


def _node(page: Page, name: str) -> Locator:
    return page.locator(f'{Selector.NODE}[data-dataset="{name}"]')


def _translate_of(page: Page, dataset_id: str) -> tuple[float, float]:
    """Позиция узла в координатах холста из transform обёртки React Flow."""
    wrapper = page.locator(f'.react-flow__node[data-id="{dataset_id}"]')
    style = wrapper.get_attribute("style") or ""
    found = TRANSLATE.search(style)
    if found is None:
        raise AssertionError(f"node {dataset_id} has no translate: {style!r}")

    return float(found.group(1)), float(found.group(2))


class TestIndex:
    def test_forms_follow_the_rights(
        self, browsers: Browsers, stand: StandProcess, catalog_seed: Seed
    ) -> None:
        admin = browsers.page("admin")
        _open_index(admin, stand)
        expect(admin.get_by_test_id("index-page")).to_have_attribute(
            "data-can-edit", "true"
        )
        expect(admin.get_by_test_id("new-view")).to_be_visible()
        expect(admin.get_by_test_id("new-draft")).to_be_visible()

        dev = browsers.page("dev")
        _open_index(dev, stand)
        expect(dev.get_by_test_id("index-page")).to_have_attribute(
            "data-can-edit", "false"
        )
        expect(dev.get_by_test_id("new-view")).to_have_count(0)
        expect(dev.get_by_test_id("index-drafts")).to_be_visible()

        guest = browsers.page("guest")
        _open_index(guest, stand)
        expect(guest.get_by_test_id("new-view")).to_have_count(0)
        expect(guest.get_by_test_id("index-drafts")).to_have_count(0)
        expect(guest.get_by_test_id("index-views")).to_contain_text("no views yet")


class TestViewFilter:
    def test_layers_then_datasets_narrow_the_diagram(
        self,
        browsers: Browsers,
        stand: StandProcess,
        catalog_api: Api,
        catalog_seed: Seed,
    ) -> None:
        page = browsers.page("admin")
        view_id = _create_view(page, stand, "ed_filter")
        expect(page.get_by_test_id("page-title")).to_have_text("ed_filter")
        expect(page.get_by_test_id("catalog-page")).to_have_attribute(
            "data-owned", "true"
        )
        all_nodes = page.locator(Selector.NODE).count()
        assert all_nodes >= len(Seed.DATASETS)

        _restrict_to_layers(page, [Ed.SRC, Ed.DST])
        expect(page.locator(Selector.NODE)).to_have_count(len(Seed.DATASETS))
        expect(page.locator(Selector.LANE)).to_have_count(2)
        expect(page.locator(Selector.EDGE_LABEL)).to_have_count(1)

        page.get_by_role("button", name="edit view").click()
        form = page.get_by_test_id("view-form")
        layers = form.get_by_test_id("view-layers")
        layers.get_by_label(Ed.SRC, exact=True).uncheck()
        layers.get_by_label(Ed.DST, exact=True).uncheck()
        datasets = form.get_by_test_id("view-datasets")
        datasets.get_by_label(f"{Ed.DST} / {Ed.RETURNS}", exact=True).check()
        form.get_by_label("view name").fill("ed_returns_only")
        _save_view_form(page)

        expect(page.get_by_test_id("page-title")).to_have_text("ed_returns_only")
        expect(page.locator(Selector.NODE)).to_have_count(1)
        expect(_node(page, Ed.RETURNS)).to_be_visible()
        expect(page.locator(Selector.LANE)).to_have_count(1)
        expect(page.locator(Selector.EDGE_LABEL)).to_have_count(0)

        stored = next(view for view in catalog_api.views() if view["id"] == view_id)
        assert stored["layer_ids"] == []
        assert stored["dataset_ids"] == [catalog_seed.id_of(Ed.RETURNS)]


class TestLayout:
    def test_dragging_a_node_saves_the_layout_and_survives_reload(
        self,
        browsers: Browsers,
        stand: StandProcess,
        catalog_api: Api,
        catalog_seed: Seed,
    ) -> None:
        page = browsers.page("admin")
        view_id = _create_view(page, stand, "ed_layout")
        _restrict_to_layers(page, [Ed.SRC, Ed.DST])
        expect(page.locator(Selector.NODE)).to_have_count(len(Seed.DATASETS))

        orders_id = catalog_seed.id_of(Ed.ORDERS)
        before = _translate_of(page, orders_id)
        node = _node(page, Ed.ORDERS)
        box = node.bounding_box()
        assert box is not None

        page.mouse.move(box["x"] + box["width"] / 2, box["y"] + 12)
        page.mouse.down()
        page.mouse.move(box["x"] + box["width"] / 2 + 160, box["y"] + 12 + 90, steps=10)
        page.mouse.up()

        expect(page.get_by_test_id("catalog-page")).to_have_attribute(
            "data-layout-saves", "1", timeout=LIVE_TIMEOUT_MS
        )
        moved = _translate_of(page, orders_id)
        assert moved != before, "the node did not move"

        saved = catalog_api.layout(view_id)
        assert set(saved) == {catalog_seed.id_of(name) for name in Seed.DATASETS}
        assert saved[orders_id] == pytest.approx(moved, abs=1.0)

        page.reload()
        page.wait_for_selector(Selector.READY, timeout=30_000)
        assert _translate_of(page, orders_id) == pytest.approx(moved, abs=1.0)

        reader = browsers.page("dev")
        _open_view(reader, stand, view_id)
        expect(reader.get_by_test_id("catalog-page")).to_have_attribute(
            "data-owned", "false"
        )
        assert _translate_of(reader, orders_id) == pytest.approx(moved, abs=1.0)
        expect(
            reader.locator(f'.react-flow__node[data-id="{orders_id}"]')
        ).not_to_have_class(re.compile(r"\bdraggable\b"))
        expect(reader.get_by_role("button", name="edit view")).to_have_count(0)


class TestSharing:
    def test_shared_role_opens_a_read_only_slice(
        self,
        browsers: Browsers,
        stand: StandProcess,
        catalog_api: Api,
        catalog_seed: Seed,
    ) -> None:
        admin = browsers.page("admin")
        view_id = _create_view(admin, stand, "ed_shared")
        _restrict_to_layers(admin, [Ed.SRC, Ed.DST])

        guest = browsers.page("guest")
        _open_view_denied(guest, stand, view_id)

        admin.get_by_role("button", name="share view").click()
        dialog = admin.locator('[data-dialog="view-shares"]')
        expect(dialog.get_by_test_id("shares-list")).to_contain_text("only you so far")
        share = dialog.get_by_test_id("share-form")
        share.get_by_label("share target kind").select_option("role")
        share.get_by_label("share target", exact=True).fill("GST")
        share.get_by_role("button", name="share").click()
        expect(dialog.locator('[data-share="role:GST"]')).to_be_visible()
        admin.get_by_role("button", name="close dialog").click()

        _open_index(guest, stand)
        item = guest.locator('[data-testid="index-views"] li[data-view="ed_shared"]')
        expect(item).to_contain_text("shared with you")
        item.get_by_role("link").click()
        guest.wait_for_selector(Selector.READY, timeout=30_000)

        expect(guest.locator(Selector.NODE)).to_have_count(len(Seed.DATASETS))
        expect(guest.get_by_test_id("catalog-page")).to_have_attribute(
            "data-owned", "false"
        )
        expect(guest.get_by_test_id("catalog-page")).to_have_attribute(
            "data-editable", "false"
        )
        expect(guest.get_by_role("button", name="edit view")).to_have_count(0)
        expect(guest.get_by_role("button", name="layer", exact=True)).to_have_count(0)
        _node(guest, Ed.ORDERS).click()
        expect(guest.get_by_test_id("detail-panel")).to_have_attribute(
            "data-dataset", Ed.ORDERS
        )
        expect(guest.get_by_role("button", name="edit dataset")).to_have_count(0)

        hidden_draft = catalog_api.new_draft("ed_hidden")
        try:
            guest.goto(f"{stand.config.base_url}/catalog/drafts/{hidden_draft}")
            expect(guest.get_by_text("the catalog is not available")).to_be_visible()
        finally:
            catalog_api.discard(hidden_draft)

        admin.get_by_role("button", name="share view").click()
        dialog.get_by_role("button", name="revoke role GST").click()
        expect(dialog.locator('[data-share="role:GST"]')).to_have_count(0)
        admin.get_by_role("button", name="close dialog").click()

        _open_view_denied(guest, stand, view_id)


def _open_view_denied(page: Page, stand: StandProcess, view_id: str) -> None:
    page.goto(f"{stand.config.base_url}/catalog/views/{view_id}")
    expect(page.get_by_text("the catalog is not available")).to_be_visible()
    expect(page.locator(Selector.NODE)).to_have_count(0)


class TestDelete:
    def test_delete_returns_to_the_index_without_the_view(
        self,
        browsers: Browsers,
        stand: StandProcess,
        catalog_api: Api,
        catalog_seed: Seed,
    ) -> None:
        page = browsers.page("admin")
        view_id = _create_view(page, stand, "ed_doomed")

        page.get_by_role("button", name="delete view").click()
        dialog = page.locator('[data-dialog="view-delete"]')
        expect(dialog).to_contain_text("ed_doomed")
        dialog.get_by_role("button", name="delete the view").click()

        expect(page.get_by_test_id("index-page")).to_be_visible()
        expect(page.locator('li[data-view="ed_doomed"]')).to_have_count(0)
        assert view_id not in {str(view["id"]) for view in catalog_api.views()}
