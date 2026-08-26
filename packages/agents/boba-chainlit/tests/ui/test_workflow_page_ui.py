"""Страница workflow в браузере: редактор → запуск → живые статусы узлов → Stop."""

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

    BRAND: ClassVar[str] = ".header__brand"
    YAML_TEXT: ClassVar[str] = 'textarea[aria-label="workflow yaml"]'
    NOTICE: ClassVar[str] = ".run-header .notice"
    ISSUE: ClassVar[str] = ".issues__item"
    RUN_STATUS: ClassVar[str] = ".run-header .badge"
    TASK_NODE: ClassVar[str] = ".task-node"
    EDITOR_NODE: ClassVar[str] = ".editor-node"
    TIMELINE_ROW: ClassVar[str] = ".timeline__row"
    INSPECTOR: ClassVar[str] = ".inspector"
    PALETTE_TOOL: ClassVar[str] = ".palette__tool"
    ARG_COMMAND: ClassVar[str] = 'textarea[aria-label="arg command"]'


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
    context = browser.new_context()
    context.add_cookies(login_cookies(stand))
    opened = context.new_page()
    opened.set_default_timeout(PAGE_TIMEOUT_MS)
    BrowserLog(opened)
    try:
        yield opened
    finally:
        context.close()


def _open_editor(page: Page, stand: StandProcess) -> None:
    page.goto(f"{stand.config.base_url}/workflow/new", wait_until="domcontentloaded")
    expect(page.locator(Selector.BRAND)).to_have_text("Boba · Workflow")


def _apply_yaml(page: Page, spec: str) -> None:
    """Вкладка YAML: текст спеки целиком, затем Apply — граф перестраивается."""
    _button(page, "YAML").click()
    page.locator(Selector.YAML_TEXT).fill(spec)
    _button(page, "Apply YAML").click()
    expect(page.locator(Selector.NOTICE)).to_contain_text("yaml applied")
    _button(page, "Graph").click()


def _button(page: Page, label: str):
    return page.get_by_role("button", name=label, exact=True)


def test_palette_and_form_build_a_task(page: Page, stand: StandProcess) -> None:
    """Узел из палитры, аргумент в форме — и всё это видно во вкладке YAML."""
    _open_editor(page, stand)

    page.locator(Selector.PALETTE_TOOL, has_text="bash").first.click()
    expect(page.locator(Selector.EDITOR_NODE)).to_have_count(1)
    page.locator(Selector.ARG_COMMAND).fill("echo FORM_ONE")

    page.locator(Selector.PALETTE_TOOL, has_text="bash").first.click()
    expect(page.locator(Selector.EDITOR_NODE)).to_have_count(2)

    _button(page, "YAML").click()
    yaml_text = page.locator(Selector.YAML_TEXT).input_value()
    assert "command: echo FORM_ONE" in yaml_text
    assert "bash_2:" in yaml_text

    _button(page, "Graph").click()
    _button(page, "Validate").click()
    expect(page.locator(Selector.ISSUE)).to_contain_text("required argument: command")
    expect(page.locator(f'{Selector.EDITOR_NODE}[data-issue="true"]')).to_have_count(1)


def test_editor_validates_saves_and_runs_live(page: Page, stand: StandProcess) -> None:
    _open_editor(page, stand)
    _apply_yaml(page, QUICK_SPEC)
    expect(page.locator(Selector.EDITOR_NODE)).to_have_count(2)

    _button(page, "Validate").click()
    expect(page.locator(Selector.NOTICE)).to_contain_text("valid: 2 stage(s)")

    _button(page, "Save").click()
    expect(page.locator(Selector.NOTICE)).to_contain_text('saved "ui-page-flow"')
    expect(page).to_have_url(re.compile(r"/workflow/w/\d+$"))

    _button(page, "Run").click()
    expect(page).to_have_url(re.compile(r"/workflow/run/[0-9a-f-]+$"))

    # статусы приходят по сокету: узлы доходят до done без перезагрузки
    expect(page.locator(Selector.RUN_STATUS)).to_have_text("done")
    done_nodes = page.locator(f'{Selector.TASK_NODE}[data-status="done"]')
    expect(done_nodes).to_have_count(2)
    expect(page.locator(Selector.TIMELINE_ROW)).to_have_count(2)

    page.locator(Selector.TASK_NODE).first.click()
    expect(page.locator(Selector.INSPECTOR)).to_contain_text("echo PAGE_ONE")


def test_stop_button_stops_a_running_workflow(page: Page, stand: StandProcess) -> None:
    _open_editor(page, stand)
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


def test_list_shows_saved_workflows_and_runs(page: Page, stand: StandProcess) -> None:
    page.goto(f"{stand.config.base_url}/workflow/", wait_until="domcontentloaded")

    expect(page.get_by_role("link", name="ui-page-flow").first).to_be_visible()
    expect(page.locator("table").nth(1)).to_contain_text("ui-page-flow")
