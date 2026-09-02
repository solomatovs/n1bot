"""Контракт отрисовки: live-ход и повтор из истории дают одинаковые шаги.

Ключи контракта: контейнер и ответ — id вопроса, thinking — id AIMessage,
tool/chart — tool_call_id. Любое расхождение id ломает resume: вкладка,
открывшая тред посреди хода, получает дубли шагов.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from typing import Any, cast
from uuid import uuid4

import pytest
from chainlit.context import ChainlitContext, context_var
from chainlit_stand import RecordedTurn
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, LLMResult

from boba.chainlit.chat.history import ConversationTranscript
from boba.chainlit.chat.tracing import AgentTracer
from boba.chainlit.chat.turn import TurnState
from boba.chainlit.rendering.chat_view import ChatView, RecordingSink, StepRole

THREAD = "22222222-2222-2222-2222-222222222222"
TURN_KEY = "human-msg-1"
AI_ID = "ai-msg-1"
CALL_ID = "call-1"
TOOL_MSG_ID = "tool-msg-1"
ANSWER_ID = "ai-msg-2"


@pytest.fixture(autouse=True)
def chainlit_context() -> Iterator[None]:
    """Трасеру нужен контекст только чтобы восстановить его в коллбэках."""
    token = context_var.set(cast("ChainlitContext", object()))
    yield
    context_var.reset(token)


def run(coro: Any) -> Any:
    return asyncio.run(coro)


class TestStepContract:
    """Идентификаторы шагов детерминированы и совпадают у live и replay."""

    REASONING = "думаю над ответом"

    async def _live(self) -> RecordingSink:
        """Ход глазами трейсера: reasoning, инструмент, завершение."""
        turn = RecordedTurn.recording(THREAD, TURN_KEY)
        sink = turn.recording_sink
        tracer = AgentTracer(turn.feed, TurnState())

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
        if missing:
            raise AssertionError(f"live даёт шаги, которых нет в истории: {missing}")

    def test_every_step_is_addressable(self) -> None:
        live = self._by_id(run(self._live()))

        container_id = ChatView.derive_id(THREAD, TURN_KEY, StepRole.PROCESS)
        thinking_id = ChatView.derive_id(THREAD, AI_ID, StepRole.THINKING)
        tool_id = ChatView.derive_id(THREAD, CALL_ID, StepRole.TOOL)
        if set(live) != {container_id, thinking_id, tool_id}:
            raise AssertionError("set(live) == {container_id, thinking_id, tool_id}")

    def test_children_stay_under_the_same_container(self) -> None:
        live = self._by_id(run(self._live()))
        replay = self._by_id(run(self._replay()))
        container_id = ChatView.derive_id(THREAD, TURN_KEY, StepRole.PROCESS)

        for steps in (live, replay):
            if container_id is None:
                raise AssertionError("container_id is not None")
            if steps[container_id].get("parentId") is not None:
                raise AssertionError('steps[container_id].get("parentId") is None')
            children = []
            for step in steps.values():
                if step.get("parentId") is not None:
                    children.append(step.get("parentId"))
            if set(children) != {container_id}:
                raise AssertionError("set(children) == {container_id}")

    def test_replayed_names_match_live(self) -> None:
        """Заголовок шага («✓ demo», «✓ process...») живёт в name и обязан совпасть.

        «Кружка сверху» в live-вкладке — это то же, что кружок resumed-вкладки:
        status using/used run-шага. Несовпадение name означало бы расхождение
        лент — на него и смотрим. end — wall-clock завершения, в двух прогонах
        он разный; сравниваем только признак завершённости (start == end).
        """
        live = self._by_id(run(self._live()))
        replay = self._by_id(run(self._replay()))
        shared = set(live) & set(replay)
        for step_id in shared:
            live_step = live[step_id]
            replay_step = replay[step_id]
            if live_step.get("name") != replay_step.get("name"):
                raise AssertionError('live_step.get("name") == replay_step.get("name")')
            if live_step.get("output") != replay_step.get("output"):
                raise AssertionError('live_step.get("output") == replay_step.get("out…')
            if live_step.get("parentId") != replay_step.get("parentId"):
                raise AssertionError('live_step.get("parentId") == replay_step.get("p…')
            if live_step.get("isError") != replay_step.get("isError"):
                raise AssertionError('live_step.get("isError") == replay_step.get("is…')
            if live_step.get("type") == "run":
                # контейнер держится весь ход: start есть, end нет — он живой
                if live_step.get("end") is not None:
                    raise AssertionError('live_step.get("end") is None')
                if replay_step.get("end") is not None:
                    raise AssertionError('replay_step.get("end") is None')
                continue
            # завершённый шаг не должен выглядеть живым: end есть и start == end
            # (иначе фронт рисует loading-cursor)
            if live_step.get("end") is None:
                raise AssertionError('live_step.get("end") is not None')
            if replay_step.get("end") is None:
                raise AssertionError('replay_step.get("end") is not None')
            if live_step.get("start") != live_step.get("end"):
                raise AssertionError('live_step.get("start") == live_step.get("end")')
            if replay_step.get("start") != replay_step.get("end"):
                raise AssertionError('replay_step.get("start") == replay_step.get("en…')

    def test_answer_id_matches_stream_target(self) -> None:
        """stream_token дописывает по id: ответ истории обязан совпасть с live."""
        replay = self._by_id(run(self._replay()))
        answer_id = ChatView.derive_id(THREAD, TURN_KEY, StepRole.ANSWER)
        if answer_id not in replay:
            raise AssertionError("answer_id in replay")
        if replay[answer_id].get("output") != "ответ":
            raise AssertionError('replay[answer_id].get("output") == "ответ"')


class TestSpendSurvivesReplay:
    """Расход токенов хранится в AIMessage: пересборка истории подписывает шаги."""

    REASONING = "прикинул объём"

    async def _replay(self) -> RecordingSink:
        sink = RecordingSink()
        view = ChatView(THREAD, sink, user_name="tester")
        messages = [
            HumanMessage(content="вопрос", id=TURN_KEY),
            AIMessage(
                content="ответ",
                id=AI_ID,
                additional_kwargs={"reasoning_content": self.REASONING},
                usage_metadata={
                    "input_tokens": 10856,
                    "output_tokens": 400,
                    "total_tokens": 11256,
                    "output_token_details": {"reasoning": 305},
                },
            ),
        ]
        await ConversationTranscript(messages, view).replay()
        return sink

    def test_thinking_and_container_carry_the_spend(self) -> None:
        sink = run(self._replay())

        names: dict[str, str] = {}
        for step in sink.steps:
            names[str(step.get("id"))] = str(step.get("name"))

        thinking_id = str(ChatView.derive_id(THREAD, AI_ID, StepRole.THINKING))
        if names[thinking_id] != "○ thinking · in: 10.9k, out: 400 (305 reasoning)":
            raise AssertionError(f"шаг рассуждений: {names[thinking_id]!r}")

        container_id = str(ChatView.derive_id(THREAD, TURN_KEY, StepRole.PROCESS))
        if names[container_id] != "process... · in: 10.9k, out: 400 (305 reasoning)":
            raise AssertionError(f"контейнер: {names[container_id]!r}")
