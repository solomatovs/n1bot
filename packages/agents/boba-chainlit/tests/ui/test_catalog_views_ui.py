"""Диаграммы процесса: создание из меню «diagrams», фильтр по слоям и узлам,
сохранение раскладки перетаскиванием, шаринг роли и просмотр расшаренной
диаграммы учёткой без прав на каталог, удаление диаграммы.

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


def _open_entry(page: Page, stand: StandProcess) -> None:
    """Вход — опубликованный процесс; без права читать его — только шаринг."""
    page.goto(f"{stand.config.base_url}/catalog/")
    expect(
        page.get_by_test_id("catalog-page").or_(page.get_by_test_id("shared-only"))
    ).to_be_visible()


def _open_diagrams(page: Page) -> Locator:
    page.get_by_test_id("diagrams-button").click()
    dialog = page.locator('[data-dialog="diagrams"]')
    expect(dialog.get_by_test_id("diagrams-list")).to_be_visible()
    return dialog


def _open_view(page: Page, stand: StandProcess, view_id: str) -> None:
    page.goto(f"{stand.config.base_url}/catalog/views/{view_id}")
    page.wait_for_selector(Selector.READY, timeout=30_000)


def _create_view(page: Page, stand: StandProcess, name: str) -> str:
    """Диаграмма из меню страницы; адрес страницы отдаёт её id."""
    _open_entry(page, stand)
    dialog = _open_diagrams(page)
    form = dialog.get_by_test_id("new-view")
    form.get_by_role("textbox").fill(name)
    form.get_by_role("button", name="save slice").click()
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


def _translate_of(page: Page, node_id: str) -> tuple[float, float]:
    """Позиция узла в координатах холста из transform обёртки React Flow."""
    wrapper = page.locator(f'.react-flow__node[data-id="{node_id}"]')
    style = wrapper.get_attribute("style") or ""
    found = TRANSLATE.search(style)
    if found is None:
        raise AssertionError(f"node {node_id} has no translate: {style!r}")

    return float(found.group(1)), float(found.group(2))


def _member_count(seed: Seed) -> int:
    return len(seed.tables) + len(seed.routines)


class TestEntry:
    def test_menus_follow_the_rights(
        self, browsers: Browsers, stand: StandProcess, catalog_seed: Seed
    ) -> None:
        """admin: формы новой диаграммы и черновика; dev: только списки;
        guest: без процесса, только расшаренные диаграммы."""
        admin = browsers.page("admin")
        _open_entry(admin, stand)
        dialog = _open_diagrams(admin)
        expect(dialog.get_by_test_id("new-view")).to_be_visible()
        admin.get_by_role("button", name="close dialog").click()
        admin.get_by_test_id("edit-button").click()
        drafts = admin.locator('[data-dialog="drafts"]')
        expect(drafts.get_by_test_id("new-draft")).to_be_visible()

        dev = browsers.page("dev")
        _open_entry(dev, stand)
        dialog = _open_diagrams(dev)
        expect(dialog.get_by_test_id("new-view")).to_have_count(0)
        dev.get_by_role("button", name="close dialog").click()
        dev.get_by_test_id("edit-button").click()
        drafts = dev.locator('[data-dialog="drafts"]')
        expect(drafts.get_by_test_id("drafts-list")).to_be_visible()
        expect(drafts.get_by_test_id("new-draft")).to_have_count(0)

        guest = browsers.page("guest")
        _open_entry(guest, stand)
        expect(guest.get_by_test_id("shared-only")).to_be_visible()
        expect(guest.get_by_test_id("shared-views")).to_contain_text(
            "nothing is shared with you yet"
        )
        expect(guest.locator(Selector.NODE)).to_have_count(0)


class TestViewFilter:
    def test_layers_then_nodes_narrow_the_diagram(
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
        assert all_nodes >= _member_count(catalog_seed)

        _restrict_to_layers(page, [Ed.SRC, Ed.DST])
        expect(page.locator(Selector.NODE)).to_have_count(_member_count(catalog_seed))
        expect(page.locator(Selector.LANE)).to_have_count(2)
        expect(page.locator(Selector.EDGE_LABEL)).to_have_count(1)

        page.get_by_role("button", name="edit view").click()
        form = page.get_by_test_id("view-form")
        layers = form.get_by_test_id("view-layers")
        layers.get_by_label(Ed.SRC, exact=True).uncheck()
        layers.get_by_label(Ed.DST, exact=True).uncheck()
        nodes = form.get_by_test_id("view-nodes")
        nodes.get_by_label(f"{Ed.DST} / {Ed.RETURNS}", exact=True).check()
        form.get_by_label("view name").fill("ed_returns_only")
        _save_view_form(page)

        expect(page.get_by_test_id("page-title")).to_have_text("ed_returns_only")
        expect(page.locator(Selector.NODE)).to_have_count(1)
        expect(page.locator(catalog_seed.node(Ed.RETURNS))).to_be_visible()
        expect(page.locator(Selector.LANE)).to_have_count(1)
        expect(page.locator(Selector.EDGE_LABEL)).to_have_count(0)

        stored = next(view for view in catalog_api.views() if view["id"] == view_id)
        assert stored["layer_ids"] == []
        assert stored["node_ids"] == [catalog_seed.id_of(Ed.RETURNS)]


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
        expect(page.locator(Selector.NODE)).to_have_count(_member_count(catalog_seed))

        orders_id = catalog_seed.id_of(Ed.ORDERS)
        before = _translate_of(page, orders_id)
        node = page.locator(catalog_seed.node(Ed.ORDERS))
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
        members = {**catalog_seed.tables, **catalog_seed.routines}
        assert set(saved) == {catalog_seed.id_of(name) for name in members}
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

        _open_entry(guest, stand)
        item = guest.locator('[data-testid="shared-views"] li[data-view="ed_shared"]')
        expect(item).to_contain_text("shared with you")
        item.get_by_role("link").click()
        guest.wait_for_selector(Selector.READY, timeout=30_000)

        expect(guest.locator(Selector.NODE)).to_have_count(_member_count(catalog_seed))
        expect(guest.get_by_test_id("catalog-page")).to_have_attribute(
            "data-owned", "false"
        )
        expect(guest.get_by_test_id("catalog-page")).to_have_attribute(
            "data-editable", "false"
        )
        expect(guest.get_by_role("button", name="edit view")).to_have_count(0)
        expect(guest.get_by_role("button", name="layer", exact=True)).to_have_count(0)
        guest.locator(catalog_seed.node(Ed.ORDERS)).click()
        expect(guest.get_by_test_id("detail-panel")).to_have_attribute(
            "data-node", catalog_seed.address(Ed.ORDERS)
        )
        expect(guest.get_by_role("button", name="edit node")).to_have_count(0)
        expect(
            guest.get_by_test_id("detail-panel").get_by_test_id("node-card")
        ).to_be_visible(timeout=15_000)

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
    def test_delete_returns_to_the_entry_without_the_view(
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

        expect(page.get_by_test_id("catalog-page")).to_have_attribute(
            "data-source", "published"
        )
        dialog = _open_diagrams(page)
        expect(dialog.locator('li[data-view="ed_doomed"]')).to_have_count(0)
        assert view_id not in {str(view["id"]) for view in catalog_api.views()}
