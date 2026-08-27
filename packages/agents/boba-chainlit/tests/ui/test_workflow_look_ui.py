"""Внешний вид страницы workflow: каждый блок — DOM, вычисленный CSS, геометрия.

Ожидания цветов и размеров — из tokens.css сборки (Tokens); резиновость —
теми же проверками на узком viewport и при увеличенной плотности пикселей.
"""

from __future__ import annotations

import re
import time
from collections.abc import Iterator
from dataclasses import dataclass
from typing import ClassVar

import httpx
import pytest
from playwright.sync_api import Browser, Page, ViewportSize, expect

from ui.conftest import login_cookies
from ui.look import Css, Tokens, close, fluid, no_horizontal_scroll
from ui.stand import StandProcess

pytestmark = pytest.mark.ui

WIDE: ViewportSize = {"width": 1280, "height": 900}
NARROW: ViewportSize = {"width": 640, "height": 800}

LIST_WIDTH = (220, 0.22, 340)
INSPECTOR_WIDTH = (300, 0.32, 480)


def list_width(viewport: int) -> float:
    minimum, share, maximum = LIST_WIDTH
    return fluid(minimum, share, viewport, maximum)


def inspector_width(viewport: int) -> float:
    minimum, share, maximum = INSPECTOR_WIDTH
    return fluid(minimum, share, viewport, maximum)


SPEC = """name: look-flow
description: two bash steps in a row
tasks:
  first:
    tool: bash
    args:
      command: echo LOOK_ONE
  second:
    tool: bash
    args:
      command: echo LOOK_TWO
edges:
  - first -> second
"""

FAILING_SPEC = """name: look-failing
tasks:
  boom:
    tool: bash
    args:
      command: echo LOOK_BOOM >&2; exit 3
  after:
    tool: bash
    args:
      command: echo never
edges:
  - boom -> after
"""


class Sel:
    """Селекторы блоков страницы."""

    TOPBAR: ClassVar[str] = ".topbar"
    BRAND: ClassVar[str] = ".topbar__brand"
    CRUMBS: ClassVar[str] = ".crumbs"
    MODE_ON: ClassVar[str] = ".topbar .segmented__item--on"
    THEME: ClassVar[str] = 'button[aria-label="Theme"]'
    RAIL: ClassVar[str] = ".rail"
    RAIL_ITEM: ClassVar[str] = ".rail__item"
    RAIL_ON: ClassVar[str] = ".rail__item--on"
    LIST: ClassVar[str] = ".list"
    LIST_FILTER: ClassVar[str] = ".list__filter"
    LIST_NEW: ClassVar[str] = ".list__new"
    ITEM: ClassVar[str] = ".list .item"
    ITEM_ON: ClassVar[str] = ".list .item--on"
    ITEM_DOT: ClassVar[str] = ".item__dot"
    CHIP: ClassVar[str] = ".chip"
    VITALS: ClassVar[str] = ".vitals"
    VITALS_FILL: ClassVar[str] = ".vitals__progress-fill"
    VITALS_DOT: ClassVar[str] = ".vitals__dot"
    VITALS_BADGE: ClassVar[str] = ".vitals__badge"
    VIEW_TAB_ON: ClassVar[str] = '.viewbar [role="tab"][aria-selected="true"]'
    TASK_NODE: ClassVar[str] = ".task-node"
    STAGE_NODE: ClassVar[str] = ".stage-node"
    EDGE_PATH: ClassVar[str] = ".react-flow__edge-path"
    MINIMAP: ClassVar[str] = ".react-flow__minimap"
    CONTROLS: ClassVar[str] = ".react-flow__controls"
    ZOOM_IN: ClassVar[str] = ".react-flow__controls-zoomin"
    VIEWPORT: ClassVar[str] = ".react-flow__viewport"
    TL_ROW: ClassVar[str] = ".tl__row"
    TL_LANE: ClassVar[str] = ".tl__lane"
    TL_BAR: ClassVar[str] = ".tl__bar"
    TL_TICK: ClassVar[str] = ".tl__tick"
    TL_GROUP: ClassVar[str] = ".tl__group"
    TABLE: ClassVar[str] = ".table"
    PILL: ClassVar[str] = ".pill"
    INSPECTOR: ClassVar[str] = ".inspector"
    INSPECTOR_CODE: ClassVar[str] = ".inspector__code"
    JSON_KEY: ClassVar[str] = ".json__key"
    JSON_STRING: ClassVar[str] = ".json__string"
    JSON_BRACE: ClassVar[str] = ".json__open"
    RESULT_VIEW: ClassVar[str] = ".result-view"
    NODE_RESULT: ClassVar[str] = ".task-node__result"
    ARG_ROW: ClassVar[str] = ".arg-row"
    ARG_KEY: ClassVar[str] = ".arg-row__key"
    ARG_VALUE: ClassVar[str] = ".arg-row__value"
    ARG_INTENT: ClassVar[str] = 'input[aria-label="task intent"]'
    BUILDER: ClassVar[str] = ".builder"
    BUILDER_LABEL: ClassVar[str] = ".builder__label"
    MENU_LIST: ClassVar[str] = ".menu__list"
    MENU_ITEM: ClassVar[str] = ".menu__item"
    EDITOR_NODE: ClassVar[str] = ".editor-node"
    HANDLE: ClassVar[str] = ".react-flow__handle"
    REQUIRED: ClassVar[str] = ".field__required"
    ARG_COMMAND: ClassVar[str] = 'textarea[aria-label="arg command"]'
    YAML_TEXT: ClassVar[str] = 'textarea[aria-label="workflow yaml"]'
    ISSUE: ClassVar[str] = ".issues__item"
    NOTICE: ClassVar[str] = "[data-notice]"
    SHELL_BODY: ClassVar[str] = ".shell__body"


@dataclass(frozen=True)
class SeededRun:
    workflow_id: int
    run_id: str


class Rest:
    """REST страницы с cookie входа: стенд готовится без браузера."""

    def __init__(self, stand: StandProcess) -> None:
        self._base = stand.config.base_url
        jar = httpx.Cookies()
        for cookie in login_cookies(stand):
            name = cookie.get("name", "")
            value = cookie.get("value", "")
            jar.set(name, value, domain="127.0.0.1", path="/")

        self._client = httpx.Client(cookies=jar, timeout=30.0)

    def seed(self, spec: str = SPEC, expected: str = "done") -> SeededRun:
        saved = self._client.post(f"{self._base}/api/v1/workflows", json={"spec": spec})
        saved.raise_for_status()
        workflow_id = int(saved.json()["id"])

        run_url = f"{self._base}/api/v1/workflows/{workflow_id}/run"
        started = self._client.post(run_url, json={})
        started.raise_for_status()
        run_id = str(started.json()["run_id"])

        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            run = self._client.get(f"{self._base}/api/v1/workflow-runs/{run_id}")
            run.raise_for_status()
            if run.json()["status"] in ("done", "failed", "stopped"):
                if run.json()["status"] != expected:
                    raise AssertionError(f"seed run is {run.json()['status']}")

                return SeededRun(workflow_id=workflow_id, run_id=run_id)

            time.sleep(0.2)

        raise AssertionError("seed run never finished")


@pytest.fixture(scope="module")
def stand(workflow_stand: StandProcess) -> StandProcess:
    return workflow_stand


@pytest.fixture(scope="module")
def seeded(stand: StandProcess) -> SeededRun:
    return Rest(stand).seed()


@pytest.fixture(scope="module")
def failed_run(stand: StandProcess) -> SeededRun:
    return Rest(stand).seed(FAILING_SPEC, "failed")


@pytest.fixture(scope="module")
def tokens() -> Tokens:
    return Tokens.load()


@pytest.fixture
def page(browser: Browser, stand: StandProcess) -> Iterator[Page]:
    yield from _page(browser, stand, WIDE, 1)


@pytest.fixture
def narrow_page(browser: Browser, stand: StandProcess) -> Iterator[Page]:
    yield from _page(browser, stand, NARROW, 1)


@pytest.fixture
def dense_page(browser: Browser, stand: StandProcess) -> Iterator[Page]:
    """Плотность 2: те же CSS-пиксели, вдвое больше физических."""
    yield from _page(browser, stand, WIDE, 2)


def _page(
    browser: Browser, stand: StandProcess, viewport: ViewportSize, scale: int
) -> Iterator[Page]:
    context = browser.new_context(viewport=viewport, device_scale_factor=scale)
    context.add_cookies(login_cookies(stand))
    opened = context.new_page()
    opened.set_default_timeout(30_000)
    try:
        yield opened
    finally:
        context.close()


def _open(page: Page, stand: StandProcess, path: str) -> None:
    page.goto(f"{stand.config.base_url}/workflow{path}", wait_until="domcontentloaded")
    expect(page.locator(Sel.BRAND)).to_contain_text("Workflow")


def _open_run(page: Page, stand: StandProcess, seeded: SeededRun) -> None:
    _open(page, stand, f"/observe/{seeded.run_id}")
    expect(page.locator(Sel.TASK_NODE)).to_have_count(2)


def _tab(page: Page, label: str) -> None:
    page.get_by_role("tab", name=label).click()


def _add_tool(page: Page, tool: str) -> None:
    page.get_by_role("button", name="Tool", exact=True).click()
    page.get_by_role("menuitem", name=tool).click()


class TestShell:
    """Каркас: топбар, крошки, сегмент режимов, рейл, тема."""

    def test_topbar_geometry_and_colors(
        self, page: Page, stand: StandProcess, tokens: Tokens
    ) -> None:
        _open(page, stand, "/observe")
        topbar = page.locator(Sel.TOPBAR)

        assert Css.box(topbar).height == tokens.px("h-topbar")
        assert Css.of(topbar, "background-color") == tokens.rgb("surface")
        assert Css.of(topbar, "border-bottom-color") == tokens.rgb("hairline")
        brand = page.locator(Sel.BRAND)
        assert "space grotesk" in Css.of(brand, "font-family").lower()
        assert Css.of(brand, "font-weight") == "700"
        assert Css.of(brand.locator("b"), "color") == tokens.rgb("signal")
        expect(page.locator(Sel.CRUMBS)).to_contain_text("History")
        assert "geist mono" in Css.of(page.locator(Sel.CRUMBS), "font-family").lower()

    def test_mode_segment_and_rail(
        self, page: Page, stand: StandProcess, tokens: Tokens
    ) -> None:
        _open(page, stand, "/build")
        on = page.locator(Sel.MODE_ON)
        expect(on).to_have_text("Build")
        assert Css.of(on, "background-color") == tokens.rgb("raised-2")
        assert Css.of(on, "border-radius") == tokens.raw("r-pill")
        assert Css.of(on, "color") == tokens.rgb("ink")

        rail = page.locator(Sel.RAIL)
        assert Css.box(rail).width == tokens.px("w-rail")
        assert Css.of(rail, "border-right-color") == tokens.rgb("hairline")
        expect(page.locator(Sel.RAIL_ITEM)).to_have_count(2)
        active = page.locator(Sel.RAIL_ON)
        expect(active).to_have_text("Workflows")
        assert Css.of(active, "color") == tokens.rgb("signal")
        assert close(Css.box(page.locator(Sel.LIST)).width, list_width(WIDE["width"]))
        expect(page.locator(Sel.CRUMBS)).to_contain_text("Workflows")

    def test_theme_toggle_swaps_tokens_and_survives_reload(
        self, page: Page, stand: StandProcess, tokens: Tokens
    ) -> None:
        _open(page, stand, "/observe")
        body = page.locator("body")
        assert Css.of(body, "background-color") == tokens.rgb("bg")

        page.locator(Sel.THEME).click()
        expect(page.locator("html")).to_have_attribute("data-theme", "light")
        assert Css.of(body, "background-color") == tokens.rgb("bg", "light")
        assert Css.of(body, "color") == tokens.rgb("ink", "light")
        assert Css.of(page.locator(Sel.TOPBAR), "background-color") == tokens.rgb(
            "surface", "light"
        )

        page.reload(wait_until="domcontentloaded")
        expect(page.locator("html")).to_have_attribute("data-theme", "light")


class TestLists:
    """Списки слева: заголовок, фильтр, элементы, «+ New workflow»."""

    def test_run_list_items(
        self, page: Page, stand: StandProcess, seeded: SeededRun, tokens: Tokens
    ) -> None:
        _open(page, stand, f"/observe/{seeded.run_id}")
        listing = page.locator(Sel.LIST)
        expect(listing).to_have_attribute("aria-label", "runs")
        assert Css.of(page.locator(Sel.LIST_FILTER), "border-radius") == tokens.raw(
            "r-cell"
        )
        assert Css.of(page.locator(Sel.LIST_FILTER), "background-color") == tokens.rgb(
            "bg"
        )

        item = page.locator(Sel.ITEM_ON)
        expect(item).to_have_count(1)
        assert Css.of(item, "border-left-color") == tokens.rgb("signal")
        assert Css.of(item, "background-color") == tokens.rgb("raised")
        assert "geist mono" in Css.of(item, "font-family").lower()
        dot = item.locator(Sel.ITEM_DOT)
        assert Css.of(dot, "background-color") == tokens.rgb("status-done")
        assert Css.of(dot, "border-radius") == "50%"
        expect(item).to_contain_text("2 tasks")

        page.locator(Sel.LIST_FILTER).fill("no-such-run")
        expect(page.locator(Sel.ITEM)).to_have_count(0)

    def test_workflow_list_items(
        self, page: Page, stand: StandProcess, seeded: SeededRun, tokens: Tokens
    ) -> None:
        _open(page, stand, "/build")
        new = page.locator(Sel.LIST_NEW)
        assert Css.of(new, "color") == tokens.rgb("signal")
        assert Css.of(new, "border-top-style") == "dashed"

        item = page.locator(Sel.ITEM, has_text="look-flow").first
        chip = item.locator(Sel.CHIP).first
        expect(chip).to_have_text("bash")
        assert Css.of(chip, "color") == tokens.rgb("signal")
        assert Css.of(chip, "text-transform") == "uppercase"
        expect(item.locator(Sel.CHIP).last).to_contain_text("runs")

        item.hover()
        assert Css.of(item, "background-color") == tokens.rgb("raised")


class TestObserve:
    """Сцена наблюдения: vitals, виды, граф, таймлайн, таблица, инспектор."""

    def test_vitals(
        self, page: Page, stand: StandProcess, seeded: SeededRun, tokens: Tokens
    ) -> None:
        _open_run(page, stand, seeded)
        vitals = page.locator(Sel.VITALS)
        assert Css.box(vitals).height >= tokens.px("h-vitals")
        assert Css.of(vitals, "background-color") == tokens.rgb("surface")
        expect(vitals).to_contain_text("2 tasks")

        fill = page.locator(Sel.VITALS_FILL)
        assert Css.of(fill, "background-color") == tokens.rgb("done")
        assert Css.box(fill).width == Css.box(page.locator(".vitals__progress")).width

        done = page.locator(f'{Sel.VITALS_DOT}[data-status="done"]')
        assert Css.of(done, "background-color", "::before") == tokens.rgb("status-done")
        expect(done).to_have_text("2")
        expect(page.locator(f'{Sel.VITALS_DOT}[data-status="failed"]')).to_have_class(
            "vitals__dot vitals__dot--zero"
        )
        expect(vitals).to_contain_text("2/2")

        badge = page.locator(Sel.VITALS_BADGE)
        expect(badge).to_have_text("done")
        assert Css.of(badge, "color") == tokens.rgb("status-done")
        assert Css.of(badge, "border-radius") == tokens.raw("r-pill")

    def test_view_switch_and_graph(
        self, page: Page, stand: StandProcess, seeded: SeededRun, tokens: Tokens
    ) -> None:
        _open_run(page, stand, seeded)
        expect(page.locator(Sel.VIEW_TAB_ON)).to_have_text("Grid")

        node = page.locator(Sel.TASK_NODE).first
        assert Css.of(node, "border-radius") == tokens.raw("r-node")
        assert Css.of(node, "background-color") == tokens.rgb("raised")
        assert Css.of(node, "border-left-color") == tokens.rgb("status-done")
        assert Css.of(node, "border-left-width") == "3px"
        ring = node.locator(".task-node__ring")
        assert Css.of(ring, "border-top-color") == tokens.rgb("status-done")
        expect(ring).to_have_text("✓")

        stage = page.locator(Sel.STAGE_NODE).first
        expect(page.locator(Sel.STAGE_NODE)).to_have_count(2)
        assert Css.of(stage, "border-radius") == tokens.raw("r-phase")
        assert Css.of(stage, "border-top-color") == tokens.rgb("phase-0")
        assert Css.of(
            page.locator(Sel.STAGE_NODE).nth(1), "border-top-color"
        ) == tokens.rgb("phase-1")
        assert Css.box(stage).contains(Css.box(node))

        # строка итога на узле: вид и цифра цветом сигнала
        line = node.locator(Sel.NODE_RESULT)
        expect(line).to_have_text(re.compile(r"^shell · exit 0 \d+ lines$"))
        assert Css.of(line, "color") == tokens.rgb("signal")
        assert Css.box(node).contains(Css.box(line))

        node.click()
        assert Css.of(node, "border-top-color") == tokens.rgb("signal")
        assert Css.of(node, "box-shadow") != "none"

        edge = page.locator(Sel.EDGE_PATH).first
        assert Css.of(edge, "stroke") == tokens.rgb("edge-control")
        expect(page.locator(Sel.MINIMAP)).to_be_visible()
        expect(page.locator(Sel.CONTROLS)).to_be_visible()
        assert Css.of(page.locator(Sel.ZOOM_IN), "background-color") == tokens.rgb(
            "raised"
        )

    def test_timeline(
        self, page: Page, stand: StandProcess, seeded: SeededRun, tokens: Tokens
    ) -> None:
        _open_run(page, stand, seeded)
        _tab(page, "Timeline")
        expect(page.locator(Sel.VIEW_TAB_ON)).to_have_text("Timeline")

        ticks = page.locator(Sel.TL_TICK)
        expect(ticks).to_have_count(5)
        expect(ticks.first).to_have_text("0:00")
        groups = page.locator(Sel.TL_GROUP)
        expect(groups).to_have_count(2)
        assert Css.of(groups.first, "color") == tokens.rgb("phase-0")
        assert Css.of(groups.nth(1), "color") == tokens.rgb("phase-1")

        rows = page.locator(Sel.TL_ROW)
        expect(rows).to_have_count(2)
        first_bar = rows.nth(0).locator(Sel.TL_BAR)
        second_bar = rows.nth(1).locator(Sel.TL_BAR)
        assert Css.of(first_bar, "background-color") == tokens.rgb("phase-0")
        assert Css.of(second_bar, "background-color") == tokens.rgb("phase-1")
        assert Css.box(rows.nth(0).locator(Sel.TL_LANE)).contains(Css.box(first_bar))
        assert Css.box(second_bar).x >= Css.box(first_bar).x
        assert Css.box(first_bar).height == 20
        assert Css.of(first_bar, "border-radius") == "3px"

        second_bar.click()
        expect(page.locator(Sel.INSPECTOR)).to_contain_text("echo LOOK_TWO")

    def test_table_and_inspector(
        self, page: Page, stand: StandProcess, seeded: SeededRun, tokens: Tokens
    ) -> None:
        _open_run(page, stand, seeded)
        _tab(page, "Table")
        header = page.locator(f"{Sel.TABLE} th").first
        assert Css.of(header, "text-transform") == "uppercase"
        assert Css.of(header, "color") == tokens.rgb("muted")
        pill = page.locator(Sel.PILL).first
        expect(pill).to_have_text("done")
        assert Css.of(pill, "background-color", "::before") == tokens.rgb("status-done")
        assert Css.of(pill, "border-radius") == tokens.raw("r-pill")

        page.locator(f"{Sel.TABLE} tbody tr").first.click()
        inspector = page.locator(Sel.INSPECTOR)
        expect(inspector).to_be_visible()
        assert close(Css.box(inspector).width, inspector_width(WIDE["width"]))
        assert Css.of(inspector, "background-color") == tokens.rgb("surface")
        assert Css.of(inspector, "border-left-color") == tokens.rgb("hairline")
        assert Css.of(inspector, "box-shadow") != "none"
        # итог задачи разобран по kind: сводка в шапке, stdout отдельным блоком
        result = inspector.locator(Sel.RESULT_VIEW)
        expect(result).to_have_attribute("data-kind", "shell")
        expect(result.locator(".chip").first).to_have_text("shell")
        expect(result.locator(".result-view__figure")).to_have_text("exit 0")
        expect(result.locator(".result__stream").first).to_contain_text("LOOK_ONE")
        assert (
            Css.of(result.locator(".result__label").first, "text-transform")
            == "uppercase"
        )
        assert Css.of(result.locator(".result__fact dt").first, "color") == tokens.rgb(
            "muted"
        )

        code = inspector.locator(Sel.INSPECTOR_CODE).first
        assert "geist mono" in Css.of(code, "font-family").lower()
        key = code.locator(Sel.JSON_KEY).first
        expect(key).to_have_text("command")
        assert Css.of(key, "color") == tokens.rgb("signal")
        assert Css.of(code.locator(Sel.JSON_BRACE).first, "color") == tokens.rgb(
            "muted"
        )
        assert Css.of(code.locator(Sel.JSON_STRING).first, "color") == tokens.rgb("ink")
        expect(inspector).to_contain_text("echo LOOK_ONE")

        page.get_by_role("button", name="Close inspector").click()
        expect(page.locator(Sel.INSPECTOR)).to_have_count(0)


class TestStatusPalette:
    """Статусы кроме done: полоса vitals, узлы, таймлайн, ошибка инспектора, список."""

    def test_failed_run_colors(
        self, page: Page, stand: StandProcess, failed_run: SeededRun, tokens: Tokens
    ) -> None:
        _open(page, stand, f"/observe/{failed_run.run_id}")
        expect(page.locator(Sel.TASK_NODE)).to_have_count(2)

        badge = page.locator(Sel.VITALS_BADGE)
        expect(badge).to_have_text("failed")
        assert Css.of(badge, "color") == tokens.rgb("status-failed")
        failed_dot = page.locator(f'{Sel.VITALS_DOT}[data-status="failed"]')
        expect(failed_dot).to_have_text("1")
        assert Css.of(failed_dot, "background-color", "::before") == tokens.rgb(
            "status-failed"
        )

        failed = page.locator(f'{Sel.TASK_NODE}[data-status="failed"]')
        skipped = page.locator(f'{Sel.TASK_NODE}[data-status="skipped"]')
        assert Css.of(failed, "border-left-color") == tokens.rgb("status-failed")
        assert Css.of(skipped, "border-left-color") == tokens.rgb("status-skipped")

        _tab(page, "Timeline")
        bar = page.locator(Sel.TL_BAR)
        expect(bar).to_have_count(1)
        assert Css.of(bar, "background-color") == tokens.rgb("error")
        mark = page.locator(".tl__mark--failed")
        assert Css.of(mark, "color") == tokens.rgb("error")

        bar.click()
        error = page.locator(f"{Sel.INSPECTOR_CODE}--error")
        expect(error).to_contain_text("LOOK_BOOM")
        assert Css.of(error, "color") == tokens.rgb("error")

        item = page.locator(Sel.ITEM_ON)
        expect(item).to_contain_text("failed 1/2")
        assert Css.of(item.locator(".is-error"), "color") == tokens.rgb("error")
        assert Css.of(item.locator(Sel.ITEM_DOT), "background-color") == tokens.rgb(
            "status-failed"
        )


class TestBuild:
    """Билдер: панель, меню инструментов, узел с хэндлами, форма, замечания, YAML."""

    def test_builder_bar_and_menu(
        self, page: Page, stand: StandProcess, tokens: Tokens
    ) -> None:
        _open(page, stand, "/build/new")
        bar = page.locator(Sel.BUILDER)
        assert Css.box(bar).height >= tokens.px("h-vitals")
        label = page.locator(Sel.BUILDER_LABEL)
        assert Css.of(label, "color") == tokens.rgb("signal")
        assert "space grotesk" in Css.of(label, "font-family").lower()
        run = page.get_by_role("button", name="Run", exact=True)
        expect(run).to_be_disabled()
        assert Css.of(run, "background-color") == tokens.rgb("signal")
        assert Css.of(run, "opacity") == "0.5"

        page.get_by_role("button", name="Tool", exact=True).click()
        menu = page.locator(Sel.MENU_LIST)
        expect(menu).to_be_visible()
        assert Css.of(menu, "background-color") == tokens.rgb("surface")
        assert Css.of(menu, "box-shadow") != "none"
        expect(menu.locator(f"{Sel.MENU_ITEM}:enabled").first).to_be_visible()
        # chat-only инструменты живут в chainlit: в каталоге студии их нет вовсе
        expect(menu.locator(f"{Sel.MENU_ITEM}:disabled")).to_have_count(0)

    def test_editor_node_form_and_issues(
        self, page: Page, stand: StandProcess, tokens: Tokens
    ) -> None:
        _open(page, stand, "/build/new")
        _add_tool(page, "bash")

        node = page.locator(Sel.EDITOR_NODE)
        expect(node).to_have_count(1)
        assert Css.of(node, "border-radius") == tokens.raw("r-node")
        assert Css.of(node, "border-left-color") == tokens.rgb("signal")
        expect(node.locator(".editor-node__eyebrow")).to_have_text("bash")
        assert (
            Css.of(node.locator(".editor-node__eyebrow"), "text-transform")
            == "uppercase"
        )

        sources = node.locator(f"{Sel.HANDLE}.source")
        targets = node.locator(f"{Sel.HANDLE}.target")
        expect(sources).to_have_count(2)
        assert targets.count() >= 2
        assert Css.of(sources.first, "background-color") == tokens.rgb("signal")

        form = page.locator('[aria-label="task form"]')
        expect(form).to_be_visible()
        assert close(Css.box(form).width, inspector_width(WIDE["width"]))
        assert Css.of(page.locator(Sel.REQUIRED).first, "color") == tokens.rgb("error")
        assert (
            "geist mono" in Css.of(page.locator(Sel.ARG_COMMAND), "font-family").lower()
        )

        page.get_by_role("button", name="Validate", exact=True).click()
        issue = page.locator(Sel.ISSUE)
        expect(issue).to_contain_text("required argument: command")
        assert Css.of(issue, "border-radius") == tokens.raw("r-pill")
        assert Css.of(issue.locator(".issues__code"), "color") == tokens.rgb("error")
        assert Css.of(node, "border-top-color") == tokens.rgb("error")

        # строки тела = порты: ключ цветом, пустой обязательный — ошибкой
        command_row = node.locator(f'{Sel.ARG_ROW}[data-arg="command"]')
        command_value = command_row.locator(Sel.ARG_VALUE)
        expect(command_value).to_have_text("required")
        assert Css.of(command_value, "color") == tokens.rgb("error")
        assert Css.of(command_row.locator(Sel.ARG_KEY), "color") == tokens.rgb("signal")
        expect(command_row.locator(Sel.HANDLE)).to_have_count(1)
        assert Css.box(command_row).contains_y(Css.box(command_row.locator(Sel.HANDLE)))

        before = Css.box(node).height
        page.locator(Sel.ARG_COMMAND).fill("echo " + "x" * 60)
        expect(command_row.locator(Sel.ARG_VALUE)).to_have_text(re.compile("…$"))
        assert Css.of(command_row.locator(Sel.ARG_VALUE), "color") == tokens.rgb("ink")
        assert Css.box(node).height == before

        # intent уходит в шапку блока, а не в строки
        page.locator(Sel.ARG_INTENT).fill("count the things")
        expect(node.locator(".editor-node__intent")).to_have_text("count the things")
        expect(node.locator(f'{Sel.ARG_ROW}[data-arg="intent"]')).to_have_count(0)
        assert Css.box(node).height > before

        footer = node.locator(".editor-node__footer")
        expect(footer).to_contain_text("result")
        expect(footer.locator(".chip")).to_have_text(["shell"])
        assert Css.of(footer, "border-top-color") == tokens.rgb("hairline")

    def test_yaml_mode_and_notices(
        self, page: Page, stand: StandProcess, tokens: Tokens
    ) -> None:
        _open(page, stand, "/build/new")
        yaml_button = page.get_by_role("button", name="YAML", exact=True)
        yaml_button.click()
        expect(yaml_button).to_have_attribute("aria-pressed", "true")
        assert Css.of(yaml_button, "color") == tokens.rgb("signal")
        text = page.locator(Sel.YAML_TEXT)
        assert "geist mono" in Css.of(text, "font-family").lower()
        assert Css.box(text).height >= 300

        text.fill("name: [broken")
        page.get_by_role("button", name="Apply YAML", exact=True).click()
        issue = page.locator(Sel.ISSUE)
        expect(issue).to_contain_text("yaml")

        text.fill(SPEC)
        page.get_by_role("button", name="Apply YAML", exact=True).click()
        notice = page.locator(Sel.NOTICE)
        expect(notice).to_have_text("yaml applied")
        assert Css.of(notice, "border-radius") == tokens.raw("r-pill")
        assert Css.of(notice, "color") == tokens.rgb("muted")
        assert Css.of(notice, "background-color") == tokens.rgb("surface")


class TestLightTheme:
    """Светлая тема пронизывает сцену наблюдения: узлы, панели, список."""

    def test_observe_in_light_theme(
        self, page: Page, stand: StandProcess, seeded: SeededRun, tokens: Tokens
    ) -> None:
        _open_run(page, stand, seeded)
        page.locator(Sel.THEME).click()
        expect(page.locator("html")).to_have_attribute("data-theme", "light")

        node = page.locator(Sel.TASK_NODE).first
        assert Css.of(node, "background-color") == tokens.rgb("raised", "light")
        assert Css.of(node, "color") == tokens.rgb("ink", "light")
        assert Css.of(page.locator(Sel.VITALS), "background-color") == tokens.rgb(
            "surface", "light"
        )
        assert Css.of(page.locator(Sel.LIST), "background-color") == tokens.rgb(
            "surface", "light"
        )
        assert Css.of(
            page.locator(Sel.STAGE_NODE).first, "border-top-color"
        ) == tokens.rgb("phase-0", "light")
        assert Css.of(node, "border-left-color") == tokens.rgb("status-done", "light")


class TestResponsive:
    """Резиновость: ширины панелей по clamp(), плотность пикселей, масштаб канваса."""

    WIDTHS: ClassVar[tuple[int, ...]] = (1920, 1280, 900)

    def test_panels_follow_viewport(
        self, browser: Browser, stand: StandProcess, seeded: SeededRun
    ) -> None:
        """Список и инспектор растут и сжимаются с окном в пределах clamp()."""
        for width in self.WIDTHS:
            for page in _page(browser, stand, {"width": width, "height": 900}, 1):
                _open_run(page, stand, seeded)
                listing = Css.box(page.locator(Sel.LIST))
                assert close(listing.width, list_width(width)), width
                columns = Css.of(page.locator(Sel.SHELL_BODY), "grid-template-columns")
                assert len(columns.split()) == 3, width
                assert no_horizontal_scroll(page), width

                page.locator(Sel.TASK_NODE).first.click()
                # инспектор выезжает анимацией: мерить после неё
                page.wait_for_timeout(350)
                inspector = Css.box(page.locator(Sel.INSPECTOR))
                assert close(inspector.width, inspector_width(width)), width
                assert inspector.right <= width + 1, width

    def test_type_scales_with_viewport(
        self, browser: Browser, stand: StandProcess
    ) -> None:
        sizes: list[float] = []
        for width in (1920, 640):
            for page in _page(browser, stand, {"width": width, "height": 900}, 1):
                _open(page, stand, "/observe")
                sizes.append(
                    float(Css.of(page.locator("body"), "font-size").removesuffix("px"))
                )

        assert sizes[0] > sizes[1]
        assert 12 <= sizes[1] <= sizes[0] <= 14

    def test_narrow_shell_uses_a_drawer(
        self, narrow_page: Page, stand: StandProcess, seeded: SeededRun
    ) -> None:
        _open_run(narrow_page, stand, seeded)
        columns = Css.of(narrow_page.locator(Sel.SHELL_BODY), "grid-template-columns")
        assert len(columns.split()) == 2
        assert Css.of(narrow_page.locator(Sel.MINIMAP), "display") == "none"
        assert no_horizontal_scroll(narrow_page)

        listing = narrow_page.locator(Sel.LIST)
        assert Css.box(listing).right <= 0
        drawer = narrow_page.get_by_role("button", name="Toggle list")
        expect(drawer).to_be_visible()
        drawer.click()
        expect(listing).to_have_class(re.compile("list--open"))
        narrow_page.wait_for_timeout(400)
        box = Css.box(listing)
        assert box.x == 56
        assert box.width <= NARROW["width"] * 0.85 + 1

        narrow_page.locator(Sel.ITEM_ON).click()
        expect(listing).not_to_have_class(re.compile("list--open"))

        # узкий экран: инспектор занимает всю сцену, кроме рейла
        _tab(narrow_page, "Table")
        narrow_page.locator(f"{Sel.TABLE} tbody tr").first.click()
        inspector = Css.box(narrow_page.locator(Sel.INSPECTOR))
        assert inspector.width == NARROW["width"] - 56

    def test_dense_screen_keeps_css_geometry(
        self, dense_page: Page, stand: StandProcess, tokens: Tokens
    ) -> None:
        _open(dense_page, stand, "/observe")
        assert Css.box(dense_page.locator(Sel.TOPBAR)).height == tokens.px("h-topbar")
        assert float(dense_page.evaluate("() => window.devicePixelRatio")) == 2
        assert no_horizontal_scroll(dense_page)

    def test_canvas_zoom_scales_nodes(
        self, page: Page, stand: StandProcess, seeded: SeededRun
    ) -> None:
        _open_run(page, stand, seeded)
        node = page.locator(Sel.TASK_NODE).first
        before = Css.box(node).width
        scale_before = Css.scale(page.locator(Sel.VIEWPORT))

        page.locator(Sel.ZOOM_IN).click()
        page.wait_for_timeout(300)

        assert Css.scale(page.locator(Sel.VIEWPORT)) > scale_before
        assert Css.box(node).width > before
