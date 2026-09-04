"""Каждая кнопка и виджет страницы процесса по DOM: тулбар холста, шапка,
поиск и подсветка, вкладка источников, все пути закрытия диалогов, форма
узла, типизированные поля потока с колонками стороны и рутиной, виды загрузки
из шапки, тосты, перебазирование с конфликтными операциями, вход в правки,
аноним, узкий экран.

Сценарии, которые меняют опубликованный каталог (снос узла для конфликта),
стоят в конце модуля: остальные тесты рассчитывают на полный сид.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator
from typing import Any
from uuid import UUID

import pytest
from catalog_ui import Api, Ed, Objects, Seed, Selector
from chat_ui import login_cookies
from playwright.sync_api import (
    Browser,
    BrowserContext,
    Locator,
    Page,
    ViewportSize,
    expect,
)

from boba.stand.ui.look import Css, no_horizontal_scroll
from boba.stand.ui.stand import StandProcess

pytestmark = pytest.mark.ui

WIDE: ViewportSize = {"width": 1400, "height": 900}
NARROW: ViewportSize = {"width": 640, "height": 800}
LIVE_TIMEOUT_MS = 15_000
SCALE = re.compile(r"scale\(([\d.]+)\)")
TRANSLATE = re.compile(r"translate\(\s*(-?[\d.]+)px,\s*(-?[\d.]+)px\)")


class Tabs:
    """Вкладки браузера под учётками стенда; закрываются разом."""

    def __init__(self, browser: Browser, stand: StandProcess) -> None:
        self.browser = browser
        self.stand = stand
        self.contexts: list[BrowserContext] = []

    def page(self, login: str, viewport: ViewportSize = WIDE) -> Page:
        context = self.browser.new_context(viewport=viewport)
        if login:
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


@pytest.fixture
def draft_id(
    catalog_api: Api, catalog_seed: Seed, request: pytest.FixtureRequest
) -> Iterator[str]:
    created = catalog_api.new_draft(f"widgets {request.node.name}")
    try:
        yield created
    finally:
        catalog_api.discard(created)


@pytest.fixture
def view_id(
    catalog_api: Api, catalog_seed: Seed, request: pytest.FixtureRequest
) -> str:
    """Вид по двум слоям сида; сам вид сносится модульной фикстурой сида."""
    layers = [catalog_seed.id_of(Ed.SRC), catalog_seed.id_of(Ed.DST)]
    return catalog_api.create_view(f"ed_w_{request.node.name[:24]}", layers, [])


def _open(page: Page, stand: StandProcess, path: str) -> None:
    page.goto(f"{stand.config.base_url}/catalog/{path}")
    page.wait_for_selector(Selector.READY, timeout=30_000)
    page.wait_for_selector(Selector.NODE, timeout=30_000)


def _dialog(page: Page, mark: str) -> Locator:
    return page.locator(f'[data-dialog="{mark}"]')


def _scale(page: Page) -> float:
    style = page.locator(".react-flow__viewport").get_attribute("style") or ""
    found = SCALE.search(style)
    if found is None:
        raise AssertionError(f"viewport has no scale: {style!r}")

    return float(found.group(1))


def _members(seed: Seed) -> int:
    return len(seed.tables) + len(seed.routines)


def _translate(page: Page, node_id: str) -> tuple[float, float]:
    wrapper = page.locator(f'.react-flow__node[data-id="{node_id}"]')
    style = wrapper.get_attribute("style") or ""
    found = TRANSLATE.search(style)
    if found is None:
        raise AssertionError(f"node {node_id} has no translate: {style!r}")

    return float(found.group(1)), float(found.group(2))


def _wait_scale(page: Page, check: Callable[[float], bool]) -> float:
    """Зум React Flow анимируется: ждём, пока масштаб не пройдёт проверку."""
    deadline = 40
    while deadline > 0:
        scale = _scale(page)
        if check(scale):
            return scale

        page.wait_for_timeout(100)
        deadline -= 1

    raise AssertionError(f"scale did not settle: {_scale(page)}")


def _relayout(page: Page, action: Callable[[], None]) -> None:
    canvas = page.get_by_test_id("canvas")
    before = canvas.get_attribute("data-layouts") or "0"
    action()
    expect(canvas).not_to_have_attribute("data-layouts", before, timeout=30_000)
    page.wait_for_selector(Selector.READY, timeout=30_000)


def _drag(page: Page, node: Locator, dx: float, dy: float) -> None:
    box = node.bounding_box()
    assert box is not None
    start = (box["x"] + box["width"] / 2, box["y"] + 12)
    page.mouse.move(*start)
    page.mouse.down()
    page.mouse.move(start[0] + dx / 2, start[1] + dy / 2, steps=5)
    page.mouse.move(start[0] + dx, start[1] + dy, steps=5)
    page.mouse.up()


def _landed(page: Page, seq: int) -> None:
    expect(page.get_by_test_id("catalog-page")).to_have_attribute(
        "data-seq", str(seq), timeout=LIVE_TIMEOUT_MS
    )


def _toast(page: Page, tone: str) -> Locator:
    return page.locator(f'.toast[data-tone="{tone}"]')


def _snapshot_names(state: dict[str, Any], table: str) -> set[str]:
    names: set[str] = set()
    for entity in state["snapshot"][table].values():
        names.add(str(entity["name"]))

    return names


class TestToolbar:
    def test_zoom_buttons_and_fit_view_change_the_viewport_scale(
        self, tabs: Tabs, stand: StandProcess, view_id: str
    ) -> None:
        page = tabs.page("admin")
        _open(page, stand, f"views/{view_id}")
        toolbar = page.get_by_test_id("canvas-toolbar")
        fitted = _scale(page)

        toolbar.get_by_role("button", name="zoom in").click()
        zoomed = _wait_scale(page, lambda scale: scale > fitted * 1.05)

        toolbar.get_by_role("button", name="zoom out").click()
        toolbar.get_by_role("button", name="zoom out").click()
        shrunk = _wait_scale(page, lambda scale: scale < fitted * 0.95)
        assert shrunk < zoomed

        toolbar.get_by_role("button", name="fit view").click()
        refit = _wait_scale(page, lambda scale: abs(scale - fitted) < 0.02)
        assert refit == pytest.approx(fitted, abs=0.02)

    def test_tidy_up_returns_a_dragged_node_to_its_computed_place(
        self,
        tabs: Tabs,
        stand: StandProcess,
        catalog_api: Api,
        catalog_seed: Seed,
        view_id: str,
    ) -> None:
        """Владелец тащит узел, «прибрать» перекладывает ELK заново и сохраняет
        раскладку так же, как перетаскивание."""
        page = tabs.page("admin")
        _open(page, stand, f"views/{view_id}")
        orders_id = catalog_seed.id_of(Ed.ORDERS)
        computed = _translate(page, orders_id)

        _drag(page, page.locator(catalog_seed.node(Ed.ORDERS)), 140, 120)
        catalog_page = page.get_by_test_id("catalog-page")
        expect(catalog_page).to_have_attribute(
            "data-layout-saves", "1", timeout=LIVE_TIMEOUT_MS
        )
        assert _translate(page, orders_id) != computed

        def tidy() -> None:
            page.get_by_test_id("canvas-toolbar").get_by_role(
                "button", name="tidy up"
            ).click()

        _relayout(page, tidy)
        assert _translate(page, orders_id) == pytest.approx(computed, abs=1.0)
        expect(catalog_page).to_have_attribute(
            "data-layout-saves", "2", timeout=LIVE_TIMEOUT_MS
        )
        assert catalog_api.layout(view_id)[orders_id] == pytest.approx(
            computed, abs=1.0
        )

    def test_show_mode_tabs_are_exclusive_and_change_the_cards(
        self, tabs: Tabs, stand: StandProcess, catalog_seed: Seed, view_id: str
    ) -> None:
        page = tabs.page("admin")
        _open(page, stand, f"views/{view_id}")
        tablist = page.get_by_role("tablist", name="show mode")
        expect(tablist.get_by_role("tab", name="keys")).to_have_attribute(
            "aria-selected", "true"
        )

        def names() -> None:
            tablist.get_by_role("tab", name="names").click()

        _relayout(page, names)
        expect(tablist.get_by_role("tab", name="names")).to_have_attribute(
            "aria-selected", "true"
        )
        expect(tablist.get_by_role("tab", name="keys")).to_have_attribute(
            "aria-selected", "false"
        )
        expect(
            page.locator(catalog_seed.node(Ed.ORDERS)).locator(".ds-node__column")
        ).to_have_count(0)
        assert "mode=TABLE_NAME" in page.url


class TestTopbar:
    def test_pane_toggle_hides_and_shows_the_left_pane(
        self, tabs: Tabs, stand: StandProcess, view_id: str
    ) -> None:
        page = tabs.page("admin")
        _open(page, stand, f"views/{view_id}")
        toggle = page.get_by_role("button", name="hide the left pane")
        expect(toggle).to_have_attribute("aria-pressed", "true")
        expect(page.get_by_test_id("left-pane")).to_have_count(1)
        scene_before = Css.box(page.locator(".page__scene"))

        toggle.click()
        shown = page.get_by_role("button", name="show the left pane")
        expect(shown).to_have_attribute("aria-pressed", "false")
        expect(page.get_by_test_id("left-pane")).to_have_count(0)
        assert Css.box(page.locator(".page__scene")).width > scene_before.width

        shown.click()
        expect(page.get_by_test_id("left-pane")).to_have_count(1)
        expect(page.get_by_role("button", name="hide the left pane")).to_have_attribute(
            "aria-pressed", "true"
        )

    def test_home_link_returns_to_the_published_process(
        self, tabs: Tabs, stand: StandProcess, view_id: str
    ) -> None:
        page = tabs.page("admin")
        _open(page, stand, f"views/{view_id}")
        page.get_by_role("link", name="catalog").click()
        expect(page.get_by_test_id("catalog-page")).to_have_attribute(
            "data-source", "published"
        )
        assert page.url.rstrip("/").endswith("/catalog")

    def test_counts_in_the_topbar_follow_the_diagram(
        self, tabs: Tabs, stand: StandProcess, catalog_seed: Seed, view_id: str
    ) -> None:
        page = tabs.page("admin")
        _open(page, stand, f"views/{view_id}")
        hint = page.locator(".topbar__hint")
        expect(hint).to_have_text(f"{_members(catalog_seed)} nodes · 1 flows")
        expect(page.get_by_test_id("page-title")).to_have_text(re.compile(r"^ed_w_"))
        expect(page.locator(".topbar .chip").first).to_have_text(re.compile(r"^v\d+$"))


class TestPaneAndHighlight:
    def test_search_narrows_the_list_and_reports_no_matches(
        self, tabs: Tabs, stand: StandProcess, catalog_seed: Seed, view_id: str
    ) -> None:
        page = tabs.page("admin")
        _open(page, stand, f"views/{view_id}")
        pane = page.get_by_test_id("left-pane")
        search = pane.get_by_role("searchbox", name="find a node")

        search.fill("ed_ord")
        expect(pane.get_by_test_id("pane-item")).to_have_count(1)
        expect(pane.locator(".pane__group")).to_have_count(1)

        search.fill("zzz")
        expect(pane.get_by_test_id("pane-item")).to_have_count(0)
        expect(pane.locator(".pane__empty")).to_have_text("nothing matches")

        search.fill("")
        expect(pane.get_by_test_id("pane-item")).to_have_count(_members(catalog_seed))

    def test_sources_tab_expands_the_tree_and_opens_the_object(
        self, tabs: Tabs, stand: StandProcess, catalog_seed: Seed, view_id: str
    ) -> None:
        """Вкладка источников: раскрытие и сворачивание источника, объект в
        дереве открывает панель объекта, у объекта в процессе — кнопка узла,
        ссылка на страницу источников."""
        page = tabs.page("admin")
        _open(page, stand, f"views/{view_id}")
        pane = page.get_by_test_id("left-pane")
        pane.get_by_role("tab", name="sources").click()
        branch = pane.locator(
            f'[data-testid="source-branch"][data-source="{catalog_seed.source_name}"]'
        )
        expect(branch).to_contain_text("v1")

        branch.get_by_role(
            "button", name=f"expand source {catalog_seed.source_name}"
        ).click()
        expect(branch).to_have_attribute("data-open", "true")
        for path, label in (
            ("prod", "prod"),
            ("prod/public", "public"),
            ("prod/public/tables", "tables"),
        ):
            item = branch.locator(f'[data-testid="tree-node"][data-path="{path}"]')
            item.get_by_role("button", name=f"expand {label}").click()

        pane.locator(catalog_seed.tree_object(Ed.ORDERS)).locator(
            ".tree__label"
        ).click()
        panel = page.get_by_test_id("object-panel")
        expect(panel).to_have_attribute("data-object", catalog_seed.address(Ed.ORDERS))
        expect(panel).to_have_attribute("data-in-process", "true")
        expect(panel.get_by_test_id("object-card-section")).to_be_visible(
            timeout=15_000
        )
        expect(panel.get_by_role("button", name="add to layer")).to_have_count(0)

        panel.get_by_role("button", name="open node").click()
        expect(page.get_by_test_id("detail-panel")).to_have_attribute(
            "data-node", catalog_seed.address(Ed.ORDERS)
        )
        expect(page.get_by_test_id("object-panel")).to_have_count(0)

        branch.get_by_role(
            "button", name=f"collapse source {catalog_seed.source_name}"
        ).click()
        expect(branch).to_have_attribute("data-open", "false")
        expect(branch.locator('[data-testid="tree-node"]')).to_have_count(0)

        pane.get_by_test_id("sources-link").click()
        expect(page.get_by_test_id("sources-page")).to_be_visible()

    def test_hovering_a_node_highlights_its_neighbours_only(
        self, tabs: Tabs, stand: StandProcess, catalog_seed: Seed, view_id: str
    ) -> None:
        page = tabs.page("admin")
        _open(page, stand, f"views/{view_id}")
        sales = page.locator(catalog_seed.node(Ed.SALES))

        page.locator(catalog_seed.node(Ed.ORDERS)).hover()
        expect(sales).to_have_attribute("data-highlighted", "true")
        expect(page.locator(catalog_seed.node(Ed.RETURNS))).to_have_attribute(
            "data-highlighted", "false"
        )
        expect(page.locator(Selector.EDGE_LABEL).first).to_have_attribute(
            "data-highlighted", "true"
        )

        page.mouse.move(5, 5)
        expect(sales).to_have_attribute("data-highlighted", "false")

    def test_flow_target_button_in_the_panel_activates_the_neighbour(
        self, tabs: Tabs, stand: StandProcess, catalog_seed: Seed, view_id: str
    ) -> None:
        page = tabs.page("admin")
        _open(page, stand, f"views/{view_id}")
        page.locator(catalog_seed.node(Ed.ORDERS)).click()
        panel = page.get_by_test_id("detail-panel")
        panel.get_by_test_id("detail-outgoing").get_by_role(
            "button", name=Ed.SALES
        ).click()

        expect(panel).to_have_attribute("data-node", catalog_seed.address(Ed.SALES))
        expect(page.locator(catalog_seed.node(Ed.SALES))).to_have_attribute(
            "data-active", "true"
        )
        expect(panel.get_by_test_id("detail-incoming")).to_contain_text(Ed.ORDERS)
        expect(
            panel.get_by_test_id("detail-outgoing").locator(".detail__empty")
        ).to_have_text("none")


class TestDialogClosing:
    def test_name_prompt_closes_by_cross_escape_and_cancel_without_changes(
        self, tabs: Tabs, stand: StandProcess, catalog_api: Api, draft_id: str
    ) -> None:
        page = tabs.page("admin")
        _open(page, stand, f"drafts/{draft_id}")
        open_prompt = page.get_by_role("button", name="layer", exact=True)
        prompt = _dialog(page, "layer-name")

        open_prompt.click()
        expect(prompt).to_be_visible()
        expect(prompt.get_by_role("button", name="save")).to_be_disabled()
        prompt.get_by_role("textbox").fill("ed_nope")
        expect(prompt.get_by_role("button", name="save")).to_be_enabled()
        prompt.get_by_role("button", name="close dialog").click()
        expect(prompt).to_have_count(0)

        open_prompt.click()
        prompt.get_by_role("textbox").fill("ed_nope")
        page.keyboard.press("Escape")
        expect(prompt).to_have_count(0)

        open_prompt.click()
        prompt.get_by_role("button", name="cancel").click()
        expect(prompt).to_have_count(0)

        expect(page.locator('.pane__group[data-layer="ed_nope"]')).to_have_count(0)
        assert catalog_api.state(draft_id)["seq"] == 0

    def test_node_and_flow_forms_cancel_without_changes(
        self,
        tabs: Tabs,
        stand: StandProcess,
        catalog_api: Api,
        catalog_seed: Seed,
        draft_id: str,
    ) -> None:
        page = tabs.page("admin")
        _open(page, stand, f"drafts/{draft_id}")
        page.locator(catalog_seed.node(Ed.ORDERS)).click()
        panel = page.get_by_test_id("detail-panel")

        panel.get_by_role("button", name="edit node").click()
        form = page.get_by_test_id("node-form")
        form.get_by_label("node alias").fill("ed_changed")
        form.get_by_role("button", name="cancel").click()
        expect(form).to_have_count(0)
        expect(panel.locator(".detail__name").first).to_have_text(Ed.ORDERS)

        panel.get_by_role("button", name="retarget node").click()
        expect(panel.locator('[data-notice="retarget-hint"]')).to_be_visible()
        panel.get_by_role("button", name="stop retargeting").click()
        expect(panel.locator('[data-notice="retarget-hint"]')).to_have_count(0)
        expect(
            panel.get_by_test_id("detail-columns").locator("tbody tr")
        ).to_have_count(len(Objects.COLUMNS))

        # у активного набора ребро подсвечено и его широкая зона клика лежит над
        # ярлыком: клик мышью в центр ярлыка попадает в ребро, как у пользователя
        label = page.locator(Selector.EDGE_LABEL).first.bounding_box()
        assert label is not None
        page.mouse.click(
            label["x"] + label["width"] / 2, label["y"] + label["height"] / 2
        )
        flow = page.get_by_test_id("flow-form")
        flow.get_by_label("flow description").fill("dropped text")
        flow.get_by_role("button", name="cancel").click()
        expect(flow).to_have_count(0)

        assert catalog_api.state(draft_id)["seq"] == 0

    def test_view_dialogs_cancel_and_close_without_changes(
        self, tabs: Tabs, stand: StandProcess, catalog_api: Api, view_id: str
    ) -> None:
        page = tabs.page("admin")
        _open(page, stand, f"views/{view_id}")
        title = page.get_by_test_id("page-title").inner_text()

        page.get_by_role("button", name="edit view").click()
        form = page.get_by_test_id("view-form")
        form.get_by_label("view name").fill("ed_renamed")
        form.get_by_role("button", name="cancel").click()
        expect(form).to_have_count(0)
        expect(page.get_by_test_id("page-title")).to_have_text(title)

        page.get_by_role("button", name="delete view").click()
        confirm = _dialog(page, "view-delete")
        confirm.get_by_role("button", name="cancel").click()
        expect(confirm).to_have_count(0)

        page.get_by_role("button", name="share view").click()
        shares = _dialog(page, "view-shares")
        expect(shares.get_by_role("button", name="share")).to_be_disabled()
        page.keyboard.press("Escape")
        expect(shares).to_have_count(0)

        assert view_id in {str(view["id"]) for view in catalog_api.views()}

    def test_keep_the_draft_as_is_leaves_a_stale_draft_untouched(
        self, tabs: Tabs, stand: StandProcess, catalog_api: Api, draft_id: str
    ) -> None:
        page = tabs.page("admin")
        _open(page, stand, f"drafts/{draft_id}")
        base = catalog_api.state(draft_id)["draft"]["base_version"]

        other = catalog_api.publish_ops(
            "widgets other",
            [
                {
                    "op": "add_layer",
                    "layer": {
                        "id": str(UUID(int=0xE0F2)),
                        "name": "ed_w_other",
                        "position": 8,
                        "description": "",
                    },
                }
            ],
        )
        expect(page.get_by_test_id("rebase-button")).to_have_text(
            f"update to v{other}", timeout=LIVE_TIMEOUT_MS
        )

        page.get_by_test_id("publish-button").click()
        conflict = _dialog(page, "publish-conflict")
        conflict.get_by_role("button", name="keep the draft as is").click()
        expect(conflict).to_have_count(0)
        expect(page.get_by_test_id("rebase-button")).to_be_visible()

        state = catalog_api.state(draft_id)
        assert state["draft"]["status"] == "open"
        assert state["draft"]["base_version"] == base


class TestDiscard:
    def test_discard_asks_first_then_closes_the_draft_and_leaves_the_page(
        self, tabs: Tabs, stand: StandProcess, catalog_api: Api, draft_id: str
    ) -> None:
        page = tabs.page("admin")
        _open(page, stand, f"drafts/{draft_id}")
        page.get_by_test_id("discard-button").click()
        dialog = _dialog(page, "draft-discard")
        expect(dialog).to_contain_text(catalog_api.state(draft_id)["draft"]["name"])

        dialog.get_by_role("button", name="keep editing").click()
        expect(dialog).to_have_count(0)
        assert catalog_api.state(draft_id)["draft"]["status"] == "open"

        page.get_by_test_id("discard-button").click()
        dialog.get_by_role("button", name="discard the draft").click()
        expect(page.get_by_test_id("catalog-page")).to_have_attribute(
            "data-source", "published"
        )
        expect(_toast(page, "success")).to_contain_text("draft discarded")
        page.get_by_test_id("edit-button").click()
        expect(
            _dialog(page, "drafts").get_by_test_id("drafts-list")
        ).not_to_contain_text("widgets test_discard")
        assert catalog_api.state(draft_id)["draft"]["status"] == "discarded"


class TestLoadKinds:
    def test_load_kinds_dialog_lists_creates_edits_and_removes(
        self,
        tabs: Tabs,
        stand: StandProcess,
        catalog_api: Api,
        catalog_seed: Seed,
        draft_id: str,
    ) -> None:
        """Диалог видов загрузки: список сида с полями и счётчиком потоков,
        новый вид с двумя полями, правка имени, удаление вида без потоков;
        вид с потоком удалить нельзя."""
        page = tabs.page("admin")
        _open(page, stand, f"drafts/{draft_id}")
        page.get_by_test_id("load-kinds-button").click()
        dialog = _dialog(page, "load-kinds")
        listing = dialog.get_by_test_id("load-kinds-list")
        expect(listing.locator(f'li[data-kind="{Ed.FULL}"]')).to_contain_text(
            "1 flow(s)"
        )
        expect(
            listing.locator(f'li[data-kind="{Ed.FULL}"]').get_by_role(
                "button", name=f"remove load kind {Ed.FULL}"
            )
        ).to_have_count(0)
        expect(listing.locator(f'li[data-kind="{Ed.TYPED}"] li')).to_have_count(5)
        expect(
            listing.locator(
                f'li[data-kind="{Ed.TYPED}"] li[data-field="{Ed.TYPED_COLUMN}"]'
            )
        ).to_have_text(f"{Ed.TYPED_COLUMN} · column · target")

        dialog.get_by_role("button", name="load kind", exact=True).click()
        form = page.get_by_test_id("load-kind-form")
        expect(form.get_by_role("button", name="save load kind")).to_be_disabled()
        form.get_by_label("load kind name").fill("ed_period")
        form.get_by_role("button", name="field", exact=True).click()
        form.get_by_role("button", name="field", exact=True).click()
        fields = form.get_by_test_id("load-kind-fields")
        expect(fields.locator("li")).to_have_count(2)
        fields.get_by_label("field 0 name").fill("period_column")
        fields.get_by_label("field 0 type").select_option("column")
        fields.get_by_label("field 0 side").select_option("source")
        fields.get_by_label("field 0 required").check()
        fields.get_by_label("field 1 name").fill("period_column")
        expect(form.get_by_role("button", name="save load kind")).to_be_disabled()
        fields.get_by_label("field 1 name").fill("days")
        fields.get_by_label("field 1 type").select_option("int")
        expect(fields.get_by_label("field 1 side")).to_be_disabled()
        fields.get_by_role("button", name="remove field 1").click()
        expect(fields.locator("li")).to_have_count(1)
        form.get_by_role("button", name="save load kind").click()
        _landed(page, 1)
        expect(listing.locator('li[data-kind="ed_period"] li')).to_have_text(
            "period_column · column · source · required"
        )

        listing.locator('li[data-kind="ed_period"]').get_by_role(
            "button", name="edit load kind ed_period"
        ).click()
        form = page.get_by_test_id("load-kind-form")
        form.get_by_label("load kind name").fill("ed_period2")
        form.get_by_role("button", name="save load kind").click()
        _landed(page, 2)
        expect(listing.locator('li[data-kind="ed_period2"]')).to_be_visible()

        listing.locator('li[data-kind="ed_period2"]').get_by_role(
            "button", name="remove load kind ed_period2"
        ).click()
        _landed(page, 3)
        expect(listing.locator('li[data-kind="ed_period2"]')).to_have_count(0)
        page.get_by_role("button", name="close dialog").click()
        expect(dialog).to_have_count(0)

        kinds = _snapshot_names(catalog_api.state(draft_id), "load_kinds")
        assert "ed_period" not in kinds
        assert "ed_period2" not in kinds

    def test_load_kinds_are_read_only_on_the_published_page(
        self, tabs: Tabs, stand: StandProcess, catalog_seed: Seed
    ) -> None:
        page = tabs.page("dev")
        _open(page, stand, "")
        page.get_by_test_id("load-kinds-button").click()
        dialog = _dialog(page, "load-kinds")
        expect(
            dialog.get_by_test_id("load-kinds-list").locator("li[data-kind]")
        ).to_have_count(len(catalog_seed.kinds))
        expect(
            dialog.get_by_role("button", name="load kind", exact=True)
        ).to_have_count(0)
        expect(
            dialog.get_by_role("button", name=re.compile("^edit load kind"))
        ).to_have_count(0)
        page.keyboard.press("Escape")
        expect(dialog).to_have_count(0)


class TestFlowForm:
    def test_typed_load_fields_are_edited_and_shown(
        self,
        tabs: Tabs,
        stand: StandProcess,
        catalog_api: Api,
        catalog_seed: Seed,
        draft_id: str,
    ) -> None:
        """Поля вида по типам: число, флаг, текст, колонка приёмника по имени,
        рутина из узлов-рутин процесса; значения показаны в панели потока."""
        page = tabs.page("admin")
        _open(page, stand, f"drafts/{draft_id}")
        page.locator(catalog_seed.node(Ed.ORDERS)).click()
        page.get_by_test_id("detail-outgoing").get_by_role(
            "button", name="flow", exact=True
        ).click()

        form = page.get_by_test_id("flow-form")
        save = form.get_by_role("button", name="save flow")
        expect(save).to_be_disabled()
        form.get_by_label("flow target").select_option(
            value=catalog_seed.id_of(Ed.RETURNS)
        )
        form.get_by_label("load kind").select_option(label=Ed.TYPED)
        expect(save).to_be_disabled()

        form.get_by_label(f"load field {Ed.TYPED_INT}").fill("7")
        expect(save).to_be_enabled()
        form.get_by_label(f"load field {Ed.TYPED_BOOL}").check()
        form.get_by_label(f"load field {Ed.TYPED_TEXT}").fill("nightly")
        column = form.get_by_label(f"load field {Ed.TYPED_COLUMN}")
        expect(column.locator("option")).to_have_count(len(Objects.COLUMNS) + 1)
        column.select_option(value="name")
        routine = form.get_by_label(f"load field {Ed.TYPED_ROUTINE}")
        expect(routine.locator("option")).to_have_count(2)
        routine.select_option(index=1)
        save.click()
        _landed(page, 1)

        flow = (
            page.get_by_test_id("detail-outgoing")
            .locator(".detail__flow")
            .filter(has_text=Ed.RETURNS)
        )
        values = flow.locator(".detail__value")
        expect(values).to_have_count(5)
        shown = {
            Ed.TYPED_INT: "7",
            Ed.TYPED_BOOL: "true",
            Ed.TYPED_TEXT: "nightly",
            Ed.TYPED_COLUMN: "name",
            Ed.TYPED_ROUTINE: catalog_seed.address(Ed.LOADER),
        }
        for field, text in shown.items():
            value = values.filter(has=page.locator("dt", has_text=field))
            expect(value.locator("dd")).to_have_text(text)

        flows = catalog_api.state(draft_id)["snapshot"]["flows"]
        typed = [
            f
            for f in flows.values()
            if f["load"]["kind_id"] == catalog_seed.id_of(Ed.TYPED)
        ]
        assert typed[0]["load"]["values"] == {
            Ed.TYPED_INT: 7,
            Ed.TYPED_BOOL: True,
            Ed.TYPED_TEXT: "nightly",
            Ed.TYPED_COLUMN: "name",
            Ed.TYPED_ROUTINE: catalog_seed.ref(Ed.LOADER),
        }


class TestToasts:
    def test_rejected_operation_shows_an_error_toast_that_dismisses_on_click(
        self, tabs: Tabs, stand: StandProcess, catalog_api: Api, draft_id: str
    ) -> None:
        page = tabs.page("admin")
        _open(page, stand, f"drafts/{draft_id}")
        page.get_by_role("button", name="layer", exact=True).click()
        prompt = _dialog(page, "layer-name")
        prompt.get_by_role("textbox").fill(Ed.SRC)
        prompt.get_by_role("button", name="save").click()

        toast = _toast(page, "error")
        expect(toast).to_be_visible()
        expect(toast).to_contain_text("duplicate layer name")
        toast.click()
        expect(toast).to_have_count(0, timeout=LIVE_TIMEOUT_MS)

        assert catalog_api.state(draft_id)["seq"] == 0
        expect(page.locator('.pane__group[data-layer="ed_src"]')).to_have_count(1)


class TestEntryNavigation:
    def test_new_draft_form_opens_the_draft_page(
        self, tabs: Tabs, stand: StandProcess, catalog_api: Api, catalog_seed: Seed
    ) -> None:
        page = tabs.page("admin")
        _open(page, stand, "")
        page.get_by_test_id("edit-button").click()
        form = _dialog(page, "drafts").get_by_test_id("new-draft")
        expect(form.get_by_role("button", name="draft")).to_be_disabled()
        form.get_by_role("textbox").fill("ed_from_entry")
        form.get_by_role("button", name="draft").click()
        page.wait_for_url(re.compile(r"/catalog/drafts/[0-9a-f-]{36}$"), timeout=30_000)
        page.wait_for_selector(Selector.READY, timeout=30_000)

        expect(page.get_by_test_id("page-title")).to_have_text("ed_from_entry")
        expect(page.get_by_test_id("catalog-page")).to_have_attribute(
            "data-editable", "true"
        )
        catalog_api.discard(page.url.rsplit("/", 1)[1])

    def test_menus_open_the_view_and_the_draft(
        self,
        tabs: Tabs,
        stand: StandProcess,
        catalog_api: Api,
        view_id: str,
        draft_id: str,
    ) -> None:
        page = tabs.page("admin")
        _open(page, stand, "")
        view_name = next(v["name"] for v in catalog_api.views() if v["id"] == view_id)
        draft_name = catalog_api.state(draft_id)["draft"]["name"]

        page.get_by_test_id("diagrams-button").click()
        _dialog(page, "diagrams").get_by_role("link", name=view_name).click()
        page.wait_for_selector(Selector.READY, timeout=30_000)
        expect(page.get_by_test_id("catalog-page")).to_have_attribute(
            "data-source", "view"
        )
        expect(page.get_by_test_id("page-title")).to_have_text(view_name)

        page.get_by_role("link", name="catalog").click()
        page.wait_for_selector(Selector.READY, timeout=30_000)
        expect(page.get_by_test_id("edit-button")).to_contain_text("edit · ")
        page.get_by_test_id("edit-button").click()
        drafts = _dialog(page, "drafts").get_by_test_id("drafts-list")
        expect(drafts.locator("li").filter(has_text=draft_name)).to_contain_text(
            "yours"
        )
        drafts.get_by_role("link", name=draft_name).click()
        page.wait_for_selector(Selector.READY, timeout=30_000)
        expect(page.get_by_test_id("catalog-page")).to_have_attribute(
            "data-source", "draft"
        )
        expect(page.get_by_test_id("page-title")).to_have_text(draft_name)


class TestAnonymousAndNarrow:
    def test_anonymous_tab_sees_the_unavailable_state(
        self, tabs: Tabs, stand: StandProcess, view_id: str
    ) -> None:
        page = tabs.page("")
        page.goto(f"{stand.config.base_url}/catalog/views/{view_id}")
        expect(page.get_by_text("the catalog is not available")).to_be_visible()
        expect(page.locator(Selector.NODE)).to_have_count(0)

    def test_narrow_screen_keeps_dialogs_and_editing_within_the_viewport(
        self, tabs: Tabs, stand: StandProcess, catalog_seed: Seed, draft_id: str
    ) -> None:
        page = tabs.page("admin", NARROW)
        _open(page, stand, f"drafts/{draft_id}")
        expect(page.get_by_test_id("left-pane")).to_have_count(0)
        assert no_horizontal_scroll(page)

        page.locator(catalog_seed.node(Ed.ORDERS)).click()
        panel = page.get_by_test_id("detail-panel")
        expect(panel.get_by_role("button", name="edit node")).to_be_visible()
        panel.get_by_role("button", name="edit node").click()
        expect(page.get_by_test_id("node-form")).to_be_visible()
        assert no_horizontal_scroll(page)
        page.get_by_test_id("node-form").get_by_role("button", name="cancel").click()

        page.get_by_role("button", name="show the left pane").click()
        page.get_by_role("button", name="layer", exact=True).click()
        dialog = _dialog(page, "layer-name")
        expect(dialog).to_be_visible()
        assert Css.box(dialog.locator(".dialog")).right <= NARROW["width"]
        assert no_horizontal_scroll(page)


class TestZRebaseWithIssues:
    """Последним: снос узла из опубликованного каталога."""

    def test_conflicting_operation_is_listed_and_dropped_on_request(
        self,
        tabs: Tabs,
        stand: StandProcess,
        catalog_api: Api,
        catalog_seed: Seed,
        draft_id: str,
    ) -> None:
        page = tabs.page("admin")
        _open(page, stand, f"drafts/{draft_id}")
        page.locator(catalog_seed.node(Ed.RETURNS)).click()
        panel = page.get_by_test_id("detail-panel")
        panel.get_by_role("button", name="edit node").click()
        page.get_by_test_id("node-form").get_by_label("node alias").fill("ed_returns_x")
        page.get_by_test_id("node-form").get_by_role("button", name="save node").click()
        _landed(page, 1)

        removal: list[dict[str, Any]] = [
            {"op": "remove_node", "id": catalog_seed.id_of(Ed.RETURNS)}
        ]
        version = catalog_api.publish_ops("widgets removal", removal)
        expect(page.get_by_test_id("rebase-button")).to_have_text(
            f"update to v{version}", timeout=LIVE_TIMEOUT_MS
        )

        page.get_by_test_id("publish-button").click()
        conflict = _dialog(page, "publish-conflict")
        conflict.get_by_role("button", name="update the draft").click()

        issues = conflict.get_by_test_id("rebase-issues")
        expect(issues).to_be_visible()
        expect(issues.locator("li")).to_have_count(1)
        expect(issues.locator("li").first).to_contain_text("portion 1 · operation #0")
        expect(conflict.get_by_role("button", name="update the draft")).to_have_count(0)

        conflict.get_by_role("button", name="drop the conflicts and update").click()
        expect(conflict).to_have_count(0)
        expect(_toast(page, "success")).to_contain_text("1 operation(s) dropped")
        expect(page.locator(catalog_seed.node(Ed.RETURNS))).to_have_count(0)
        expect(page.get_by_test_id("rebase-button")).to_have_count(0)

        state = catalog_api.state(draft_id)
        assert state["draft"]["base_version"] == version
        assert catalog_seed.id_of(Ed.RETURNS) not in state["snapshot"]["nodes"]
