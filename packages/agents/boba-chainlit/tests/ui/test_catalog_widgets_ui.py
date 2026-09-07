"""Каждая кнопка и виджет страницы процесса по DOM: тулбар холста, шапка,
поиск и подсветка, вкладка подключений, все пути закрытия диалогов, форма
узла, ссылка на просмотр для гостя, свойства процесса, тосты, перебазирование
с конфликтными операциями, вход в правки, аноним, узкий экран.

Сценарии, которые меняют опубликованный процесс (снос узла для конфликта),
стоят в конце модуля: остальные тесты рассчитывают на полный сид.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator
from typing import Any
from uuid import UUID

import pytest
from catalog_ui import Api, Ed, Objects, Seed, Selector, settled_box
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
    name = f"widgets {request.node.name}"
    created = catalog_api.new_draft(catalog_seed.process_id, name)
    try:
        yield created
    finally:
        catalog_api.discard(created)


@pytest.fixture
def process_path(catalog_seed: Seed) -> str:
    """Адрес опубликованной страницы процесса сида."""
    return f"processes/{catalog_seed.process_id}"


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
    """Карточка за шапку на dx, dy; холст сначала должен остановиться после
    вписывания графа в окно, иначе захват промахивается."""
    box = settled_box(page, node)
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
        self, tabs: Tabs, stand: StandProcess, process_path: str
    ) -> None:
        page = tabs.page("admin")
        _open(page, stand, process_path)
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

    def test_tidy_up_lays_the_nodes_out_again(
        self, tabs: Tabs, stand: StandProcess, catalog_seed: Seed, process_path: str
    ) -> None:
        """«Прибрать» раскладывает все карточки ELK заново: узел уходит с места
        из процесса, повторное «прибрать» даёт то же место, счётчик
        готовностей растёт."""
        page = tabs.page("admin")
        _open(page, stand, process_path)
        orders_id = catalog_seed.id_of(Ed.ORDERS)
        seeded = _translate(page, orders_id)

        def tidy() -> None:
            page.get_by_test_id("canvas-toolbar").get_by_role(
                "button", name="tidy up"
            ).click()

        _relayout(page, tidy)
        computed = _translate(page, orders_id)
        assert computed != pytest.approx(seeded, abs=1.0)

        _relayout(page, tidy)
        assert _translate(page, orders_id) == pytest.approx(computed, abs=1.0)

    def test_show_mode_tabs_are_exclusive_and_change_the_cards(
        self, tabs: Tabs, stand: StandProcess, catalog_seed: Seed, process_path: str
    ) -> None:
        page = tabs.page("admin")
        _open(page, stand, process_path)
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
            page.locator(catalog_seed.node(Ed.ORDERS)).locator(".proc-node__column")
        ).to_have_count(0)
        assert "mode=TABLE_NAME" in page.url


class TestTopbar:
    def test_pane_toggle_hides_and_shows_the_left_pane(
        self, tabs: Tabs, stand: StandProcess, process_path: str
    ) -> None:
        page = tabs.page("admin")
        _open(page, stand, process_path)
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

    def test_home_link_returns_to_the_process_list(
        self, tabs: Tabs, stand: StandProcess, catalog_seed: Seed, process_path: str
    ) -> None:
        page = tabs.page("admin")
        _open(page, stand, process_path)
        page.get_by_role("link", name="processes").click()
        listed = page.get_by_test_id("processes-list")
        expect(
            listed.locator(f'li[data-process="{catalog_seed.process_name}"]')
        ).to_be_visible()
        assert page.url.rstrip("/").endswith("/catalog")

    def test_counts_in_the_topbar_follow_the_diagram(
        self, tabs: Tabs, stand: StandProcess, catalog_seed: Seed, process_path: str
    ) -> None:
        page = tabs.page("admin")
        _open(page, stand, process_path)
        hint = page.locator(".topbar__hint")
        expect(hint).to_have_text(f"{_members(catalog_seed)} nodes · 1 flows")
        expect(page.get_by_test_id("page-title")).to_have_text(
            catalog_seed.process_name
        )
        expect(page.get_by_test_id("version-chip")).to_have_text(re.compile(r"^v\d+$"))
        # в шапке нет ни правок, ни видов загрузки, ни диаграмм: всё в панели
        expect(
            page.locator(".topbar").get_by_role("button", name="edit")
        ).to_have_count(0)
        expect(page.get_by_test_id("load-kinds-button")).to_have_count(0)
        expect(page.get_by_test_id("diagrams-button")).to_have_count(0)
        # кнопки «edit» нет нигде: черновик заводит первая правка
        expect(page.get_by_test_id("edit-button")).to_have_count(0)
        expect(page.get_by_test_id("catalog-page")).to_have_attribute(
            "data-editable", "true"
        )


class TestPaneAndHighlight:
    def test_search_narrows_the_list_and_reports_no_matches(
        self, tabs: Tabs, stand: StandProcess, catalog_seed: Seed, process_path: str
    ) -> None:
        page = tabs.page("admin")
        _open(page, stand, process_path)
        pane = page.get_by_test_id("left-pane")
        search = pane.get_by_role("searchbox", name="find a node")

        search.fill("ed_ord")
        expect(pane.get_by_test_id("pane-item")).to_have_count(1)
        expect(pane.get_by_test_id("pane-item")).to_have_attribute(
            "data-node", catalog_seed.address(Ed.ORDERS)
        )

        search.fill("zzz")
        expect(pane.get_by_test_id("pane-item")).to_have_count(0)
        expect(pane.get_by_test_id("pane-empty")).to_have_text("nothing matches")

        search.fill("")
        expect(pane.get_by_test_id("pane-item")).to_have_count(_members(catalog_seed))

    def test_connections_tab_expands_the_tree_and_opens_the_object(
        self, tabs: Tabs, stand: StandProcess, catalog_seed: Seed, process_path: str
    ) -> None:
        """Вкладка подключений: закреплённая полоса действий сверху, раскрытие
        и сворачивание подключения, объект в дереве открывает панель объекта,
        у объекта в процессе — кнопка узла, ссылка на страницу подключений."""
        page = tabs.page("admin")
        _open(page, stand, process_path)
        pane = page.get_by_test_id("left-pane")
        pane.get_by_role("tab", name="connections").click()
        actions = pane.get_by_test_id("connections-actions")
        expect(actions.get_by_test_id("add-connection")).to_be_visible()
        expect(actions.get_by_test_id("connections-link")).to_be_visible()
        branch = pane.locator(
            f'[data-testid="connection-branch"][data-connection="{catalog_seed.connection_name}"]'
        )
        expect(branch).to_contain_text("v1")
        assert Css.box(actions).y < Css.box(branch).y

        branch.get_by_role(
            "button", name=f"expand connection {catalog_seed.connection_name}"
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
        expect(panel.get_by_role("button", name="add to the canvas")).to_have_count(0)

        panel.get_by_role("button", name="open node").click()
        expect(page.get_by_test_id("detail-panel")).to_have_attribute(
            "data-node", catalog_seed.address(Ed.ORDERS)
        )
        expect(page.get_by_test_id("object-panel")).to_have_count(0)

        branch.get_by_role(
            "button", name=f"collapse connection {catalog_seed.connection_name}"
        ).click()
        expect(branch).to_have_attribute("data-open", "false")
        expect(branch.locator('[data-testid="tree-node"]')).to_have_count(0)

        pane.get_by_test_id("connections-link").click()
        expect(page.get_by_test_id("connections-page")).to_be_visible()

    def test_hovering_a_node_highlights_its_neighbours_only(
        self, tabs: Tabs, stand: StandProcess, catalog_seed: Seed, process_path: str
    ) -> None:
        page = tabs.page("admin")
        _open(page, stand, process_path)
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
        self, tabs: Tabs, stand: StandProcess, catalog_seed: Seed, process_path: str
    ) -> None:
        page = tabs.page("admin")
        _open(page, stand, process_path)
        page.locator(catalog_seed.node(Ed.ORDERS)).click()
        panel = page.get_by_test_id("detail-panel")
        panel.get_by_test_id("detail-outgoing").get_by_role(
            "button", name=Ed.SALES, exact=True
        ).click()

        expect(panel).to_have_attribute("data-node", catalog_seed.address(Ed.SALES))
        expect(page.locator(catalog_seed.node(Ed.SALES))).to_have_attribute(
            "data-active", "true"
        )
        expect(panel.get_by_test_id("detail-incoming")).to_contain_text(Ed.ORDERS)
        expect(
            panel.get_by_test_id("detail-outgoing").get_by_test_id("detail-empty")
        ).to_have_text("none")


class TestDialogClosing:
    def test_name_prompt_closes_by_cross_escape_and_cancel_without_changes(
        self,
        tabs: Tabs,
        stand: StandProcess,
        catalog_api: Api,
        catalog_seed: Seed,
        draft_id: str,
    ) -> None:
        page = tabs.page("admin")
        _open(page, stand, f"drafts/{draft_id}")
        page.locator(catalog_seed.node(Ed.LOADER)).click()
        open_prompt = page.get_by_test_id("group-button")
        prompt = _dialog(page, "group-name")

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

        expect(page.locator(f'{Selector.FRAME}[data-group="ed_nope"]')).to_have_count(0)
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
        expect(panel.get_by_test_id("panel-name").first).to_have_text(Ed.ORDERS)

        panel.get_by_role("button", name="retarget node").click()
        expect(panel.locator('[data-notice="retarget-hint"]')).to_be_visible()
        panel.get_by_role("button", name="stop retargeting").click()
        expect(panel.locator('[data-notice="retarget-hint"]')).to_have_count(0)
        expect(
            panel.get_by_test_id("detail-columns").locator("tbody tr")
        ).to_have_count(len(Objects.COLUMNS))

        # у активного набора ребро подсвечено и его широкая зона клика лежит над
        # ярлыком: клик мышью в центр ярлыка попадает в ребро, как у пользователя
        label = settled_box(page, page.locator(Selector.EDGE_LABEL).first)
        page.mouse.dblclick(
            label["x"] + label["width"] / 2, label["y"] + label["height"] / 2
        )
        flow = page.get_by_test_id("flow-form")
        flow.get_by_label("flow description").fill("dropped text")
        flow.get_by_role("button", name="cancel").click()
        expect(flow).to_have_count(0)

        assert catalog_api.state(draft_id)["seq"] == 0

    def test_process_and_share_dialogs_cancel_and_close_without_changes(
        self,
        tabs: Tabs,
        stand: StandProcess,
        catalog_api: Api,
        catalog_seed: Seed,
        process_path: str,
    ) -> None:
        page = tabs.page("admin")
        _open(page, stand, process_path)
        title = page.get_by_test_id("page-title").inner_text()

        page.get_by_role("button", name="process settings").click()
        form = page.get_by_test_id("process-form")
        form.get_by_label("process name").fill("ed_renamed")
        form.get_by_test_id("delete-process").click()
        expect(form.locator('[data-notice="process-delete"]')).to_be_visible()
        form.get_by_role("button", name="cancel").click()
        expect(form).to_have_count(0)
        expect(page.get_by_test_id("page-title")).to_have_text(title)

        page.get_by_test_id("share-button").click()
        shares = _dialog(page, "share")
        expect(shares.get_by_test_id("share-list")).to_have_attribute(
            "data-empty", "true"
        )
        page.keyboard.press("Escape")
        expect(shares).to_have_count(0)

        names = {process["name"] for process in catalog_api.processes()}
        assert catalog_seed.process_name in names
        assert "ed_renamed" not in names

    def test_keep_the_draft_as_is_leaves_a_stale_draft_untouched(
        self,
        tabs: Tabs,
        stand: StandProcess,
        catalog_api: Api,
        catalog_seed: Seed,
        draft_id: str,
    ) -> None:
        page = tabs.page("admin")
        _open(page, stand, f"drafts/{draft_id}")
        base = catalog_api.state(draft_id)["draft"]["base_version"]

        other = catalog_api.publish_ops(
            catalog_seed.process_id,
            "widgets other",
            [
                {
                    "op": "add_group",
                    "group": {"id": str(UUID(int=0xE0F2)), "name": "ed_w_other"},
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
        expect(page.get_by_test_id("processes-list")).not_to_contain_text(
            "widgets test_discard"
        )
        assert catalog_api.state(draft_id)["draft"]["status"] == "discarded"


class TestShare:
    def test_share_link_opens_the_process_for_a_guest_read_only(
        self,
        tabs: Tabs,
        stand: StandProcess,
        catalog_api: Api,
        catalog_seed: Seed,
        process_path: str,
    ) -> None:
        """Владелец выпускает ссылку в диалоге, гость без входа видит процесс
        только на чтение: без вкладок, черновиков и кнопок правок; после
        отзыва ссылка не открывается."""
        page = tabs.page("admin")
        _open(page, stand, process_path)
        page.get_by_test_id("share-button").click()
        dialog = _dialog(page, "share")
        dialog.get_by_test_id("new-share").click()
        row = dialog.get_by_test_id("share-list").locator("li[data-token]")
        expect(row).to_have_count(1)
        token = row.get_attribute("data-token") or ""
        expect(row).to_contain_text(f"/catalog/shared/{token}")

        guest = tabs.page("")
        guest.goto(f"{stand.config.base_url}/catalog/shared/{token}")
        guest.wait_for_selector(Selector.READY, timeout=30_000)
        expect(guest.get_by_test_id("catalog-page")).to_have_attribute(
            "data-source", "shared"
        )
        expect(guest.locator('[data-notice="shared-bar"]')).to_be_visible()
        expect(guest.locator(Selector.NODE)).to_have_count(_members(catalog_seed))
        expect(guest.get_by_role("tablist", name="left pane tab")).to_have_count(0)
        expect(guest.get_by_test_id("processes-group")).to_have_count(0)
        expect(guest.get_by_test_id("catalog-page")).to_have_attribute(
            "data-editable", "false"
        )

        guest.locator(catalog_seed.node(Ed.ORDERS)).click()
        panel = guest.get_by_test_id("detail-panel")
        expect(panel.get_by_test_id("node-card")).to_be_visible(timeout=15_000)
        expect(panel.get_by_role("button", name="edit node")).to_have_count(0)
        flow = panel.get_by_test_id("detail-outgoing").get_by_test_id("detail-flow")
        expect(flow.locator('dd[data-fact="id->id"]')).to_have_text("→ id")

        dialog.get_by_role("button", name=f"revoke link {token}").click()
        expect(row).to_have_count(0, timeout=LIVE_TIMEOUT_MS)
        guest.goto(f"{stand.config.base_url}/catalog/shared/{token}")
        expect(guest.get_by_text("the process is not available")).to_be_visible()


class TestToasts:
    def test_rejected_operation_shows_an_error_toast_that_dismisses_on_click(
        self, tabs: Tabs, stand: StandProcess, catalog_api: Api, draft_id: str
    ) -> None:
        page = tabs.page("admin")
        _open(page, stand, f"drafts/{draft_id}")
        dst = page.locator(f'{Selector.FRAME}[data-group="{Ed.DST}"]')
        dst.get_by_role("button", name=f"rename group {Ed.DST}").click()
        prompt = _dialog(page, "group-name")
        prompt.get_by_role("textbox").fill(Ed.SRC)
        prompt.get_by_role("button", name="save").click()

        toast = _toast(page, "error")
        expect(toast).to_be_visible()
        expect(toast).to_contain_text("duplicate group name")
        toast.click()
        expect(toast).to_have_count(0, timeout=LIVE_TIMEOUT_MS)

        assert catalog_api.state(draft_id)["seq"] == 0
        expect(page.locator(f'{Selector.FRAME}[data-group="{Ed.SRC}"]')).to_have_count(
            1
        )
        expect(dst).to_have_count(1)


class TestEntryNavigation:
    def test_first_edit_of_a_published_process_starts_a_draft(
        self,
        tabs: Tabs,
        stand: StandProcess,
        catalog_api: Api,
        catalog_seed: Seed,
        process_path: str,
    ) -> None:
        """Опубликованный процесс правится сразу: сдвиг карточки заводит
        черновик «draft N», страница уходит на него, правка лежит в нём;
        карандаш в полосе переименовывает черновик."""
        page = tabs.page("admin")
        _open(page, stand, process_path)
        expect(page.locator('[data-notice="draft-bar"]')).to_have_count(0)
        before = len(catalog_api.my_drafts())

        _drag(page, page.locator(catalog_seed.node(Ed.RETURNS)), 60, 40)
        # черновик заведён порцией сдвига: сначала виден по API, потом страница
        # уходит на него
        for _ in range(100):
            if len(catalog_api.my_drafts()) == before + 1:
                break

            page.wait_for_timeout(300)
        else:
            raise AssertionError(f"the drag did not start a draft: {page.url}")

        page.wait_for_url(re.compile(r"/catalog/drafts/[0-9a-f-]{36}$"), timeout=30_000)
        page.wait_for_selector(Selector.READY, timeout=30_000)
        draft_id = page.url.rsplit("/", 1)[1]
        try:
            expect(page.get_by_test_id("catalog-page")).to_have_attribute(
                "data-source", "draft"
            )
            expect(page.get_by_test_id("draft-name")).to_have_text(
                re.compile(r"^draft “draft \d+”$")
            )
            expect(page.get_by_test_id("page-title")).to_have_text(
                catalog_seed.process_name
            )
            assert len(catalog_api.my_drafts()) == before + 1
            state = catalog_api.state(draft_id)
            assert state["seq"] == 1
            moved = state["snapshot"]["nodes"][catalog_seed.id_of(Ed.RETURNS)]
            assert moved["position"] != catalog_seed.position_of(Ed.RETURNS)

            current = page.get_by_test_id("processes-list").locator(
                f'li[data-draft="{state["draft"]["name"]}"]'
            )
            expect(current).to_have_attribute("data-active", "true")
            expect(current).to_contain_text("draft")

            page.get_by_role("button", name="rename draft").click()
            prompt = _dialog(page, "draft-name")
            prompt.get_by_role("textbox").fill("ed_renamed")
            prompt.get_by_role("button", name="save").click()
            expect(page.get_by_test_id("draft-name")).to_have_text("draft “ed_renamed”")
            expect(
                page.get_by_test_id("processes-list").locator(
                    'li[data-draft="ed_renamed"]'
                )
            ).to_have_attribute("data-active", "true")
        finally:
            catalog_api.discard(draft_id)

    def test_process_list_and_drafts_group_open_the_pages(
        self,
        tabs: Tabs,
        stand: StandProcess,
        catalog_api: Api,
        catalog_seed: Seed,
        draft_id: str,
    ) -> None:
        page = tabs.page("admin")
        page.goto(f"{stand.config.base_url}/catalog/")
        listed = page.get_by_test_id("processes-list")
        row = listed.locator(f'li[data-process="{catalog_seed.process_name}"]')
        draft_name = catalog_api.state(draft_id)["draft"]["name"]
        # свой черновик стоит строкой сразу под процессом с чипом draft
        draft_row = listed.locator(f'li[data-draft="{draft_name}"]')
        expect(draft_row).to_contain_text("draft")
        assert Css.box(row).y < Css.box(draft_row).y
        row.get_by_role("link", name=catalog_seed.process_name).click()
        page.wait_for_selector(Selector.READY, timeout=30_000)
        expect(page.get_by_test_id("catalog-page")).to_have_attribute(
            "data-source", "published"
        )

        page.get_by_test_id("processes-list").get_by_role(
            "link", name=draft_name
        ).click()
        page.wait_for_selector(Selector.READY, timeout=30_000)
        expect(page.get_by_test_id("catalog-page")).to_have_attribute(
            "data-source", "draft"
        )
        expect(page.get_by_test_id("draft-name")).to_have_text(f"draft “{draft_name}”")
        expect(page.get_by_test_id("publish-button")).to_be_visible()


class TestTableCard:
    """Одна карточка таблицы у узла на холсте и у объекта дерева: колонки с
    ключом, констрейнты с целью внешнего ключа и правилами, индексы с методом
    и колонками, связанные таблицы кнопками, которые открывают их карточку."""

    def test_node_card_shows_keys_constraints_indexes_and_related(
        self, tabs: Tabs, stand: StandProcess, catalog_seed: Seed, process_path: str
    ) -> None:
        page = tabs.page("admin")
        _open(page, stand, process_path)
        page.locator(catalog_seed.node(Ed.SALES)).click()
        card = page.get_by_test_id("detail-panel").get_by_test_id("node-card")
        expect(card).to_be_visible(timeout=15_000)

        rows = card.get_by_test_id("card-columns").locator("tbody tr")
        expect(rows).to_have_count(len(Objects.COLUMNS))
        expect(
            rows.filter(has_text="id").first.locator("td.table__icon svg")
        ).to_have_count(1)
        expect(rows.filter(has_text="name").locator('[data-col="null"]')).to_have_text(
            "null"
        )

        constraints = card.get_by_test_id("card-constraints")
        expect(constraints.locator('tr[data-kind="primary"]')).to_contain_text("(id)")
        foreign = constraints.locator('tr[data-kind="foreign"]')
        expect(foreign).to_have_count(1)
        expect(foreign.locator('[data-col="detail"]')).to_contain_text(
            f"(id) → public.{Ed.ORDERS} (id)"
        )
        expect(foreign.locator('[data-col="detail"]')).to_contain_text(
            "on delete cascade"
        )
        expect(foreign.locator('[data-col="detail"]')).not_to_contain_text("on update")

        index = card.get_by_test_id("card-indexes").locator(
            f'tr[data-index="{Ed.SALES}_name_idx"]'
        )
        expect(index).to_contain_text("btree")
        expect(index.locator('[data-col="detail"]')).to_contain_text("(name)")

        related = card.get_by_test_id("card-related")
        related.get_by_role("button", name=f"public.{Ed.ORDERS}").click()
        objects = page.get_by_test_id("object-panel")
        expect(objects).to_have_attribute(
            "data-object", catalog_seed.address(Ed.ORDERS)
        )
        expect(objects).to_have_attribute("data-in-process", "true")
        expect(page.get_by_test_id("detail-panel")).to_have_count(0)
        # у первой таблицы внешних ключей нет: связанных нет
        expect(objects.get_by_test_id("object-card-section")).to_be_visible(
            timeout=15_000
        )
        expect(objects.get_by_test_id("card-related")).to_have_count(0)
        expect(objects.get_by_test_id("card-constraints")).to_contain_text("primary")


class TestHomeButtons:
    """Кнопки входа: «process» в полосе и на сцене открывают форму нового
    процесса, «connections» ведёт к подключениям, плюс в панели процесса
    заводит новый процесс не уходя со страницы."""

    def test_new_process_buttons_open_the_form(
        self, tabs: Tabs, stand: StandProcess
    ) -> None:
        page = tabs.page("admin")
        page.goto(f"{stand.config.base_url}/catalog/")
        page.get_by_test_id("process-actions").get_by_test_id("new-process").click()
        form = page.get_by_test_id("new-process-form")
        expect(form.get_by_role("button", name="create")).to_be_disabled()
        form.get_by_role("button", name="cancel").click()
        expect(form).to_have_count(0)

        page.get_by_test_id("process-actions").get_by_test_id("new-process").click()
        expect(page.get_by_test_id("new-process-form")).to_be_visible()
        page.keyboard.press("Escape")
        expect(page.get_by_test_id("new-process-form")).to_have_count(0)

        page.get_by_role("tab", name="connections").click()
        page.get_by_test_id("connections-link").click()
        page.wait_for_url(re.compile(r"/catalog/connections$"), timeout=30_000)
        expect(page.get_by_test_id("connections-page")).to_be_visible()

    def test_pane_toggle_and_object_card_on_the_entry(
        self, tabs: Tabs, stand: StandProcess, catalog_seed: Seed
    ) -> None:
        page = tabs.page("admin")
        page.goto(f"{stand.config.base_url}/catalog/")
        page.get_by_role("button", name="hide the left pane").click()
        expect(page.get_by_test_id("left-pane")).to_have_count(0)
        page.get_by_role("button", name="show the left pane").click()

        page.get_by_role("tab", name="connections").click()
        branch = page.locator(
            f'[data-testid="connection-branch"][data-connection="{catalog_seed.connection_name}"]'
        )
        branch.get_by_role(
            "button", name=f"expand connection {catalog_seed.connection_name}"
        ).click()
        for path, label in (
            ("prod", "prod"),
            ("prod/public", "public"),
            ("prod/public/tables", "tables"),
        ):
            item = branch.locator(f'[data-testid="tree-node"][data-path="{path}"]')
            item.get_by_role("button", name=f"expand {label}").click()

        page.locator(catalog_seed.tree_object(Ed.ORDERS)).locator(
            ".tree__label"
        ).click()
        card = page.get_by_test_id("object-panel")
        expect(card).to_have_attribute("data-object", catalog_seed.address(Ed.ORDERS))
        expect(card).to_have_attribute("data-in-process", "false")
        expect(card.get_by_test_id("object-card-section")).to_be_visible(timeout=15_000)
        card.get_by_role("button", name="close details").click()
        expect(card).to_have_count(0)

    def test_plus_in_the_processes_group_starts_a_draft_of_a_new_process(
        self, tabs: Tabs, stand: StandProcess, catalog_api: Api, process_path: str
    ) -> None:
        """Плюс в секции процессов заводит черновик нового процесса: страница
        уходит на пустой черновик с именем будущего процесса, в списке он
        стоит строкой «new process»; процесса в каталоге ещё нет."""
        page = tabs.page("admin")
        _open(page, stand, process_path)
        page.get_by_test_id("processes-group").get_by_role(
            "button", name="new process"
        ).click()
        form = page.get_by_test_id("new-process-form")
        form.get_by_label("process name").fill("ed_from_pane")
        form.get_by_role("button", name="create").click()
        page.wait_for_url(re.compile(r"/catalog/drafts/[0-9a-f-]{36}$"), timeout=30_000)
        draft_id = page.url.rsplit("/", 1)[1]
        try:
            # новый процесс открывается сразу пустым холстом
            page.wait_for_selector(Selector.READY, timeout=30_000)
            expect(page.locator(Selector.NODE)).to_have_count(0)
            expect(page.get_by_test_id("page-title")).to_have_text("ed_from_pane")
            expect(page.get_by_test_id("version-chip")).to_have_text("v0")
            current = page.get_by_test_id("processes-list").locator(
                'li[data-draft="ed_from_pane"]'
            )
            expect(current).to_have_attribute("data-active", "true")
            expect(current).to_contain_text("new process")
            assert all(
                process["name"] != "ed_from_pane" for process in catalog_api.processes()
            )
        finally:
            catalog_api.discard(draft_id)


class TestAnonymousAndNarrow:
    def test_anonymous_tab_sees_the_unavailable_state(
        self, tabs: Tabs, stand: StandProcess, process_path: str
    ) -> None:
        page = tabs.page("")
        page.goto(f"{stand.config.base_url}/catalog/{process_path}")
        expect(page.get_by_text("the process is not available")).to_be_visible()
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
        page.get_by_role("button", name="hide the left pane").click()
        page.locator(f'{Selector.FRAME}[data-group="{Ed.SRC}"]').get_by_role(
            "button", name=f"rename group {Ed.SRC}"
        ).click()
        dialog = _dialog(page, "group-name")
        expect(dialog).to_be_visible()
        assert Css.box(dialog.get_by_role("dialog")).right <= NARROW["width"]
        assert no_horizontal_scroll(page)


class TestZRebaseWithIssues:
    """Последним: снос узла из опубликованного процесса."""

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
        version = catalog_api.publish_ops(
            catalog_seed.process_id, "widgets removal", removal
        )
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
