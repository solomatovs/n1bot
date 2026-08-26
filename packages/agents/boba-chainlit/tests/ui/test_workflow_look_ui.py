"""Внешний вид страницы workflow: каждый блок — DOM, вычисленный CSS, геометрия.

Ожидания цветов и размеров — из tokens.css сборки (Tokens); резиновость —
теми же проверками на узком viewport и при увеличенной плотности пикселей.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from dataclasses import dataclass
from typing import ClassVar

import httpx
import pytest
from playwright.sync_api import Browser, Page, ViewportSize, expect

from ui.conftest import login_cookies
from ui.look import Css, Tokens, no_horizontal_scroll
from ui.stand import StandProcess

pytestmark = pytest.mark.ui

WIDE: ViewportSize = {"width": 1280, "height": 900}
NARROW: ViewportSize = {"width": 640, "height": 800}

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


class Sel:
    """Селекторы блоков страницы."""

    HEADER: ClassVar[str] = ".header"
    BRAND: ClassVar[str] = ".header__brand"
    NAV_LINK: ClassVar[str] = ".header__nav a"
    THEME: ClassVar[str] = 'button[title="Theme"]'
    TABLE: ClassVar[str] = ".table"
    TH: ClassVar[str] = ".table th"
    BADGE: ClassVar[str] = ".badge"
    RUN_HEADER: ClassVar[str] = ".run-header"
    TASK_NODE: ClassVar[str] = ".task-node"
    STAGE_NODE: ClassVar[str] = ".stage-node"
    EDGE_PATH: ClassVar[str] = ".react-flow__edge-path"
    MINIMAP: ClassVar[str] = ".react-flow__minimap"
    CONTROLS: ClassVar[str] = ".react-flow__controls"
    ZOOM_IN: ClassVar[str] = ".react-flow__controls-zoomin"
    VIEWPORT: ClassVar[str] = ".react-flow__viewport"
    TIMELINE_ROW: ClassVar[str] = ".timeline__row"
    TIMELINE_TRACK: ClassVar[str] = ".timeline__track"
    TIMELINE_BAR: ClassVar[str] = ".timeline__bar"
    INSPECTOR: ClassVar[str] = ".inspector"
    INSPECTOR_CODE: ClassVar[str] = ".inspector__code"
    CANVAS: ClassVar[str] = ".canvas"
    PALETTE: ClassVar[str] = ".palette"
    PALETTE_TOOL: ClassVar[str] = ".palette__tool"
    EDITOR_NODE: ClassVar[str] = ".editor-node"
    HANDLE: ClassVar[str] = ".react-flow__handle"
    TAB_ACTIVE: ClassVar[str] = ".tabs .btn--active"
    REQUIRED: ClassVar[str] = ".field__required"
    ARG_COMMAND: ClassVar[str] = 'textarea[aria-label="arg command"]'
    YAML_TEXT: ClassVar[str] = 'textarea[aria-label="workflow yaml"]'


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
            jar.set(
                cookie.get("name", ""),
                cookie.get("value", ""),
                domain="127.0.0.1",
                path="/",
            )

        self._client = httpx.Client(cookies=jar, timeout=30.0)

    def seed(self, spec: str = SPEC, expected: str = "done") -> SeededRun:
        saved = self._client.post(f"{self._base}/workflows", json={"spec": spec})
        saved.raise_for_status()
        workflow_id = int(saved.json()["id"])

        started = self._client.post(
            f"{self._base}/workflows/{workflow_id}/run", json={}
        )
        started.raise_for_status()
        run_id = str(started.json()["run_id"])

        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            run = self._client.get(f"{self._base}/workflow-runs/{run_id}")
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
    expect(page.locator(Sel.BRAND)).to_have_text("Boba · Workflow")


def _open_run(page: Page, stand: StandProcess, seeded: SeededRun) -> None:
    _open(page, stand, f"/run/{seeded.run_id}")
    expect(page.locator(Sel.TASK_NODE)).to_have_count(2)


class TestShell:
    """Каркас: шапка, навигация, переключатель темы, кнопки."""

    def test_header_geometry_and_colors(
        self, page: Page, stand: StandProcess, tokens: Tokens
    ) -> None:
        _open(page, stand, "/")
        header = page.locator(Sel.HEADER)

        assert Css.box(header).height == tokens.px("header-h")
        assert Css.of(header, "background-color") == tokens.rgb("bg-elev")
        assert Css.of(header, "border-bottom-color") == tokens.rgb("border")
        assert Css.of(page.locator(Sel.BRAND), "font-weight") == "600"
        expect(page.locator(Sel.NAV_LINK)).to_have_count(2)
        assert Css.of(page.locator(Sel.NAV_LINK).first, "color") == tokens.rgb("fg")

    def test_theme_toggle_swaps_tokens_and_survives_reload(
        self, page: Page, stand: StandProcess, tokens: Tokens
    ) -> None:
        _open(page, stand, "/")
        body = page.locator("body")
        assert Css.of(body, "background-color") == tokens.rgb("bg")

        page.locator(Sel.THEME).click()
        expect(page.locator("html")).to_have_attribute("data-theme", "light")
        assert Css.of(body, "background-color") == tokens.rgb("bg", "light")
        assert Css.of(body, "color") == tokens.rgb("fg", "light")

        page.reload(wait_until="domcontentloaded")
        expect(page.locator("html")).to_have_attribute("data-theme", "light")

    def test_buttons_follow_tokens(
        self, page: Page, stand: StandProcess, tokens: Tokens
    ) -> None:
        _open(page, stand, "/new")
        save = page.get_by_role("button", name="Save", exact=True)
        run = page.get_by_role("button", name="Run", exact=True)

        assert Css.of(save, "background-color") == tokens.rgb("accent")
        assert Css.of(save, "color") == tokens.rgb("accent-fg")
        assert Css.of(save, "border-radius") == tokens.raw("radius-sm")
        expect(run).to_be_disabled()
        assert Css.of(run, "opacity") == "0.5"
        assert Css.of(run, "cursor") == "default"


class TestListLook:
    """Список: таблицы, заголовки, статусы, наведение."""

    def test_tables_and_badges(
        self, page: Page, stand: StandProcess, seeded: SeededRun, tokens: Tokens
    ) -> None:
        _open(page, stand, "/")
        expect(page.locator(Sel.TABLE)).to_have_count(2)

        header = page.locator(Sel.TH).first
        assert Css.of(header, "text-transform") == "uppercase"
        assert Css.of(header, "color") == tokens.rgb("fg-muted")
        assert Css.of(header, "font-size") == "12px"

        badge = page.locator(Sel.BADGE, has_text="done").first
        assert Css.of(badge, "border-radius") == "999px"
        assert Css.of(badge, "background-color", "::before") == tokens.rgb(
            "status-done"
        )
        assert Css.of(badge, "border-radius", "::before") == "50%"

    def test_row_hover_uses_hover_token(
        self, page: Page, stand: StandProcess, seeded: SeededRun, tokens: Tokens
    ) -> None:
        _open(page, stand, "/")
        cell = page.locator(f"{Sel.TABLE} tbody td").first
        cell.hover()
        assert Css.of(cell, "background-color") == tokens.rgb("bg-hover")


class TestRunLook:
    """Страница запуска: шапка, граф, рёбра, таймлайн, инспектор."""

    def test_header_and_status(
        self, page: Page, stand: StandProcess, seeded: SeededRun, tokens: Tokens
    ) -> None:
        _open_run(page, stand, seeded)
        header = page.locator(Sel.RUN_HEADER)
        assert Css.of(header, "background-color") == tokens.rgb("bg-elev")

        badge = header.locator(Sel.BADGE)
        expect(badge).to_have_text("done")
        assert Css.of(badge, "background-color", "::before") == tokens.rgb(
            "status-done"
        )
        expect(page.get_by_role("button", name="Stop")).to_be_disabled()

    def test_graph_nodes_stages_and_edges(
        self, page: Page, stand: StandProcess, seeded: SeededRun, tokens: Tokens
    ) -> None:
        _open_run(page, stand, seeded)
        node = page.locator(Sel.TASK_NODE).first
        assert Css.of(node, "border-radius") == tokens.raw("radius")
        assert Css.of(node, "background-color") == tokens.rgb("bg-elev")
        ring = node.locator(".task-node__ring")
        assert Css.of(ring, "background-color") == tokens.rgb("status-done")
        assert Css.of(ring, "border-radius") == "50%"

        stage = page.locator(Sel.STAGE_NODE).first
        expect(page.locator(Sel.STAGE_NODE)).to_have_count(2)
        assert Css.of(stage, "border-style") == "dashed"
        assert Css.of(stage, "border-color") == tokens.rgb("border-strong")

        # узел задачи лежит внутри рамки своей стадии
        assert Css.box(stage).contains(Css.box(node))

        node.click()
        assert Css.of(node, "border-color") == tokens.rgb("accent")
        assert Css.of(node, "box-shadow") != "none"

        edge = page.locator(Sel.EDGE_PATH).first
        expect(page.locator(Sel.EDGE_PATH)).to_have_count(1)
        assert Css.of(edge, "stroke") == tokens.rgb("edge-control")
        expect(page.locator(Sel.MINIMAP)).to_be_visible()
        expect(page.locator(Sel.CONTROLS)).to_be_visible()

    def test_timeline_bars(
        self, page: Page, stand: StandProcess, seeded: SeededRun, tokens: Tokens
    ) -> None:
        _open_run(page, stand, seeded)
        rows = page.locator(Sel.TIMELINE_ROW)
        expect(rows).to_have_count(2)

        first_track = Css.box(rows.nth(0).locator(Sel.TIMELINE_TRACK))
        first_bar = Css.box(rows.nth(0).locator(Sel.TIMELINE_BAR))
        second_bar = Css.box(rows.nth(1).locator(Sel.TIMELINE_BAR))
        assert first_track.contains(first_bar)
        assert second_bar.x >= first_bar.x
        assert Css.of(
            rows.nth(0).locator(Sel.TIMELINE_BAR), "background-color"
        ) == tokens.rgb("status-done")

    def test_inspector_panel(
        self, page: Page, stand: StandProcess, seeded: SeededRun, tokens: Tokens
    ) -> None:
        _open_run(page, stand, seeded)
        expect(page.locator(Sel.INSPECTOR)).to_have_count(0)

        page.locator(Sel.TASK_NODE).first.click()
        inspector = page.locator(Sel.INSPECTOR)
        expect(inspector).to_be_visible()
        assert Css.box(inspector).width == 340
        assert Css.of(inspector, "border-left-color") == tokens.rgb("border")
        expect(inspector).to_contain_text("echo LOOK_ONE")

        code = inspector.locator(Sel.INSPECTOR_CODE).first
        assert "mono" in Css.of(code, "font-family").lower()
        assert Css.of(code, "border-radius") == tokens.raw("radius-sm")

        page.locator(".react-flow__pane").click(position={"x": 5, "y": 5})
        expect(page.locator(Sel.INSPECTOR)).to_have_count(0)


class TestEditorLook:
    """Редактор: палитра, узел с хэндлами, форма, вкладки, замечания."""

    def test_palette_groups_and_disabled_tools(
        self, page: Page, stand: StandProcess, tokens: Tokens
    ) -> None:
        _open(page, stand, "/new")
        palette = page.locator(Sel.PALETTE)
        assert Css.box(palette).width == 220
        assert Css.of(palette, "border-right-color") == tokens.rgb("border")

        enabled = palette.locator(f"{Sel.PALETTE_TOOL}:enabled")
        disabled = palette.locator(f"{Sel.PALETTE_TOOL}:disabled")
        expect(enabled.first).to_be_visible()
        expect(disabled.first).to_be_visible()
        assert Css.of(disabled.first, "opacity") == "0.45"
        assert Css.of(enabled.first, "cursor") == "pointer"

    def test_editor_node_handles_form_and_issues(
        self, page: Page, stand: StandProcess, tokens: Tokens
    ) -> None:
        _open(page, stand, "/new")
        page.locator(Sel.PALETTE_TOOL, has_text="bash").first.click()

        node = page.locator(Sel.EDITOR_NODE)
        expect(node).to_have_count(1)
        assert Css.of(node, "border-radius") == tokens.raw("radius")
        assert Css.of(node, "border-color") == tokens.rgb("accent")

        handles = node.locator(Sel.HANDLE)
        targets = node.locator(f"{Sel.HANDLE}.target")
        sources = node.locator(f"{Sel.HANDLE}.source")
        expect(sources).to_have_count(2)
        assert targets.count() >= 2
        assert Css.of(handles.first, "background-color") == tokens.rgb("accent")
        assert Css.box(handles.first).width == 9

        required = page.locator(Sel.REQUIRED).first
        assert Css.of(required, "color") == tokens.rgb("status-failed")
        assert "mono" in Css.of(page.locator(Sel.ARG_COMMAND), "font-family").lower()

        page.get_by_role("button", name="Validate", exact=True).click()
        expect(page.locator(".issues__item")).to_contain_text(
            "required argument: command"
        )
        assert Css.of(node, "border-color") == tokens.rgb("status-failed")

    def test_tabs_and_yaml_view(
        self, page: Page, stand: StandProcess, tokens: Tokens
    ) -> None:
        _open(page, stand, "/new")
        active = page.locator(Sel.TAB_ACTIVE)
        expect(active).to_have_text("Graph")
        assert Css.of(active, "color") == tokens.rgb("accent")

        page.get_by_role("button", name="YAML", exact=True).click()
        expect(page.locator(Sel.TAB_ACTIVE)).to_have_text("YAML")
        text = page.locator(Sel.YAML_TEXT)
        expect(text).to_be_visible()
        assert "mono" in Css.of(text, "font-family").lower()
        assert Css.box(text).height >= 300


class TestResponsive:
    """Резиновость: узкий экран, плотность пикселей, масштаб канваса."""

    def test_wide_editor_has_three_columns(
        self, page: Page, stand: StandProcess
    ) -> None:
        _open(page, stand, "/new")
        page.locator(Sel.PALETTE_TOOL, has_text="bash").first.click()
        columns = Css.of(page.locator(Sel.CANVAS), "grid-template-columns").split()
        assert len(columns) == 3
        assert no_horizontal_scroll(page)

    def test_narrow_editor_stacks_panels(
        self, narrow_page: Page, stand: StandProcess
    ) -> None:
        _open(narrow_page, stand, "/new")
        narrow_page.locator(Sel.PALETTE_TOOL, has_text="bash").first.click()
        columns = Css.of(
            narrow_page.locator(Sel.CANVAS), "grid-template-columns"
        ).split()
        assert len(columns) == 1
        assert Css.box(narrow_page.locator(Sel.PALETTE)).width == NARROW["width"]
        assert no_horizontal_scroll(narrow_page)

    def test_narrow_run_hides_minimap(
        self, narrow_page: Page, stand: StandProcess, seeded: SeededRun
    ) -> None:
        _open_run(narrow_page, stand, seeded)
        assert Css.of(narrow_page.locator(Sel.MINIMAP), "display") == "none"
        assert no_horizontal_scroll(narrow_page)

    def test_dense_screen_keeps_css_geometry(
        self, dense_page: Page, stand: StandProcess, tokens: Tokens
    ) -> None:
        _open(dense_page, stand, "/")
        assert Css.box(dense_page.locator(Sel.HEADER)).height == tokens.px("header-h")
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
        expect(page.locator(Sel.VIEWPORT)).not_to_have_css(
            "transform", Css.of(page.locator(Sel.VIEWPORT), "transform")
        ) if False else None
        page.wait_for_timeout(300)

        scale_after = Css.scale(page.locator(Sel.VIEWPORT))
        assert scale_after > scale_before
        assert Css.box(node).width > before


class TestStatusPalette:
    """Статусы кроме done: цвета кружков, узлов, полос и текста ошибки."""

    def test_failed_and_skipped_colors(
        self, page: Page, stand: StandProcess, failed_run: SeededRun, tokens: Tokens
    ) -> None:
        _open(page, stand, f"/run/{failed_run.run_id}")
        expect(page.locator(Sel.TASK_NODE)).to_have_count(2)

        header_badge = page.locator(Sel.RUN_HEADER).locator(Sel.BADGE)
        expect(header_badge).to_have_text("failed")
        assert Css.of(header_badge, "background-color", "::before") == tokens.rgb(
            "status-failed"
        )

        failed = page.locator(f'{Sel.TASK_NODE}[data-status="failed"]')
        skipped = page.locator(f'{Sel.TASK_NODE}[data-status="skipped"]')
        expect(failed).to_have_count(1)
        expect(skipped).to_have_count(1)
        ring = ".task-node__ring"
        assert Css.of(failed.locator(ring), "background-color") == tokens.rgb(
            "status-failed"
        )
        assert Css.of(skipped.locator(ring), "background-color") == tokens.rgb(
            "status-skipped"
        )

        bars = page.locator(Sel.TIMELINE_BAR)
        expect(bars).to_have_count(1)
        assert Css.of(bars.first, "background-color") == tokens.rgb("status-failed")

        failed.click()
        error = page.locator(f"{Sel.INSPECTOR_CODE}--error")
        expect(error).to_contain_text("LOOK_BOOM")
        assert Css.of(error, "border-color") == tokens.rgb("status-failed")

    def test_list_badges_follow_each_status(
        self,
        page: Page,
        stand: StandProcess,
        seeded: SeededRun,
        failed_run: SeededRun,
        tokens: Tokens,
    ) -> None:
        _open(page, stand, "/")
        for status in ("done", "failed"):
            badge = page.locator(Sel.BADGE, has_text=status).first
            expect(badge).to_be_visible()
            assert Css.of(badge, "background-color", "::before") == tokens.rgb(
                f"status-{status}"
            )
            assert Css.of(badge, "color") == tokens.rgb("fg-muted")


class TestNoticesAndIssues:
    """Сообщения страницы: нейтральное, ошибка, список замечаний."""

    def test_issue_list_and_error_notice(
        self, page: Page, stand: StandProcess, tokens: Tokens
    ) -> None:
        _open(page, stand, "/new")
        page.get_by_role("button", name="YAML", exact=True).click()
        page.locator(Sel.YAML_TEXT).fill("name: [broken")
        page.get_by_role("button", name="Apply YAML", exact=True).click()

        item = page.locator(".issues__item")
        expect(item).to_have_count(1)
        expect(item).to_contain_text("yaml")
        assert Css.of(item, "border-left-color") == tokens.rgb("status-failed")
        assert Css.of(item, "border-left-width") == "3px"
        assert Css.of(item.locator(".issues__code"), "color") == tokens.rgb(
            "status-failed"
        )

    def test_plain_notice_look(
        self, page: Page, stand: StandProcess, tokens: Tokens
    ) -> None:
        _open(page, stand, "/new")
        page.get_by_role("button", name="YAML", exact=True).click()
        page.locator(Sel.YAML_TEXT).fill(SPEC)
        page.get_by_role("button", name="Apply YAML", exact=True).click()

        notice = page.locator(".run-header .notice")
        expect(notice).to_have_text("yaml applied")
        assert Css.of(notice, "background-color") == tokens.rgb("bg-elev")
        assert Css.of(notice, "border-color") == tokens.rgb("border")
        assert Css.of(notice, "border-radius") == tokens.raw("radius")
        assert Css.of(notice, "color") == tokens.rgb("fg-muted")


class TestLightTheme:
    """Светлая тема пронизывает страницу запуска: узлы, панели, текст."""

    def test_run_page_in_light_theme(
        self, page: Page, stand: StandProcess, seeded: SeededRun, tokens: Tokens
    ) -> None:
        _open_run(page, stand, seeded)
        page.locator(Sel.THEME).click()
        expect(page.locator("html")).to_have_attribute("data-theme", "light")

        node = page.locator(Sel.TASK_NODE).first
        assert Css.of(node, "background-color") == tokens.rgb("bg-elev", "light")
        assert Css.of(node, "color") == tokens.rgb("fg", "light")
        assert Css.of(page.locator(Sel.RUN_HEADER), "background-color") == tokens.rgb(
            "bg-elev", "light"
        )
        assert Css.of(page.locator(Sel.STAGE_NODE).first, "border-color") == tokens.rgb(
            "border-strong", "light"
        )
        # статусы от темы не зависят
        ring = node.locator(".task-node__ring")
        assert Css.of(ring, "background-color") == tokens.rgb("status-done", "light")


class TestGraphChrome:
    """Обвязка канваса: кнопки управления, подписи рёбер."""

    def test_controls_and_edge_labels(
        self, page: Page, stand: StandProcess, tokens: Tokens
    ) -> None:
        _open(page, stand, "/new")
        page.get_by_role("button", name="YAML", exact=True).click()
        page.locator(Sel.YAML_TEXT).fill(
            "name: labelled\n"
            "tasks:\n"
            "  a: {tool: bash, args: {command: echo a}}\n"
            "  b: {tool: bash, args: {command: 'echo {{ a }}'}}\n"
            "edges:\n"
            "  - a.result -> b.args.command\n"
        )
        page.get_by_role("button", name="Apply YAML", exact=True).click()
        page.get_by_role("button", name="Graph", exact=True).click()

        label = page.locator(".react-flow__edge-text")
        expect(label).to_have_text("command")
        assert Css.of(label, "fill") == tokens.rgb("fg-muted")
        edge = page.locator(Sel.EDGE_PATH).first
        assert Css.of(edge, "stroke") == tokens.rgb("edge-value")
        assert Css.of(edge, "stroke-dasharray") != "none"

        zoom_in = page.locator(Sel.ZOOM_IN)
        assert Css.of(zoom_in, "background-color") == tokens.rgb("bg-elev")
        assert Css.of(zoom_in, "color") == tokens.rgb("fg")
