"""Модель связывает потоковые инструменты каналами в одном ответе.

Агентный цикл create_agent с заскриптованной моделью: модель одним ответом
зовёт источник, трансформ и приёмники, назвав каналы в полях out/feed/feeds.
StreamGroupMiddleware строит группу по ответу, ToolNode исполняет вызовы
одновременно, обвязка StreamCallHooks ставит каждый на его место. Тела —
настоящие субпроцессы инструментов стенда.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool
from langchain_core.utils.function_calling import convert_to_openai_tool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph.state import CompiledStateGraph
from pydantic import SecretStr

from boba.chainlit.agent.flow import GraphSpec, PlainGraphBuilder, StreamGroupMiddleware
from boba.chainlit.infra.providers import build_history_view
from boba.runtime.plugins import ToolBridge
from boba.stand_core import fake_toolmod
from boba.stand_core.fake_toolmod import FakeConfig
from boba.toolkit.entry import ToolMain
from boba.toolkit.result import FailureResult, GroupFailureResult, ToolArtifact
from boba.toolkit.wrap import ToolProcessWrap
from boba.toolrun.call_id import ToolCallIdField
from boba.toolrun.errors import ToolErrorGuard
from boba.toolrun.injected import InjectedConfig
from boba.toolrun.process import ProcessLauncherConfig, ProcessToolCaller
from boba.toolrun.stream_calls import (
    StreamCallHooks,
    StreamCallKind,
    StreamChannelFields,
    StreamGroups,
    StreamGroupsConfig,
)

CFG = FakeConfig(token=SecretStr("t0ken"), limit=5)
STREAM_CFG = StreamGroupsConfig(
    open_sec=20.0,
    stall_sec=10.0,
    poll_sec=0.1,
    pipe_bytes=1 << 20,
    pipe_bytes_max=1 << 20,
)
TIMINGS = STREAM_CFG.timings()
THREAD = RunnableConfig(configurable={"thread_id": "stream-channels"})
FINAL = "streams are wired"


@pytest.fixture(autouse=True)
def chainlit_context() -> None:
    """Контекст chainlit инструментам стенда не нужен: ленты у них нет."""


class ScriptedChat(GenericFakeChatModel):
    """Модель по сценарию: bind_tools у фейка не реализован."""

    def bind_tools(self, tools: Any, **kwargs: Any) -> Any:
        return self


class ChannelStand:
    """Инструменты стенда под той же цепочкой обвязок, что в загрузчике, и
    граф хода над ними."""

    def __init__(self, workdir: Path) -> None:
        self.workdir = workdir
        cfg = ProcessLauncherConfig(
            provider="process",
            workdir=str(workdir),
            timeout_sec=60.0,
            channel_limit_bytes=4_000_000,
            stderr_tail_bytes=8192,
            kill_grace_sec=0.5,
        )
        launcher = ProcessToolCaller("stream-channels", cfg)

        payloads = (
            fake_toolmod.fake_emit,
            fake_toolmod.fake_collect,
            fake_toolmod.fake_stream,
            fake_toolmod.fake_merge,
        )
        tools: list[BaseTool] = []
        for payload in payloads:
            tools.append(ToolBridge.as_structured_tool(payload.model_copy()))

        ToolProcessWrap.guard_all(ToolMain.toolset(*tools), launcher)

        def resolve(name: str, annotation: Any) -> object:
            return CFG

        InjectedConfig.bind_all(tools, resolve)
        StreamChannelFields(STREAM_CFG).attach_all(tools)
        ToolCallIdField.attach_all(tools)
        self.streams = StreamGroups(TIMINGS, tools)
        StreamCallHooks(self.streams).guard_all(tools)
        ToolErrorGuard().guard_all(tools)
        self.tools = tools

    def tool(self, name: str) -> BaseTool:
        for tool in self.tools:
            if tool.name == name:
                return tool

        raise AssertionError(f"no tool {name!r}")

    def graph(self, calls: Sequence[Mapping[str, Any]]) -> CompiledStateGraph:
        tool_calls: list[dict[str, Any]] = []
        for index, call in enumerate(calls):
            tool_calls.append({**call, "id": f"call_{index}", "type": "tool_call"})

        script: list[AIMessage | str] = [
            AIMessage(content="", tool_calls=tool_calls),
            AIMessage(content=FINAL),
        ]
        names: set[str] = set()
        for tool in self.tools:
            names.add(tool.name)

        spec = GraphSpec(
            chat=ScriptedChat(messages=iter(script), disable_streaming=True),
            tools=self.tools,
            system_prompt="wire the streams",
            checkpointer=InMemorySaver(),
            history=build_history_view(frozenset(names), 30),
            streams=StreamGroupMiddleware(self.streams),
        )
        return PlainGraphBuilder().build(spec)

    async def turn(self, calls: Sequence[Mapping[str, Any]]) -> dict[str, ToolMessage]:
        state = await self.graph(calls).ainvoke(
            {"messages": [HumanMessage(content="copy the stream")]}, config=THREAD
        )

        replies: dict[str, ToolMessage] = {}
        for message in state["messages"]:
            if isinstance(message, ToolMessage):
                replies[message.tool_call_id] = message

        assert state["messages"][-1].content == FINAL
        return replies


def _error(message: ToolMessage) -> FailureResult:
    artifact = ToolArtifact.revive(message.artifact)
    assert isinstance(artifact, FailureResult), message.content
    return artifact


def _ok(message: ToolMessage) -> str:
    artifact = ToolArtifact.revive(message.artifact)
    assert not isinstance(artifact, FailureResult), message.content
    return str(message.content)


def _collected(prefix: str, count: int, size: int) -> str:
    digest = hashlib.sha256()
    total = 0
    for seq in range(count):
        body = f"{prefix}{seq}".encode() + b"x" * size
        digest.update(body)
        total += len(body)

    return f"collected {count} frames {total} bytes {digest.hexdigest()[:16]}"


def _emit(out: str, count: int, size: int, *, fail: bool = False) -> dict[str, Any]:
    return {
        "name": "fake_emit",
        "args": {
            "prefix": "m",
            "count": count,
            "size": size,
            "fail_midway": fail,
            "out": out,
        },
    }


def _collect(feed: str, marker: str) -> dict[str, Any]:
    return {
        "name": "fake_collect",
        "args": {"marker": marker, "fail": False, "gated": True, "feed": feed},
    }


class TestChannelSchema:
    def test_ports_become_channel_names_for_the_model(self, tmp_path: Path) -> None:
        """Модель видит каналы строками и списками, портов и барьера не видит."""
        stand = ChannelStand(tmp_path)

        emit = convert_to_openai_tool(stand.tool("fake_emit"))["function"]["parameters"]
        collect = convert_to_openai_tool(stand.tool("fake_collect"))["function"][
            "parameters"
        ]
        merge = convert_to_openai_tool(stand.tool("fake_merge"))["function"][
            "parameters"
        ]

        assert emit["properties"]["out"]["type"] == "string"
        assert "out" in emit["required"]
        assert collect["properties"]["feed"]["type"] == "string"
        assert "group" not in collect["properties"]
        assert merge["properties"]["feeds"]["type"] == "array"
        assert "каждый канал приходит отдельным потоком" in str(
            merge["properties"]["feeds"]["description"]
        )


@pytest.mark.anyio
class TestModelWiresStreams:
    async def test_one_response_fans_a_source_out_to_two_sinks(
        self, tmp_path: Path
    ) -> None:
        stand = ChannelStand(tmp_path)

        replies = await stand.turn(
            [
                _emit("rows", 48, 65536),
                _collect("rows", "left"),
                _collect("rows", "right"),
            ]
        )

        expected = _collected("m", 48, 65536)
        assert "emitted 48" in _ok(replies["call_0"])
        assert _ok(replies["call_1"]).startswith(expected)
        assert _ok(replies["call_2"]).startswith(expected)
        assert (tmp_path / "left").read_text() == expected
        assert (tmp_path / "right").read_text() == expected

    async def test_one_response_builds_a_diamond(self, tmp_path: Path) -> None:
        stand = ChannelStand(tmp_path)

        replies = await stand.turn(
            [
                _emit("raw", 3, 0),
                {
                    "name": "fake_stream",
                    "args": {"prefix": "L", "feed": "raw", "out": "l"},
                },
                {
                    "name": "fake_stream",
                    "args": {"prefix": "R", "feed": "raw", "out": "r"},
                },
                {"name": "fake_merge", "args": {"feeds": ["l", "r"]}},
            ]
        )

        assert _ok(replies["call_3"]) == "merged 2:Lm0Lm1Lm2;Rm0Rm1Rm2|t0ken"

    async def test_broken_plan_refuses_every_call_of_the_response(
        self, tmp_path: Path
    ) -> None:
        """Два писателя одного канала: план отвергнут до запуска, каждый вызов
        ответа получает текст нарушения, ни один процесс не стартует."""
        stand = ChannelStand(tmp_path)

        replies = await stand.turn(
            [_emit("rows", 4, 0), _emit("rows", 4, 0), _collect("rows", "never")]
        )

        for key in ("call_0", "call_1", "call_2"):
            error = _error(replies[key])
            assert "channel 'rows' has two writers" in error.llm_view()
            assert error.error_kind == StreamCallKind.PLAN_REFUSED

        assert not (tmp_path / "never").exists()

    async def test_failed_writer_leaves_no_commit(self, tmp_path: Path) -> None:
        stand = ChannelStand(tmp_path)

        replies = await stand.turn(
            [_emit("rows", 40, 1024, fail=True), _collect("rows", "cut")]
        )

        for key in ("call_0", "call_1"):
            error = _error(replies[key])
            assert isinstance(error, GroupFailureResult), error
            assert error.origin is not None, error
            assert error.origin.call_id == "call_0", error.origin
            assert error.own == (key == "call_0"), error
            assert "nothing was committed" in error.llm_view()

        assert not (tmp_path / "cut").exists()
