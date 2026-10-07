"""Кнопка живого вывода видна на бегущем шаге инструмента, а не после него.

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
from boba.stand.ui.stand import StandApp, StandConfig, StandProcess, free_port

pytestmark = pytest.mark.ui

BOOT_TIMEOUT_SEC = 300.0
TURN_TIMEOUT_SEC = 120.0
BUTTON = '[aria-label="Show tool output"]'
POLL_SEC = 4.0


@pytest.fixture(scope="module")
def sandbox_stand(
    stand_workdir: Path, llm_port: int, fake_llm: None, stand_database: str
) -> Iterator[StandProcess]:
    config = StandConfig(
        workdir=stand_workdir / "sandbox-stream",
        app=StandApp.CHAINLIT,
        app_port=free_port(),
        llm_port=llm_port,
        db_name=stand_database,
        url_prefix="/boba-stream",
        sandbox=True,
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
    request = {"name": "bash", "arguments": {"command": command}}
    return f"{ScenarioName.CALL.value} {json.dumps(request, ensure_ascii=False)}"


def _button_appears(chat: ChatPage, within_sec: float) -> bool:
    deadline = time.monotonic() + within_sec
    while time.monotonic() < deadline:
        if chat.page.locator(BUTTON).count() > 0:
            return True

        chat.page.wait_for_timeout(200)

    return False


def test_button_appears_when_the_call_is_over(sandbox_chat: ChatPage) -> None:
    """Журнал вызова читается по адресу из его итога: пока вызов идёт, шаг
    показывает ход работы без кнопки журнала, кнопка встаёт с итогом."""
    sandbox_chat.ask(_bash_call("sleep 8; echo stream-probe"))
    sandbox_chat.await_step(StepKind.RUN, timeout_ms=TURN_TIMEOUT_SEC * 1000)
    sandbox_chat.expand_process()
    sandbox_chat.await_step(StepKind.TOOL, timeout_ms=TURN_TIMEOUT_SEC * 1000)
    sandbox_chat.expand_step(StepKind.TOOL)

    seen_running = _button_appears(sandbox_chat, POLL_SEC)

    sandbox_chat.await_idle(timeout_sec=TURN_TIMEOUT_SEC)

    if seen_running:
        raise AssertionError(
            "stream button is shown before the call has a journal address\n"
            + sandbox_chat.log.describe()
        )

    if not _button_appears(sandbox_chat, POLL_SEC):
        raise AssertionError(
            "stream button is absent on the finished tool step\n"
            + sandbox_chat.log.describe()
        )
