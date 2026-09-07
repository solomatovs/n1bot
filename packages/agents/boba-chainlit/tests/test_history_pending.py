"""История треда посреди параллельной пачки вызовов: ответ уже доработавшего
инструмента лежит в pending writes checkpoint'а, и лента обязана его показать.
Настоящие create_agent, InMemorySaver и TranscriptFeed, модель по сценарию.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Any

import pytest
from chainlit.step import StepDict
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver

from boba.chainlit.agent.flow import GraphSpec, PlainGraphBuilder
from boba.chainlit.chat.history import CheckpointMessages, TranscriptFeed
from boba.chainlit.domain.fields import StepField
from boba.chainlit.infra.providers import build_history_view
from boba.chainlit.rendering.chat_view import StepKind

pytestmark = pytest.mark.anyio

THREAD = "55555555-5555-5555-5555-555555555555"
CONFIG = RunnableConfig(configurable={"thread_id": THREAD})
FAST_CALL = "call_fast"
SLOW_CALL = "call_slow"
WAIT_SEC = 5.0


class ScriptedChat(GenericFakeChatModel):
    """Модель по сценарию: bind_tools у фейка не реализован."""

    def bind_tools(self, tools: Any, **kwargs: Any) -> Any:
        return self


class Gate:
    """Задерживает медленный инструмент, пока тест не посмотрит историю."""

    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.fast_done = asyncio.Event()


GATE = Gate()


@tool
async def fast_index(space: str) -> str:
    """Быстрая индексация."""
    GATE.fast_done.set()
    return f"indexed {space}"


@tool
async def slow_index(space: str) -> str:
    """Медленная индексация."""
    await GATE.release.wait()
    return f"indexed {space}"


def _graph(saver: InMemorySaver):
    calls = AIMessage(
        content="",
        tool_calls=[
            {"name": "fast_index", "args": {"space": "A"}, "id": FAST_CALL},
            {"name": "slow_index", "args": {"space": "B"}, "id": SLOW_CALL},
        ],
    )
    chat = ScriptedChat(messages=iter([calls, AIMessage(content="both indexed")]))
    spec = GraphSpec(
        chat=chat,
        tools=[fast_index, slow_index],
        system_prompt="index everything",
        checkpointer=saver,
        history=build_history_view(frozenset({"fast_index", "slow_index"}), 30),
    )
    return PlainGraphBuilder().build(spec)


def _tool_steps(steps: Sequence[StepDict]) -> list[str]:
    """Имена tool-шагов ленты без значка статуса перед именем."""
    names: list[str] = []
    for step in steps:
        if step.get(StepField.TYPE) != StepKind.TOOL.value:
            continue

        title = str(step.get(StepField.NAME, ""))
        names.append(title.rsplit(" ", maxsplit=1)[-1])

    return names


async def test_finished_call_of_a_running_batch_is_in_the_history() -> None:
    saver = InMemorySaver()
    graph = _graph(saver)
    feed = TranscriptFeed(CheckpointMessages(saver))
    question = HumanMessage(content="index A and B", id="q1")

    run = asyncio.create_task(graph.ainvoke({"messages": [question]}, config=CONFIG))
    await asyncio.wait_for(GATE.fast_done.wait(), WAIT_SEC)
    await asyncio.sleep(0.2)

    messages = await CheckpointMessages(saver).load(THREAD)
    replies = [m.tool_call_id for m in messages if isinstance(m, ToolMessage)]
    assert replies == [FAST_CALL]

    steps = await feed.steps(THREAD, "user")
    assert _tool_steps(steps) == ["fast_index"]

    GATE.release.set()
    await asyncio.wait_for(run, WAIT_SEC)

    settled = await CheckpointMessages(saver).load(THREAD)
    replies = [m.tool_call_id for m in settled if isinstance(m, ToolMessage)]
    assert sorted(replies) == [FAST_CALL, SLOW_CALL]

    steps = await feed.steps(THREAD, "user")
    assert sorted(_tool_steps(steps)) == ["fast_index", "slow_index"]
