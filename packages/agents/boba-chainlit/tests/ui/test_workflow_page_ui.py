"""Страница workflow в браузере: билдер → запуск → живые статусы → Stop → списки."""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import ClassVar

import pytest
from playwright.sync_api import Browser, Page, expect

from ui.conftest import login_cookies
from ui.stand import StandProcess

pytestmark = pytest.mark.ui

PAGE_TIMEOUT_MS = 60_000

QUICK_SPEC = """name: ui-page-flow
description: two bash steps in a row
tasks:
  first:
    tool: bash
    args:
      command: echo PAGE_ONE
  second:
    tool: bash
    args:
      command: echo PAGE_TWO
edges:
  - first -> second
"""

LONG_SPEC = """name: ui-page-long
tasks:
  wait:
    tool: bash
    args:
      command: sleep 120
"""


class Selector:
    """Селекторы страницы: одно место, чтобы разметка менялась в одном."""

    BRAND: ClassVar[str] = ".topbar__brand"
    RAIL_ITEM: ClassVar[str] = ".rail__item"
    LIST_NEW: ClassVar[str] = ".list__new"
    LIST_ITEM: ClassVar[str] = ".list .item"
    LIST_ITEM_ON: ClassVar[str] = ".list .item--on"
    CRUMB_CURRENT: ClassVar[str] = ".crumbs__current"
    YAML_TEXT: ClassVar[str] = 'textarea[aria-label="workflow yaml"]'
    NOTICE: ClassVar[str] = "[data-notice]"
    ISSUE: ClassVar[str] = ".issues__item"
    RUN_STATUS: ClassVar[str] = ".vitals__badge"
    TASK_NODE: ClassVar[str] = ".task-node"
    EDITOR_NODE: ClassVar[str] = ".editor-node"
    TIMELINE_ROW: ClassVar[str] = ".tl__row"
    TIMELINE_BAR: ClassVar[str] = ".tl__bar"
    INSPECTOR: ClassVar[str] = ".inspector"
    ARG_COMMAND: ClassVar[str] = 'textarea[aria-label="arg command"]'
    TABLE: ClassVar[str] = ".table"
    OUTPUT_TEXT: ClassVar[str] = ".output__text"
    SOCKET_LAMP: ClassVar[str] = ".topbar .lamp"
    PROFILE_SELECT: ClassVar[str] = 'select[aria-label="profile"]'


class BrowserLog:
    """Ошибки консоли и ответы 4xx/5xx печатаются сразу: pytest покажет их в отказе."""

    def __init__(self, page: Page) -> None:
        page.on("console", self._console)
        page.on("pageerror", self._error)
        page.on("response", self._response)

    @staticmethod
    def _console(message: object) -> None:
        kind = getattr(message, "type", "")
        if kind in ("error", "warning"):
            print(f"browser console.{kind}: {getattr(message, 'text', '')}")

    @staticmethod
    def _error(error: object) -> None:
        print(f"browser pageerror: {error}")

    @staticmethod
    def _response(response: object) -> None:
        status = int(getattr(response, "status", 0))
        if status >= 400:
            print(f"browser http {status}: {getattr(response, 'url', '')}")


@pytest.fixture
def stand(workflow_stand: StandProcess) -> StandProcess:
    return workflow_stand


@pytest.fixture
def page(browser: Browser, stand: StandProcess) -> Iterator[Page]:
    context = browser.new_context(viewport={"width": 1280, "height": 900})
    context.add_cookies(login_cookies(stand))
    opened = context.new_page()
    opened.set_default_timeout(PAGE_TIMEOUT_MS)
    BrowserLog(opened)
    try:
        yield opened
    finally:
        context.close()


def _open(page: Page, stand: StandProcess, path: str) -> None:
    page.goto(f"{stand.config.base_url}/workflow{path}", wait_until="domcontentloaded")
    expect(page.locator(Selector.BRAND)).to_contain_text("Workflow")


def _button(page: Page, label: str):
    return page.get_by_role("button", name=label, exact=True)


def _apply_yaml(page: Page, spec: str) -> None:
    """Режим YAML билдера: текст спеки целиком, Apply, обратно в граф."""
    _button(page, "YAML").click()
    page.locator(Selector.YAML_TEXT).fill(spec)
    _button(page, "Apply YAML").click()
    expect(page.locator(Selector.NOTICE)).to_contain_text("yaml applied")
    _button(page, "YAML").click()


def test_shell_navigation(page: Page, stand: StandProcess) -> None:
    """Рейл переключает History/Workflows, сегмент Observe/Build — то же самое."""
    _open(page, stand, "/observe")
    expect(page.locator(Selector.RAIL_ITEM)).to_have_count(2)
    expect(page.locator(".list[aria-label='runs']")).to_be_visible()

    page.get_by_role("link", name="Workflows").first.click()
    expect(page).to_have_url(re.compile(r"/workflow/build$"))
    expect(page.locator(".list[aria-label='workflows']")).to_be_visible()
    expect(page.locator(Selector.LIST_NEW)).to_have_text("+ New workflow")

    page.get_by_role("link", name="Observe").click()
    expect(page).to_have_url(re.compile(r"/workflow/observe$"))


def test_tool_menu_and_form_build_a_task(page: Page, stand: StandProcess) -> None:
    """Узел из меню «+ Tool», аргумент в форме — и всё это видно в YAML."""
    _open(page, stand, "/build/new")

    _button(page, "Tool").click()
    page.get_by_role("menuitem", name="bash").click()
    expect(page.locator(Selector.EDITOR_NODE)).to_have_count(1)
    page.locator(Selector.ARG_COMMAND).fill("echo FORM_ONE")

    _button(page, "Tool").click()
    page.get_by_role("menuitem", name="bash").click()
    expect(page.locator(Selector.EDITOR_NODE)).to_have_count(2)

    _button(page, "YAML").click()
    yaml_text = page.locator(Selector.YAML_TEXT).input_value()
    assert "command: echo FORM_ONE" in yaml_text
    assert "bash_2:" in yaml_text
    _button(page, "YAML").click()

    _button(page, "Validate").click()
    expect(page.locator(Selector.ISSUE)).to_contain_text("required argument: command")
    expect(page.locator(f'{Selector.EDITOR_NODE}[data-issue="true"]')).to_have_count(1)


def test_builder_validates_saves_and_runs_live(page: Page, stand: StandProcess) -> None:
    _open(page, stand, "/build/new")
    _apply_yaml(page, QUICK_SPEC)
    expect(page.locator(Selector.EDITOR_NODE)).to_have_count(2)

    _button(page, "Validate").click()
    expect(page.locator(Selector.NOTICE)).to_contain_text("valid: 2 stage(s)")

    _button(page, "Save").click()
    expect(page.locator(Selector.NOTICE)).to_contain_text('saved "ui-page-flow"')
    expect(page).to_have_url(re.compile(r"/workflow/build/\d+$"))
    expect(page.locator(Selector.CRUMB_CURRENT)).to_have_text("ui-page-flow")
    expect(page.locator(Selector.LIST_ITEM_ON)).to_contain_text("ui-page-flow")

    _button(page, "Run").click()
    expect(page).to_have_url(re.compile(r"/workflow/observe/[0-9a-f-]+$"))
    # лампочка в топбаре: живые снимки идут по websocket через фронт стенда
    expect(page.locator(Selector.SOCKET_LAMP)).to_have_attribute(
        "data-socket", "connected"
    )

    # статусы приходят по сокету: узлы доходят до done без перезагрузки
    expect(page.locator(Selector.RUN_STATUS)).to_have_text("done")
    expect(page.locator(f'{Selector.TASK_NODE}[data-status="done"]')).to_have_count(2)
    expect(page.locator(Selector.LIST_ITEM_ON)).to_contain_text("ui-page-flow")

    page.get_by_role("tab", name="Timeline").click()
    expect(page.locator(Selector.TIMELINE_ROW)).to_have_count(2)
    expect(page.locator(Selector.TIMELINE_BAR)).to_have_count(2)

    page.get_by_role("tab", name="Table").click()
    expect(page.locator(f"{Selector.TABLE} tbody tr")).to_have_count(2)

    page.get_by_role("tab", name="Grid").click()
    page.locator(Selector.TASK_NODE).first.click()
    expect(page.locator(Selector.INSPECTOR)).to_contain_text("echo PAGE_ONE")
    # вывод стадии читается из журнала окнами: stdout целиком, не усечённый итог
    expect(page.locator(Selector.OUTPUT_TEXT)).to_contain_text("PAGE_ONE")


def test_stop_button_stops_a_running_workflow(page: Page, stand: StandProcess) -> None:
    _open(page, stand, "/build/new")
    _apply_yaml(page, LONG_SPEC)
    _button(page, "Save").click()
    expect(page.locator(Selector.NOTICE)).to_contain_text('saved "ui-page-long"')

    _button(page, "Run").click()
    expect(page.locator(f'{Selector.TASK_NODE}[data-status="running"]')).to_have_count(
        1
    )

    _button(page, "Stop").click()
    expect(page.locator(Selector.RUN_STATUS)).to_have_text("stopped")
    expect(page.locator(f'{Selector.TASK_NODE}[data-status="stopped"]')).to_have_count(
        1
    )


def test_lists_show_saved_workflows_and_runs(page: Page, stand: StandProcess) -> None:
    _open(page, stand, "/observe")
    runs = page.locator(Selector.LIST_ITEM, has_text="ui-page-flow")
    expect(runs.first).to_be_visible()
    runs.first.click()
    expect(page).to_have_url(re.compile(r"/workflow/observe/[0-9a-f-]+$"))
    expect(page.locator(Selector.RUN_STATUS)).to_have_text("done")

    _open(page, stand, "/build")
    item = page.locator(Selector.LIST_ITEM, has_text="ui-page-flow")
    expect(item.first).to_contain_text("runs")
    item.first.click()
    expect(page).to_have_url(re.compile(r"/workflow/build/\d+$"))
    expect(page.locator(Selector.EDITOR_NODE)).to_have_count(2)


def test_finished_run_loads_lists_once(page: Page, stand: StandProcess) -> None:
    """Снимок законченного запуска не крутит перезапрос списков по кругу."""
    _open(page, stand, "/observe")
    page.locator(Selector.LIST_ITEM, has_text="ui-page-flow").first.click()
    expect(page.locator(Selector.RUN_STATUS)).to_have_text("done")
    expect(page.locator(Selector.TASK_NODE).first).to_be_visible()

    list_requests: list[str] = []
    page.on("request", lambda request: list_requests.append(request.url))
    page.wait_for_timeout(3000)

    listed = [
        url for url in list_requests if "/workflows" in url or "/workflow-runs?" in url
    ]
    assert listed == []
    expect(page.locator(Selector.TASK_NODE)).to_have_count(2)


def test_profile_chip_switches_the_catalog_and_survives_reload(
    page: Page, stand: StandProcess
) -> None:
    """Профиль search без bash: меню инструментов меняется, выбор переживает reload."""
    _open(page, stand, "/build/new")
    expect(page.locator(Selector.PROFILE_SELECT)).to_have_value("general")
    _button(page, "Tool").click()
    expect(page.get_by_role("menuitem", name="bash")).to_be_enabled()
    page.keyboard.press("Escape")

    # у search нет bash: в каталоге он остаётся, но недоступным (denied → disabled)
    page.locator(Selector.PROFILE_SELECT).select_option("search")
    expect(page.locator(Selector.PROFILE_SELECT)).to_have_value("search")
    _button(page, "Tool").click()
    expect(page.get_by_role("menuitem", name="bash")).to_be_disabled()
    page.keyboard.press("Escape")

    page.reload(wait_until="domcontentloaded")
    expect(page.locator(Selector.PROFILE_SELECT)).to_have_value("search")

    page.locator(Selector.PROFILE_SELECT).select_option("general")
    expect(page.locator(Selector.PROFILE_SELECT)).to_have_value("general")
