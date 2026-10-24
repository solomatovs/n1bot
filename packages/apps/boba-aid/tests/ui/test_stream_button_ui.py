"""Кнопка живого вывода видна на бегущем шаге инструмента, а журнал в
панели растёт, пока вызов идёт.

Фронт chainlit монтирует inline-элементы шага только вместе с секцией output,
поэтому бегущий шаг обязан приходить с непустым output.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from boba.stand.ui.chat_page import ChatPage, StepKind
from boba.stand.ui.fake_llm import ScenarioName
from boba.stand.ui.stand import (
    StandApp,
    StandConfig,
    StandPorts,
    StandProcess,
    StandToolNames,
)

pytestmark = pytest.mark.ui

BOOT_TIMEOUT_SEC = 300.0
TURN_TIMEOUT_SEC = 120.0
BUTTON = "[data-canvas-stream]"
POLL_SEC = 15.0
PROBE = "stream-line"
LINES = 8


@pytest.fixture(scope="module")
def sandbox_stand(
    stand_workdir: Path, llm_port: int, fake_llm: None, stand_database: str
) -> Iterator[StandProcess]:
    config = StandConfig(
        workdir=stand_workdir / "sandbox-stream",
        app=StandApp.AID,
        app_port=StandPorts().free(),
        llm_port=llm_port,
        db_name=stand_database,
        url_prefix="/boba-stream",
        sandbox=True,
        service_plugins=("bash",),
    )
    process = StandProcess(config=config, log_path=stand_workdir / "stream-app.log")
    process.start(boot_timeout_sec=BOOT_TIMEOUT_SEC)
    try:
        yield process
    finally:
        process.stop()


@pytest.fixture
def sandbox_chat(sandbox_stand: StandProcess, open_chat: Any) -> ChatPage:
    return open_chat(sandbox_stand)


def _bash_call(command: str) -> str:
    name = StandToolNames().full("bash")
    request = {"name": name, "arguments": {"command": command}}
    return f"{ScenarioName.CALL.value} {json.dumps(request, ensure_ascii=False)}"


def _button_appears(chat: ChatPage, within_sec: float) -> bool:
    deadline = time.monotonic() + within_sec
    while time.monotonic() < deadline:
        if chat.page.locator(BUTTON).count() > 0:
            return True

        chat.page.wait_for_timeout(200)

    return False


def test_journal_grows_in_the_panel_while_the_call_runs(
    sandbox_chat: ChatPage, sandbox_stand: StandProcess
) -> None:
    """Адрес журнала сервер называет, едва журнал открыт: кнопка журнала
    стоит на бегущем шаге, а текст журнала в панели растёт, пока вызов
    идёт, — по событиям подписки subscriptions/listen, а не запасным
    опросом."""
    mark = sandbox_stand.log_lines()
    lines = " ".join(str(number) for number in range(1, LINES + 1))
    command = f"for i in {lines}; do echo {PROBE}-$i; sleep 1; done"
    sandbox_chat.ask(_bash_call(command))
    sandbox_chat.await_step(StepKind.RUN, timeout_ms=TURN_TIMEOUT_SEC * 1000)
    sandbox_chat.expand_process()
    titled = "stand call of bash"
    sandbox_chat.await_tool_titled(titled, timeout_ms=TURN_TIMEOUT_SEC * 1000)
    sandbox_chat.expand_tool_titled(titled)

    if not _button_appears(sandbox_chat, POLL_SEC):
        raise AssertionError(
            "stream button is absent on the running tool step\n"
            + sandbox_chat.log.describe()
        )

    sandbox_chat.page.locator(BUTTON).first.click()
    panel = sandbox_chat.page.locator("#side-view-content")
    panel.wait_for(state="visible", timeout=POLL_SEC * 1000)
    first = panel.inner_text()

    last = f"{PROBE}-{LINES}"
    deadline = time.monotonic() + TURN_TIMEOUT_SEC
    while last not in panel.inner_text():
        if time.monotonic() > deadline:
            raise AssertionError(
                f"the journal in the panel stopped at {panel.inner_text()!r}"
            )

        sandbox_chat.page.wait_for_timeout(200)

    if last in first:
        raise AssertionError(
            f"the panel opened after the call was over, nothing grew: {first!r}"
        )

    sandbox_chat.await_idle(timeout_sec=TURN_TIMEOUT_SEC)

    log = sandbox_stand.log_path.read_text(encoding="utf-8", errors="replace")
    lines = log.splitlines()[mark:]
    listened = [line for line in lines if "journal watch:" in line]
    if not any("listens on" in line for line in listened):
        raise AssertionError(f"the panel watch listens on the journal: {listened}")
    if any("polled" in line or "falls back to polling" in line for line in listened):
        raise AssertionError(
            f"the panel watch did not fall back to polling: {listened}"
        )
