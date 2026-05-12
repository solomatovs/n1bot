"""Юнит-тесты RememberUserQueryReducer."""

from __future__ import annotations

from boba.agent.turn.reducers import RememberUserQueryReducer
from boba.agent.turn.spec import TurnState
from boba.llm.models import AssistantMessage, ToolResultMessage, UserMessage
from boba.tools.domain import TextResult


def _state(*messages: object) -> TurnState:
    return TurnState(messages=tuple(messages))  # type: ignore[arg-type]


def _tool_result(call_id: str = "c1", text: str = "ok") -> ToolResultMessage:
    return ToolResultMessage(
        tool_call_id=call_id,
        result=TextResult(text=text),
    )


def test_empty_messages_unchanged():
    state = _state()
    out = RememberUserQueryReducer().apply(state)
    assert out.messages == ()


def test_no_op_when_last_is_assistant_text():
    state = _state(UserMessage(content="привет"), AssistantMessage(content="ответ"))
    out = RememberUserQueryReducer().apply(state)
    assert out.messages == state.messages


def test_no_op_when_last_is_user_message():
    state = _state(
        UserMessage(content="первый"),
        AssistantMessage(content="..."),
        UserMessage(content="второй"),
    )
    out = RememberUserQueryReducer().apply(state)
    assert out.messages == state.messages


def test_appends_reminder_after_tool_result():
    state = _state(
        UserMessage(content="посчитай 2+2"),
        AssistantMessage(content="вызываю калькулятор"),
        _tool_result(text="4"),
    )
    out = RememberUserQueryReducer().apply(state)
    assert len(out.messages) == 4
    last = out.messages[-1]
    assert isinstance(last, UserMessage)
    assert last.content == (
        RememberUserQueryReducer.DEFAULT_PREFIX + "посчитай 2+2"
    )


def test_uses_last_user_message_when_multiple():
    state = _state(
        UserMessage(content="старая задача"),
        AssistantMessage(content="..."),
        UserMessage(content="новая задача"),
        AssistantMessage(content="..."),
        _tool_result(),
    )
    out = RememberUserQueryReducer().apply(state)
    last = out.messages[-1]
    assert isinstance(last, UserMessage)
    assert last.content.endswith("новая задача")


def test_no_op_when_no_user_messages_at_all():
    state = _state(AssistantMessage(content="..."), _tool_result())
    out = RememberUserQueryReducer().apply(state)
    assert out.messages == state.messages


def test_custom_prefix_used():
    state = _state(UserMessage(content="X"), _tool_result())
    out = RememberUserQueryReducer(prefix="REMIND: ").apply(state)
    last = out.messages[-1]
    assert isinstance(last, UserMessage)
    assert last.content == "REMIND: X"


def test_reducer_id_and_priority_defaults():
    r = RememberUserQueryReducer()
    assert r.id() == RememberUserQueryReducer.ID
    assert r.priority() == 35
