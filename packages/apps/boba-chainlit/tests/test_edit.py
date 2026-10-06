"""Тесты отката треда при правке вопроса."""

from __future__ import annotations

import asyncio
from typing import Any, ClassVar, cast

import pytest
from chainlit.data.base import BaseDataLayer
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import START, MessagesState, StateGraph
from langgraph.graph.state import CompiledStateGraph

from boba.chainlit.agent.bridge import LangchainMessages
from boba.chainlit.agent.history import GraphAgent
from boba.chainlit.chat.dialog import Attachment, DialogMessage
from boba.chainlit.chat.history import RewindPlan, ThreadRewind
from boba.chainlit.rendering.chat_view import ChatView, StepRole

THREAD = "11111111-1111-1111-1111-111111111111"


@pytest.fixture(autouse=True)
def chainlit_context() -> None:
    pass


def turn(
    question_id: str, answer_id: str, call_id: str | None = None
) -> list[BaseMessage]:
    messages: list[BaseMessage] = [HumanMessage(content="q", id=question_id)]
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


def _graph() -> CompiledStateGraph:
    graph = StateGraph(MessagesState)
    graph.add_node("noop", lambda state: {})
    graph.add_edge(START, "noop")
    return graph.compile(checkpointer=InMemorySaver())


class _ElementSink:
    """Слой данных в тестах: фиксирует только удаления вложений."""

    def __init__(self) -> None:
        self.deleted: list[str] = []

    async def delete_element(self, element_id: str, thread_id: str) -> None:
        self.deleted.append(element_id)


def _dialog(messages: list[BaseMessage]) -> list[DialogMessage]:
    """История так, как её отдаёт чату агент сессии."""
    return list(LangchainMessages().dialog(messages))


def _plan(messages: list[BaseMessage], message_id: str) -> RewindPlan:
    rewind = ThreadRewind(
        GraphAgent(_graph()), cast("BaseDataLayer", _ElementSink()), THREAD
    )

    return rewind.plan(_dialog(messages), message_id)


async def _stored(graph: CompiledStateGraph) -> list[BaseMessage]:
    """Сообщения треда, как они лежат в состоянии графа."""
    state = await graph.aget_state(RunnableConfig(configurable={"thread_id": THREAD}))

    return list(state.values["messages"])


class TestRewindPlan:
    def test_last_turn_is_truncated(self) -> None:
        messages = turn("q1", "a1")
        plan = _plan(messages, "q1")
        if list(plan.remove_ids) != ["a1"]:
            raise AssertionError('plan.remove_ids == ["a1"]')
        if list(plan.element_ids) != []:
            raise AssertionError("plan.element_ids == []")

    def test_middle_turn_drops_everything_after(self) -> None:
        messages = turn("q1", "a1") + turn("q2", "a2")
        plan = _plan(messages, "q1")
        if list(plan.remove_ids) != ["a1", "q2", "a2"]:
            raise AssertionError('plan.remove_ids == ["a1", "q2", "a2"]')

    def test_chart_elements_are_collected(self) -> None:
        messages = turn("q1", "a1", call_id="call_1")
        plan = _plan(messages, "q1")
        if not (
            list(plan.element_ids)
            == [ChatView.derive_id(THREAD, "call_1", StepRole.ELEMENT)]
        ):
            raise AssertionError('plan.element_ids == [ ChatView.derive_id(THREAD, "c…')
        if "a1-tool" not in plan.remove_ids:
            raise AssertionError('"a1-tool" in plan.remove_ids')

    def test_nothing_after_question(self) -> None:
        plan = _plan([HumanMessage(content="q", id="q1")], "q1")
        if plan:
            raise AssertionError("not plan")

    def test_unknown_question_changes_nothing(self) -> None:
        plan = _plan(turn("q1", "a1"), "нет-такого")
        if plan:
            raise AssertionError("not plan")


class TestPrefix:
    def test_prefix_ends_before_the_question(self) -> None:
        messages = turn("q1", "a1") + turn("q2", "a2")

        kept = GraphAgent.prefix(messages, "q2")

        if [m.id for m in kept] != ["q1", "a1"]:
            raise AssertionError('[m.id for m in kept] == ["q1", "a1"]')

    def test_first_question_gives_empty_prefix(self) -> None:
        if GraphAgent.prefix(turn("q1", "a1"), "q1") != []:
            raise AssertionError('GraphAgent.prefix(turn("q1", "a1"), "q1") == []')


class TestApplyOnRealGraph:
    """Правка против настоящего графа с checkpointer: канал переписывается.

    Точечный RemoveMessage падал, когда прерванный ход оставлял pending
    writes; полная перепись канала от них не зависит.
    """

    def _rewind(self, graph: CompiledStateGraph) -> tuple[ThreadRewind, _ElementSink]:
        sink = _ElementSink()
        rewind = ThreadRewind(GraphAgent(graph), cast("BaseDataLayer", sink), THREAD)
        return rewind, sink

    @staticmethod
    def run(coro: Any) -> Any:
        return asyncio.run(coro)

    def test_edit_replaces_the_tail(self) -> None:
        async def scenario() -> tuple[list[Any], list[str]]:
            graph = _graph()
            rewind, sink = self._rewind(graph)
            config = RunnableConfig(configurable={"thread_id": THREAD})
            history = turn("q1", "a1", call_id="call_1") + turn("q2", "a2")
            await graph.ainvoke({"messages": history}, config)

            if await rewind.is_edit("q1") is not True:
                raise AssertionError('await rewind.is_edit("q1") is True')
            await rewind.apply("q1", "новый вопрос")

            return await _stored(graph), sink.deleted

        messages, deleted = self.run(scenario())

        if [m.id for m in messages] != ["q1"]:
            raise AssertionError('[m.id for m in messages] == ["q1"]')
        if messages[0].content != "новый вопрос":
            raise AssertionError('messages[0].content == "новый вопрос"')
        if deleted != [ChatView.derive_id(THREAD, "call_1", StepRole.ELEMENT)]:
            raise AssertionError('deleted == [ChatView.derive_id(THREAD, "call_1", St…')

    def test_edit_of_a_middle_question_keeps_the_prefix(self) -> None:
        async def scenario() -> list[Any]:
            graph = _graph()
            rewind, _ = self._rewind(graph)
            config = RunnableConfig(configurable={"thread_id": THREAD})
            history = turn("q1", "a1") + turn("q2", "a2")
            await graph.ainvoke({"messages": history}, config)

            await rewind.apply("q2", "правка второго")

            return await _stored(graph)

        messages = self.run(scenario())

        if [m.id for m in messages] != ["q1", "a1", "q2"]:
            raise AssertionError('[m.id for m in messages] == ["q1", "a1", "q2"]')
        if messages[-1].content != "правка второго":
            raise AssertionError('messages[-1].content == "правка второго"')


class TestEditKeepsAttachments:
    """Правка меняет текст вопроса, а файлы, приложенные к нему, остаются с ним."""

    STORED: ClassVar[dict[str, list[dict[str, str]]]] = {
        "attachments": [{"name": "note.txt", "path": "/workspace/t-1/upload/note.txt"}]
    }
    """Вложения, как они лежат в additional_kwargs вопроса в checkpoint'е."""

    ATTACHMENTS: ClassVar[tuple[Attachment, ...]] = (
        Attachment(name="note.txt", path="/workspace/t-1/upload/note.txt"),
    )

    def test_plan_carries_the_question_attachments(self) -> None:
        history = turn("q1", "a1")
        history[0].additional_kwargs = dict(self.STORED)

        plan = _plan(history, "q1")

        assert tuple(plan.attachments) == self.ATTACHMENTS

    def test_apply_keeps_the_question_attachments(self) -> None:
        async def scenario() -> list[Any]:
            graph = _graph()
            rewind = ThreadRewind(
                GraphAgent(graph), cast("BaseDataLayer", _ElementSink()), THREAD
            )
            config = RunnableConfig(configurable={"thread_id": THREAD})
            history = turn("q1", "a1")
            history[0].additional_kwargs = dict(self.STORED)
            await graph.ainvoke({"messages": history}, config)

            plan = await rewind.apply("q1", "что тут теперь?")
            assert tuple(plan.attachments) == self.ATTACHMENTS

            return await _stored(graph)

        messages = asyncio.run(scenario())

        assert [m.id for m in messages] == ["q1"]
        assert messages[0].content == "что тут теперь?"
        assert messages[0].additional_kwargs == self.STORED
