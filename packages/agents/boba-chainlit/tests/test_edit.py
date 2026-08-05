"""Тесты отката треда при правке вопроса."""

from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from boba.chainlit.chat.edit import ThreadRewind
from boba.chainlit.rendering.chat_view import ChatView

THREAD = "11111111-1111-1111-1111-111111111111"


@pytest.fixture(autouse=True)
def chainlit_context() -> None:
    pass


def turn(question_id: str, answer_id: str, call_id: str | None = None) -> list:
    messages: list = [HumanMessage(content="q", id=question_id)]
    if call_id:
        messages += [
            AIMessage(
                content="",
                id=f"{answer_id}-calls",
                tool_calls=[
                    {
                        "name": "visualize",
                        "args": {},
                        "id": call_id,
                        "type": "tool_call",
                    }
                ],
            ),
            ToolMessage(
                content="ok",
                id=f"{answer_id}-tool",
                name="visualize",
                tool_call_id=call_id,
            ),
        ]
    messages.append(AIMessage(content="a", id=answer_id))
    return messages


class TestRewindPlan:
    def test_last_turn_is_truncated(self) -> None:
        messages = turn("q1", "a1")
        plan = ThreadRewind.plan(messages, "q1", THREAD)
        assert plan.remove_ids == ["a1"]
        assert plan.element_ids == []

    def test_middle_turn_drops_everything_after(self) -> None:
        messages = turn("q1", "a1") + turn("q2", "a2")
        plan = ThreadRewind.plan(messages, "q1", THREAD)
        assert plan.remove_ids == ["a1", "q2", "a2"]

    def test_chart_elements_are_collected(self) -> None:
        messages = turn("q1", "a1", call_id="call_1")
        plan = ThreadRewind.plan(messages, "q1", THREAD)
        assert plan.element_ids == [ChatView.derive_id(THREAD, "call_1", "element")]
        assert "a1-tool" in plan.remove_ids

    def test_nothing_after_question(self) -> None:
        plan = ThreadRewind.plan([HumanMessage(content="q", id="q1")], "q1", THREAD)
        assert not plan

    def test_unknown_question_changes_nothing(self) -> None:
        plan = ThreadRewind.plan(turn("q1", "a1"), "нет-такого", THREAD)
        assert not plan
