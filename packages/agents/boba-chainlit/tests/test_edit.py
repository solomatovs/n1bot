"""Тесты отката треда при правке вопроса."""

from __future__ import annotations

import asyncio
from typing import Any, cast

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import START, MessagesState, StateGraph
from langgraph.graph.state import CompiledStateGraph

from boba.chainlit.chat.edit import ThreadRewind
from boba.chainlit.rendering.chat_view import ChatView, StepRole
from chainlit.data.base import BaseDataLayer

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
        assert plan.element_ids == [
            ChatView.derive_id(THREAD, "call_1", StepRole.ELEMENT)
        ]
        assert "a1-tool" in plan.remove_ids

    def test_nothing_after_question(self) -> None:
        plan = ThreadRewind.plan([HumanMessage(content="q", id="q1")], "q1", THREAD)
        assert not plan

    def test_unknown_question_changes_nothing(self) -> None:
        plan = ThreadRewind.plan(turn("q1", "a1"), "нет-такого", THREAD)
        assert not plan


class TestPrefix:
    def test_prefix_ends_before_the_question(self) -> None:
        messages = turn("q1", "a1") + turn("q2", "a2")

        kept = ThreadRewind.prefix(messages, "q2")

        assert [m.id for m in kept] == ["q1", "a1"]

    def test_first_question_gives_empty_prefix(self) -> None:
        assert ThreadRewind.prefix(turn("q1", "a1"), "q1") == []


class _ElementSink:
    """Слой данных в тестах: фиксирует только удаления вложений."""

    def __init__(self) -> None:
        self.deleted: list[str] = []

    async def delete_element(self, element_id: str, thread_id: str) -> None:
        self.deleted.append(element_id)


class TestApplyOnRealGraph:
    """Правка против настоящего графа с checkpointer: канал переписывается.

    Точечный RemoveMessage падал, когда прерванный ход оставлял pending
    writes; полная перепись канала от них не зависит.
    """

    @staticmethod
    def _graph() -> CompiledStateGraph:
        graph = StateGraph(MessagesState)
        graph.add_node("noop", lambda state: {})
        graph.add_edge(START, "noop")
        return graph.compile(checkpointer=InMemorySaver())

    def _rewind(self, graph: CompiledStateGraph) -> tuple[ThreadRewind, _ElementSink]:
        sink = _ElementSink()
        rewind = ThreadRewind(graph, cast("BaseDataLayer", sink), THREAD)
        return rewind, sink

    @staticmethod
    def run(coro: Any) -> Any:
        return asyncio.run(coro)

    def test_edit_replaces_the_tail(self) -> None:
        async def scenario() -> tuple[list[Any], list[str]]:
            graph = self._graph()
            rewind, sink = self._rewind(graph)
            config = RunnableConfig(configurable={"thread_id": THREAD})
            history = turn("q1", "a1", call_id="call_1") + turn("q2", "a2")
            await graph.ainvoke({"messages": history}, config)

            assert await rewind.is_edit("q1") is True
            await rewind.apply("q1", "новый вопрос")

            return await rewind.messages(), sink.deleted

        messages, deleted = self.run(scenario())

        assert [m.id for m in messages] == ["q1"]
        assert messages[0].content == "новый вопрос"
        assert deleted == [ChatView.derive_id(THREAD, "call_1", StepRole.ELEMENT)]

    def test_edit_of_a_middle_question_keeps_the_prefix(self) -> None:
        async def scenario() -> list[Any]:
            graph = self._graph()
            rewind, _ = self._rewind(graph)
            config = RunnableConfig(configurable={"thread_id": THREAD})
            history = turn("q1", "a1") + turn("q2", "a2")
            await graph.ainvoke({"messages": history}, config)

            await rewind.apply("q2", "правка второго")

            return await rewind.messages()

        messages = self.run(scenario())

        assert [m.id for m in messages] == ["q1", "a1", "q2"]
        assert messages[-1].content == "правка второго"
