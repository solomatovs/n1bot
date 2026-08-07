"""Контракт отрисовки: live-ход и повтор из истории дают одинаковые шаги.

Ключи контракта: контейнер и ответ — id вопроса, thinking — id AIMessage,
tool/chart — tool_call_id. Любое расхождение id ломает resume: вкладка,
открывшая тред посреди хода, получает дубли шагов.
"""

from __future__ import annotations

import asyncio
from typing import Any, cast
from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, LLMResult

from boba.chainlit.chat.agent_tracer import AgentTracer
from boba.chainlit.chat.transcript import ConversationTranscript
from boba.chainlit.rendering.chat_view import ChatView, RecordingSink, StepRole
from chainlit.context import ChainlitContext, context_var

THREAD = "22222222-2222-2222-2222-222222222222"
TURN_KEY = "human-msg-1"
AI_ID = "ai-msg-1"
CALL_ID = "call-1"
TOOL_MSG_ID = "tool-msg-1"
ANSWER_ID = "ai-msg-2"


@pytest.fixture(autouse=True)
def chainlit_context() -> None:
    """Трасеру нужен контекст только чтобы восстановить его в коллбэках."""
    context_var.set(cast("ChainlitContext", object()))


def run(coro: Any) -> Any:
    return asyncio.run(coro)


class TestStepContract:
    """Идентификаторы шагов детерминированы и совпадают у live и replay."""

    REASONING = "думаю над ответом"

    async def _live(self) -> RecordingSink:
        """Ход глазами трейсера: reasoning, инструмент, завершение."""
        sink = RecordingSink()
        view = ChatView(THREAD, sink, user_name="tester")
        view.begin_turn(TURN_KEY)
        tracer = AgentTracer(view)

        llm_run = uuid4()
        await tracer.on_llm_start({}, [""], run_id=llm_run)
        result = LLMResult(
            generations=[
                [
                    ChatGeneration(
                        message=AIMessage(
                            content="",
                            id=AI_ID,
                            additional_kwargs={"reasoning_content": self.REASONING},
                        )
                    )
                ]
            ]
        )
        await tracer.on_llm_end(result, run_id=llm_run)

        tool_run = uuid4()
        await tracer.on_tool_start(
            {"name": "demo"},
            "{'x': 1}",
            run_id=tool_run,
            inputs={"x": 1},
            tool_call_id=CALL_ID,
        )
        await tracer.on_tool_end(
            ToolMessage(
                content="hi",
                tool_call_id=CALL_ID,
                id=TOOL_MSG_ID,
                artifact={"kind": "text", "text": "hi"},
            ),
            run_id=tool_run,
        )
        return sink

    async def _replay(self) -> RecordingSink:
        """Тот же ход, восстановленный из истории checkpointer'а."""
        sink = RecordingSink()
        view = ChatView(THREAD, sink, user_name="tester")
        messages = [
            HumanMessage(content="вопрос", id=TURN_KEY),
            AIMessage(
                content="",
                id=AI_ID,
                additional_kwargs={"reasoning_content": self.REASONING},
                tool_calls=[{"name": "demo", "args": {"x": 1}, "id": CALL_ID}],
            ),
            ToolMessage(
                content="hi",
                tool_call_id=CALL_ID,
                id=TOOL_MSG_ID,
                artifact={"kind": "text", "text": "hi"},
            ),
            AIMessage(content="ответ", id=ANSWER_ID),
        ]
        await ConversationTranscript(messages, view).replay()
        return sink

    @staticmethod
    def _by_id(sink: RecordingSink) -> dict[str, dict[str, Any]]:
        steps: dict[str, dict[str, Any]] = {}
        for step in sink.steps:
            steps[str(step.get("id"))] = dict(step)
        return steps

    def test_live_ids_match_replay(self) -> None:
        live = self._by_id(run(self._live()))
        replay = self._by_id(run(self._replay()))

        # live не рисует вопрос (его шлёт фронт) и ответ (он идёт стримом)
        missing = set(live) - set(replay)
        assert not missing, f"live даёт шаги, которых нет в истории: {missing}"

    def test_every_step_is_addressable(self) -> None:
        live = self._by_id(run(self._live()))

        container_id = ChatView.derive_id(THREAD, TURN_KEY, StepRole.PROCESS)
        thinking_id = ChatView.derive_id(THREAD, AI_ID, StepRole.THINKING)
        tool_id = ChatView.derive_id(THREAD, CALL_ID, StepRole.TOOL)
        assert set(live) == {container_id, thinking_id, tool_id}

    def test_children_stay_under_the_same_container(self) -> None:
        live = self._by_id(run(self._live()))
        replay = self._by_id(run(self._replay()))
        container_id = ChatView.derive_id(THREAD, TURN_KEY, StepRole.PROCESS)

        for steps in (live, replay):
            assert container_id is not None
            assert steps[container_id].get("parentId") is None
            children = []
            for step in steps.values():
                if step.get("parentId") is not None:
                    children.append(step.get("parentId"))
            assert set(children) == {container_id}

    def test_answer_id_matches_stream_target(self) -> None:
        """stream_token дописывает по id: ответ истории обязан совпасть с live."""
        replay = self._by_id(run(self._replay()))
        answer_id = ChatView.derive_id(THREAD, TURN_KEY, StepRole.ANSWER)
        assert answer_id in replay
        assert replay[answer_id].get("output") == "ответ"
