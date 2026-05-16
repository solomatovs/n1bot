"""Юнит-тесты RememberUserQueryReducer."""

from __future__ import annotations

from collections.abc import Callable

from boba.agent.turn.reducers import RememberUserQueryReducer
from boba.agent.turn.spec import TurnState
from boba.llm.models import AssistantMessage, ToolResultMessage, UserMessage


def test_empty_messages_unchanged(
    make_turn_state: Callable[..., TurnState],
):
    state = make_turn_state()
    out = RememberUserQueryReducer().apply(state)
    assert out.dialog_messages == ()


def test_no_op_when_last_is_assistant_text(
    make_turn_state: Callable[..., TurnState],
):
    state = make_turn_state(
        UserMessage.from_text("привет"),
        AssistantMessage.from_text("ответ"),
    )
    out = RememberUserQueryReducer().apply(state)
    assert out.dialog_messages == state.dialog_messages


def test_no_op_when_last_is_user_message(
    make_turn_state: Callable[..., TurnState],
):
    state = make_turn_state(
        UserMessage.from_text("первый"),
        AssistantMessage.from_text("..."),
        UserMessage.from_text("второй"),
    )
    out = RememberUserQueryReducer().apply(state)
    assert out.dialog_messages == state.dialog_messages


def test_appends_reminder_after_tool_result(
    make_turn_state: Callable[..., TurnState],
    make_tool_result_message: Callable[..., ToolResultMessage],
):
    state = make_turn_state(
        UserMessage.from_text("посчитай 2+2"),
        AssistantMessage.from_text("вызываю калькулятор"),
        make_tool_result_message(text="4"),
    )
    out = RememberUserQueryReducer().apply(state)
    assert len(out.dialog_messages) == 4
    last = out.dialog_messages[-1]
    assert isinstance(last, UserMessage)
    assert last.content == (RememberUserQueryReducer.DEFAULT_PREFIX + "посчитай 2+2")


def test_uses_last_user_message_when_multiple(
    make_turn_state: Callable[..., TurnState],
    make_tool_result_message: Callable[..., ToolResultMessage],
):
    state = make_turn_state(
        UserMessage.from_text("старая задача"),
        AssistantMessage.from_text("..."),
        UserMessage.from_text("новая задача"),
        AssistantMessage.from_text("..."),
        make_tool_result_message(),
    )
    out = RememberUserQueryReducer().apply(state)
    last = out.dialog_messages[-1]
    assert isinstance(last, UserMessage)
    assert last.content.endswith("новая задача")


def test_no_op_when_no_user_messages_at_all(
    make_turn_state: Callable[..., TurnState],
    make_tool_result_message: Callable[..., ToolResultMessage],
):
    state = make_turn_state(
        AssistantMessage.from_text("..."),
        make_tool_result_message(),
    )
    out = RememberUserQueryReducer().apply(state)
    assert out.dialog_messages == state.dialog_messages


def test_custom_prefix_used(
    make_turn_state: Callable[..., TurnState],
    make_tool_result_message: Callable[..., ToolResultMessage],
):
    state = make_turn_state(
        UserMessage.from_text("X"),
        make_tool_result_message(),
    )
    out = RememberUserQueryReducer(prefix="REMIND: ").apply(state)
    last = out.dialog_messages[-1]
    assert isinstance(last, UserMessage)
    assert last.content == "REMIND: X"


def test_reducer_id_and_priority_defaults():
    r = RememberUserQueryReducer()
    assert r.id() == RememberUserQueryReducer.ID
    assert r.priority() == 35
