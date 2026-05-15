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
    assert out.messages == ()


def test_no_op_when_last_is_assistant_text(
    make_turn_state: Callable[..., TurnState],
):
    state = make_turn_state(
        UserMessage(content="привет"),
        AssistantMessage(content="ответ"),
    )
    out = RememberUserQueryReducer().apply(state)
    assert out.messages == state.messages


def test_no_op_when_last_is_user_message(
    make_turn_state: Callable[..., TurnState],
):
    state = make_turn_state(
        UserMessage(content="первый"),
        AssistantMessage(content="..."),
        UserMessage(content="второй"),
    )
    out = RememberUserQueryReducer().apply(state)
    assert out.messages == state.messages


def test_appends_reminder_after_tool_result(
    make_turn_state: Callable[..., TurnState],
    make_tool_result_message: Callable[..., ToolResultMessage],
):
    state = make_turn_state(
        UserMessage(content="посчитай 2+2"),
        AssistantMessage(content="вызываю калькулятор"),
        make_tool_result_message(text="4"),
    )
    out = RememberUserQueryReducer().apply(state)
    assert len(out.messages) == 4
    last = out.messages[-1]
    assert isinstance(last, UserMessage)
    assert last.content == (
        RememberUserQueryReducer.DEFAULT_PREFIX + "посчитай 2+2"
    )


def test_uses_last_user_message_when_multiple(
    make_turn_state: Callable[..., TurnState],
    make_tool_result_message: Callable[..., ToolResultMessage],
):
    state = make_turn_state(
        UserMessage(content="старая задача"),
        AssistantMessage(content="..."),
        UserMessage(content="новая задача"),
        AssistantMessage(content="..."),
        make_tool_result_message(),
    )
    out = RememberUserQueryReducer().apply(state)
    last = out.messages[-1]
    assert isinstance(last, UserMessage)
    assert last.content.endswith("новая задача")


def test_no_op_when_no_user_messages_at_all(
    make_turn_state: Callable[..., TurnState],
    make_tool_result_message: Callable[..., ToolResultMessage],
):
    state = make_turn_state(
        AssistantMessage(content="..."),
        make_tool_result_message(),
    )
    out = RememberUserQueryReducer().apply(state)
    assert out.messages == state.messages


def test_custom_prefix_used(
    make_turn_state: Callable[..., TurnState],
    make_tool_result_message: Callable[..., ToolResultMessage],
):
    state = make_turn_state(
        UserMessage(content="X"),
        make_tool_result_message(),
    )
    out = RememberUserQueryReducer(prefix="REMIND: ").apply(state)
    last = out.messages[-1]
    assert isinstance(last, UserMessage)
    assert last.content == "REMIND: X"


def test_reducer_id_and_priority_defaults():
    r = RememberUserQueryReducer()
    assert r.id() == RememberUserQueryReducer.ID
    assert r.priority() == 35
