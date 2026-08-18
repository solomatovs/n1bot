"""Кружок ожидания хода: он держится, пока ход идёт, и гаснет вместе с ним.

Кружок — сообщение ленты с zero-width space в тексте: фронт chainlit рисует
такой символ элементом `.loading-cursor`. Доказательство берётся из socket.io
(шаг пришёл и был снят) и из DOM (после хода мигать нечему).
"""

from __future__ import annotations

import pytest

from boba.chainlit.rendering.chat_view import TurnPulse
from ui.chat_page import ChatPage, StepKind
from ui.fake_llm import ScenarioName
from ui.socket_log import ChatEvent, SocketLog, StepField

pytestmark = pytest.mark.ui

CURSOR = ".loading-cursor"


def _pulse_id(log: SocketLog) -> str:
    """id шага-кружка: его узнают по zero-width space в выводе."""
    for frame in log.of_event(ChatEvent.NEW_MESSAGE):
        if not isinstance(frame.payload, dict):
            continue

        if frame.payload.get(StepField.OUTPUT.value) != TurnPulse.CONTENT:
            continue

        return frame.step_id

    raise AssertionError(f"no pulse step was sent\n{log.describe()}")


def _index_of(log: SocketLog, event: ChatEvent, step_id: str) -> int:
    """Позиция последнего кадра события для шага; -1 — такого кадра не было."""
    found = -1
    for index, frame in enumerate(log.frames):
        if frame.event is not event:
            continue

        if frame.step_id != step_id:
            continue

        found = index

    return found


class TestTurnPulse:
    """Ход с инструментом: кружок жив всё это время и снят по завершении."""

    def test_pulse_outlives_the_tool_call(self, chat: ChatPage) -> None:
        chat.ask(f"{ScenarioName.TOOL.value} please")
        chat.await_idle()

        pulse = _pulse_id(chat.log)
        removed_at = _index_of(chat.log, ChatEvent.DELETE_MESSAGE, pulse)
        if removed_at < 0:
            raise AssertionError(f"pulse is never removed\n{chat.log.describe()}")

        tool_at = -1
        for index, frame in enumerate(chat.log.frames):
            if frame.event is not ChatEvent.NEW_MESSAGE:
                continue

            if frame.step_type != StepKind.TOOL.value:
                continue

            tool_at = index

        if tool_at < 0:
            raise AssertionError(f"no tool step in the turn\n{chat.log.describe()}")

        if removed_at < tool_at:
            raise AssertionError(
                f"pulse died before the tool step\n{chat.log.describe()}"
            )

    def test_finished_turn_has_no_cursor(self, chat: ChatPage) -> None:
        chat.ask(f"{ScenarioName.TOOL.value} please")
        chat.await_idle()
        chat.page.wait_for_timeout(500)

        blinking = chat.page.locator(CURSOR).count()
        if blinking:
            raise AssertionError(f"{blinking} blinking cursors left after the turn")
