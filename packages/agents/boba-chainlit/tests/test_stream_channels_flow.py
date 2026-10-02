"""Модель связывает потоковые инструменты одним вызовом workflow.

Агентный цикл create_agent с заскриптованной моделью: модель зовёт
инструмент workflow, его узлы — источник, трансформ и приёмники, каналы
названы в полях out/feed/feeds аргументов узлов. DagMiddleware перехватывает
вызов: DagCalls раскрывает его в узлы DAG, исполнитель запускает их
группой, вызов получает итоги узлов одним результатом. Тела — настоящие
субпроцессы инструментов стенда.
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

from boba.chainlit.agent.flow import GraphSpec, PlainGraphBuilder
from boba.chainlit.chat.history import CheckpointMessages, TranscriptFeed
from boba.chainlit.domain.fields import StepField
from boba.chainlit.infra.providers import build_history_view
from boba.chainlit.rendering.chat_view import StepKind
from boba.stand_core import fake_toolmod
from boba.stand_core.fake_toolmod import FakeConfig
from boba.toolkit.entry import ToolMain
from boba.toolkit.launcher import TappedCall, ToolCall, ToolLauncher
from boba.toolkit.protocol import ToolCommand
from boba.toolkit.result import (
    FailureResult,
    GroupFailureResult,
    ToolArtifact,
    WorkflowNodeResult,
    WorkflowResult,
)
from boba.toolkit.wrap import ToolProcessWrap
from boba.toolrun.bridge import ToolBridge
from boba.toolrun.call_id import ToolCallIdField
from boba.toolrun.dev_null import DevNullTool
from boba.toolrun.errors import ToolErrorGuard
from boba.toolrun.injected import InjectedConfig
from boba.toolrun.process import ProcessLauncherConfig, ProcessToolCaller
from boba.toolrun.stream_calls import (
    DagCalls,
    StreamCallKind,
    StreamChannelFields,
    StreamGroupsConfig,
    WorkflowTool,
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
THREAD_ID = "66666666-6666-6666-6666-666666666666"
THREAD = RunnableConfig(configurable={"thread_id": THREAD_ID})
FINAL = "streams are wired"


@pytest.fixture(autouse=True)
def chainlit_context() -> None:
    """Контекст chainlit инструментам стенда не нужен: ленты у них нет."""


class ScriptedChat(GenericFakeChatModel):
    """Модель по сценарию: bind_tools у фейка не реализован."""

    def bind_tools(self, tools: Any, **kwargs: Any) -> Any:
        return self


class RecordingLauncher(ToolLauncher):
    """Лаунчер стенда, запоминающий команды запущенных процессов."""

    def __init__(self, launcher: ToolLauncher) -> None:
        self._launcher = launcher
        self.started: list[tuple[str, ...]] = []

    def open(self, command: ToolCommand) -> ToolCall:
        self.started.append(command.argv)

        return self._launcher.open(command)

    def open_tap(self, command: ToolCommand) -> TappedCall:
        self.started.append(command.argv)

        return self._launcher.open_tap(command)


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
        launcher = RecordingLauncher(ProcessToolCaller("stream-channels", cfg))
        self._launcher = launcher

        payloads = (
            fake_toolmod.fake_echo,
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
        ToolErrorGuard().guard_all(tools)

        # сток хоста идёт без поля id вызова: снимать его в стенде некому
        drain = ToolBridge.as_structured_tool(DevNullTool.build())
        StreamChannelFields(STREAM_CFG).attach_all([drain])
        tools.append(drain)

        self.streams = DagCalls(tools, STREAM_CFG)
        self.tools = tools

    def started(self) -> list[tuple[str, ...]]:
        """Команды процессов инструментов, которые стенд запустил."""
        return list(self._launcher.started)

    def tool(self, name: str) -> BaseTool:
        for tool in self.tools:
            if tool.name == name:
                return tool

        raise AssertionError(f"no tool {name!r}")

    def graph(
        self, calls: Sequence[Mapping[str, Any]], saver: InMemorySaver
    ) -> CompiledStateGraph:
        tool_calls: list[dict[str, Any]] = []
        for index, call in enumerate(calls):
            tool_calls.append({**call, "id": f"call_{index}", "type": "tool_call"})

        script: list[AIMessage | str] = [
            AIMessage(content="", tool_calls=tool_calls),
            AIMessage(content=FINAL),
        ]

        spec = GraphSpec(
            chat=ScriptedChat(messages=iter(script), disable_streaming=True),
            tools=self.streams.model_tools(),
            system_prompt="wire the streams",
            checkpointer=saver,
            history=build_history_view(self.streams.history_names(), 30),
            calls=self.streams,
        )
        return PlainGraphBuilder().build(spec)

    async def turn(
        self, calls: Sequence[Mapping[str, Any]], saver: InMemorySaver | None = None
    ) -> dict[str, ToolMessage]:
        if saver is None:
            saver = InMemorySaver()

        state = await self.graph(calls, saver).ainvoke(
            {"messages": [HumanMessage(content="copy the stream", id="q1")]},
            config=THREAD,
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


def _nodes(message: ToolMessage) -> dict[str, WorkflowNodeResult]:
    """Итоги узлов вызова workflow по именам узлов."""
    artifact = ToolArtifact.revive(message.artifact)
    assert isinstance(artifact, WorkflowResult), message.content

    found: dict[str, WorkflowNodeResult] = {}
    for node in artifact.nodes:
        found[node.key] = node

    return found


def _ok(node: WorkflowNodeResult) -> str:
    assert not isinstance(node.result, FailureResult), node.content
    return node.content


def _failed(node: WorkflowNodeResult) -> FailureResult:
    assert isinstance(node.result, FailureResult), node.content
    return node.result


def _collected(prefix: str, count: int, size: int) -> str:
    digest = hashlib.sha256()
    total = 0
    for seq in range(count):
        body = f"{prefix}{seq}".encode() + b"x" * size
        digest.update(body)
        total += len(body)

    return f"collected {count} frames {total} bytes {digest.hexdigest()[:16]}"


def _workflow(**nodes: Mapping[str, Any]) -> dict[str, Any]:
    """Вызов workflow: имя узла — ключ, значение — инструмент и аргументы."""
    listed: list[dict[str, Any]] = []
    for key, node in nodes.items():
        listed.append({"key": key, "tool": node["tool"], "args": node["args"]})

    return {"name": WorkflowTool.NAME, "args": {"nodes": listed}}


def _emit(out: str, count: int, size: int, *, fail: bool = False) -> dict[str, Any]:
    return {
        "tool": "fake_emit",
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
        "tool": "fake_collect",
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

    def test_stream_tools_reach_the_model_only_inside_workflow(
        self, tmp_path: Path
    ) -> None:
        """Потоковые инструменты модели отдельно не отдаются: она видит один
        workflow, а в схеме его узла — выбор инструмента и его аргументы."""
        stand = ChannelStand(tmp_path)

        offered: list[str] = []
        for tool in stand.streams.model_tools():
            offered.append(tool.name)

        assert offered == ["fake_echo", WorkflowTool.NAME]

        workflow = stand.streams.model_tools()[-1]
        schema = convert_to_openai_tool(workflow)["function"]["parameters"]

        variants: dict[str, Any] = {}
        for variant in schema["properties"]["nodes"]["items"]["anyOf"]:
            variants[variant["properties"]["tool"]["const"]] = variant

        assert sorted(variants) == [
            "dev_null",
            "fake_collect",
            "fake_emit",
            "fake_merge",
            "fake_stream",
        ]
        assert sorted(variants["fake_emit"]["required"]) == ["args", "key", "tool"]

        emit_args = variants["fake_emit"]["properties"]["args"]
        assert emit_args["properties"]["out"]["type"] == "string"
        assert "boba_tool_call_id" not in emit_args["properties"]
        assert "$defs" not in str(schema)


@pytest.mark.anyio
class TestModelWiresStreams:
    async def test_one_workflow_fans_a_source_out_to_two_sinks(
        self, tmp_path: Path
    ) -> None:
        stand = ChannelStand(tmp_path)

        replies = await stand.turn(
            [
                _workflow(
                    src=_emit("rows", 48, 65536),
                    left=_collect("rows", "left"),
                    right=_collect("rows", "right"),
                )
            ]
        )

        reply = replies["call_0"]
        nodes = _nodes(reply)
        expected = _collected("m", 48, 65536)
        assert reply.status == "success"
        assert "emitted 48" in _ok(nodes["src"])
        assert _ok(nodes["left"]).startswith(expected)
        assert _ok(nodes["right"]).startswith(expected)
        assert nodes["src"].call_id == "call_0_0"
        assert "[left] fake_collect:" in str(reply.content)
        assert (tmp_path / "left").read_text() == expected
        assert (tmp_path / "right").read_text() == expected

    async def test_one_workflow_builds_a_diamond(self, tmp_path: Path) -> None:
        stand = ChannelStand(tmp_path)

        replies = await stand.turn(
            [
                _workflow(
                    src=_emit("raw", 3, 0),
                    left={
                        "tool": "fake_stream",
                        "args": {"prefix": "L", "feed": "raw", "out": "l"},
                    },
                    right={
                        "tool": "fake_stream",
                        "args": {"prefix": "R", "feed": "raw", "out": "r"},
                    },
                    sink={"tool": "fake_merge", "args": {"feeds": ["l", "r"]}},
                )
            ]
        )

        nodes = _nodes(replies["call_0"])
        assert _ok(nodes["sink"]) == "merged 2:Lm0Lm1Lm2;Rm0Rm1Rm2|t0ken"

    async def test_two_workflows_of_a_response_are_independent(
        self, tmp_path: Path
    ) -> None:
        """Два вызова workflow в одном ответе — две группы: писатель одной
        упал, вторая доработала."""
        stand = ChannelStand(tmp_path)

        replies = await stand.turn(
            [
                _workflow(
                    src=_emit("a", 40, 1024, fail=True), sink=_collect("a", "cut")
                ),
                _workflow(src=_emit("b", 8, 1024), sink=_collect("b", "whole")),
            ]
        )

        assert replies["call_0"].status == "error"
        assert isinstance(
            _failed(_nodes(replies["call_0"])["sink"]), GroupFailureResult
        )
        assert not (tmp_path / "cut").exists()

        assert replies["call_1"].status == "success"
        expected = _collected("m", 8, 1024)
        assert _ok(_nodes(replies["call_1"])["sink"]).startswith(expected)
        assert (tmp_path / "whole").read_text() == expected

    async def test_broken_plan_refuses_the_workflow(self, tmp_path: Path) -> None:
        """Два писателя одного канала: план отвергнут до запуска, вызов
        получает текст нарушения, ни один процесс не стартует."""
        stand = ChannelStand(tmp_path)

        replies = await stand.turn(
            [
                _workflow(
                    one=_emit("rows", 4, 0),
                    two=_emit("rows", 4, 0),
                    sink=_collect("rows", "never"),
                )
            ]
        )

        error = _error(replies["call_0"])
        assert "channel 'rows' has two writers" in error.llm_view()
        assert error.error_kind == StreamCallKind.PLAN_REFUSED
        assert not (tmp_path / "never").exists()

    async def test_malformed_workflow_is_refused_with_the_field(
        self, tmp_path: Path
    ) -> None:
        """Узел без инструмента: отказ называет поле, процессы не стартуют."""
        stand = ChannelStand(tmp_path)

        replies = await stand.turn(
            [
                {
                    "name": WorkflowTool.NAME,
                    "args": {"nodes": [{"key": "src", "args": {"out": "rows"}}]},
                }
            ]
        )

        error = _error(replies["call_0"])
        assert error.error_kind == StreamCallKind.PLAN_REFUSED
        assert "nodes.0.tool: Field required" in error.llm_view()

    async def test_failed_writer_leaves_no_commit(self, tmp_path: Path) -> None:
        stand = ChannelStand(tmp_path)

        replies = await stand.turn(
            [
                _workflow(
                    src=_emit("rows", 40, 1024, fail=True),
                    sink=_collect("rows", "cut"),
                )
            ]
        )

        nodes = _nodes(replies["call_0"])
        for key in ("src", "sink"):
            error = _failed(nodes[key])
            assert isinstance(error, GroupFailureResult), error
            assert error.origin is not None, error
            assert error.origin.call_id == "call_0_0", error.origin
            assert error.own == (key == "src"), error
            assert "nothing was committed" in error.llm_view()

        assert replies["call_0"].status == "error"
        assert not (tmp_path / "cut").exists()

    async def test_history_draws_the_workflow_as_steps_of_its_nodes(
        self, tmp_path: Path
    ) -> None:
        """Лента из истории раскрывает результат workflow в шаги узлов."""
        stand = ChannelStand(tmp_path)
        saver = InMemorySaver()

        await stand.turn(
            [_workflow(src=_emit("rows", 4, 0), sink=_collect("rows", "kept"))],
            saver,
        )

        steps = await TranscriptFeed(CheckpointMessages(saver)).steps(THREAD_ID, "user")

        names: list[str] = []
        for step in steps:
            if step.get(StepField.TYPE) != StepKind.TOOL.value:
                continue

            title = str(step.get(StepField.NAME, ""))
            names.append(title.rsplit(" ", maxsplit=1)[-1])

        assert names == ["fake_emit", "fake_collect"]


def _stream(key_in: str, key_out: str) -> dict[str, Any]:
    return {
        "tool": "fake_stream",
        "args": {"prefix": "S", "feed": key_in, "out": key_out},
    }


def _echo(text: str) -> dict[str, Any]:
    return {"name": "fake_echo", "args": {"text": text, "repeat": 1}}


@pytest.mark.anyio
class TestWorkflowGroups:
    """Группы внутри вызова workflow и вызовы рядом с ним в одном ответе."""

    async def test_unrelated_groups_of_one_workflow_are_independent(
        self, tmp_path: Path
    ) -> None:
        """Две связки одного вызова не связаны каналами: писатель первой
        упал — сорвана только она, вторая зафиксировала результат."""
        stand = ChannelStand(tmp_path)

        replies = await stand.turn(
            [
                _workflow(
                    a_src=_emit("a", 40, 1024, fail=True),
                    a_sink=_collect("a", "cut"),
                    b_src=_emit("b", 8, 1024),
                    b_sink=_collect("b", "whole"),
                )
            ]
        )

        reply = replies["call_0"]
        nodes = _nodes(reply)
        assert reply.status == "error"

        broken = _failed(nodes["a_sink"])
        assert isinstance(broken, GroupFailureResult), broken
        assert broken.origin is not None
        assert broken.origin.call_id == "call_0_0"
        assert not (tmp_path / "cut").exists()

        expected = _collected("m", 8, 1024)
        assert _ok(nodes["b_sink"]).startswith(expected)
        assert "emitted 8" in _ok(nodes["b_src"])
        assert (tmp_path / "whole").read_text() == expected

    async def test_channel_names_live_inside_one_workflow_call(
        self, tmp_path: Path
    ) -> None:
        """Два вызова workflow называют канал одинаково: это разные каналы,
        двух писателей у канала нет."""
        stand = ChannelStand(tmp_path)

        replies = await stand.turn(
            [
                _workflow(src=_emit("rows", 4, 16), sink=_collect("rows", "first")),
                _workflow(src=_emit("rows", 6, 16), sink=_collect("rows", "second")),
            ]
        )

        assert replies["call_0"].status == "success"
        assert replies["call_1"].status == "success"
        assert (tmp_path / "first").read_text() == _collected("m", 4, 16)
        assert (tmp_path / "second").read_text() == _collected("m", 6, 16)

    async def test_workflow_and_a_plain_call_share_a_response(
        self, tmp_path: Path
    ) -> None:
        stand = ChannelStand(tmp_path)

        replies = await stand.turn(
            [
                _echo("hi"),
                _workflow(src=_emit("rows", 4, 16), sink=_collect("rows", "kept")),
            ]
        )

        assert str(replies["call_0"].content) == "hi|t0ken"
        assert replies["call_1"].status == "success"
        assert (tmp_path / "kept").read_text() == _collected("m", 4, 16)

    async def test_unread_output_is_drained_by_dev_null(self, tmp_path: Path) -> None:
        stand = ChannelStand(tmp_path)

        replies = await stand.turn(
            [
                _workflow(
                    src=_emit("rows", 4, 16),
                    drain={"tool": "dev_null", "args": {"feeds": ["rows"]}},
                )
            ]
        )

        nodes = _nodes(replies["call_0"])
        assert replies["call_0"].status == "success"
        assert "rows" in _ok(nodes["drain"])

    async def test_broken_workflow_does_not_touch_its_neighbours(
        self, tmp_path: Path
    ) -> None:
        """Отказ плана — ошибка одного вызова: соседний workflow и обычный
        вызов того же ответа исполнены."""
        stand = ChannelStand(tmp_path)

        replies = await stand.turn(
            [
                _workflow(src=_emit("rows", 4, 16), sink=_collect("row", "never")),
                _workflow(src=_emit("rows", 4, 16), sink=_collect("rows", "kept")),
                _echo("still here"),
            ]
        )

        assert _error(replies["call_0"]).error_kind == StreamCallKind.PLAN_REFUSED
        assert not (tmp_path / "never").exists()

        assert replies["call_1"].status == "success"
        assert (tmp_path / "kept").read_text() == _collected("m", 4, 16)
        assert str(replies["call_2"].content) == "still here|t0ken"


REFUSALS: dict[str, tuple[dict[str, Any], tuple[str, ...]]] = {
    "typo_in_channel_name": (
        _workflow(src=_emit("orders", 4, 0), sink=_collect("order", "never")),
        (
            "fake_collect (sink) reads channel 'order' in 'feed'",
            "which no node writes",
            "channels written here: ['orders']",
        ),
    ),
    "output_nobody_reads": (
        _workflow(src=_emit("rows", 4, 0)),
        (
            "channel 'rows' written by fake_emit (src) has no readers",
            "dev_null",
        ),
    ),
    "two_writers_of_a_channel": (
        _workflow(
            one=_emit("rows", 4, 0),
            two=_emit("rows", 4, 0),
            sink=_collect("rows", "never"),
        ),
        ("channel 'rows' has two writers, fake_emit (one) and fake_emit (two)",),
    ),
    "cycle": (
        _workflow(a=_stream("x", "y"), b=_stream("y", "x")),
        ("channels form a cycle", "fake_stream (a)", "fake_stream (b)"),
    ),
    "port_field_is_missing": (
        _workflow(
            src={"tool": "fake_emit", "args": {"prefix": "m", "count": 1, "size": 0}},
            sink=_collect("rows", "never"),
        ),
        ("fake_emit (src) names no channel for port 'out'",),
    ),
    "port_named_by_a_wrong_field": (
        _workflow(
            src={
                "tool": "fake_emit",
                "args": {"prefix": "m", "count": 1, "size": 0, "output": "rows"},
            },
            sink=_collect("rows", "never"),
        ),
        ("fake_emit (src) names no channel for port 'out'",),
    ),
    "list_for_a_single_port": (
        _workflow(
            src=_emit("rows", 4, 0),
            sink={
                "tool": "fake_collect",
                "args": {
                    "marker": "never",
                    "fail": False,
                    "gated": True,
                    "feed": ["rows"],
                },
            },
        ),
        ("fake_collect (sink) field 'feed' expects a channel name",),
    ),
    "string_for_a_list_port": (
        _workflow(
            src=_emit("rows", 4, 0),
            sink={"tool": "fake_merge", "args": {"feeds": "rows"}},
        ),
        ("fake_merge (sink) field 'feeds' expects a list of channel names",),
    ),
    "unknown_tool": (
        _workflow(
            src=_emit("rows", 4, 0),
            sink={"tool": "fake_sink_into_nowhere", "args": {"feed": "rows"}},
        ),
        (
            "node 'sink' names tool 'fake_sink_into_nowhere'",
            "expected one of the stream tools",
            "fake_collect",
        ),
    ),
    "plain_tool_inside_workflow": (
        _workflow(
            src=_emit("rows", 4, 0),
            echo={"tool": "fake_echo", "args": {"text": "hi", "repeat": 1}},
        ),
        (
            "node 'echo' names tool 'fake_echo'",
            "a tool without stream ports is called directly, not through workflow",
        ),
    ),
    "repeated_node_key": (
        {
            "name": WorkflowTool.NAME,
            "args": {
                "nodes": [
                    {"key": "src", **_emit("rows", 4, 0)},
                    {"key": "src", **_collect("rows", "never")},
                ]
            },
        },
        ("workflow node key 'src' is repeated",),
    ),
    "no_nodes": (
        {"name": WorkflowTool.NAME, "args": {"nodes": []}},
        ("nodes: Value should have at least 1 item",),
    ),
    "nodes_is_not_a_list": (
        {"name": WorkflowTool.NAME, "args": {"nodes": "src -> sink"}},
        ("nodes: 'str' instances are not allowed as a Sequence value",),
    ),
    "node_without_a_tool": (
        {
            "name": WorkflowTool.NAME,
            "args": {"nodes": [{"key": "src", "args": {"out": "rows"}}]},
        },
        ("nodes.0.tool: Field required",),
    ),
    "node_with_a_made_up_field": (
        {
            "name": WorkflowTool.NAME,
            "args": {
                "nodes": [
                    {"key": "src", "name": "fake_emit", "arguments": {"out": "rows"}}
                ]
            },
        },
        (
            "nodes.0.tool: Field required",
            "nodes.0.name: Extra inputs are not permitted",
            "nodes.0.arguments: Extra inputs are not permitted",
        ),
    ),
    "no_arguments_at_all": (
        {"name": WorkflowTool.NAME, "args": {}},
        ("nodes: Field required",),
    ),
}


@pytest.mark.anyio
class TestWorkflowRefusals:
    """Галлюцинации модели в вызове workflow: отказ до запуска с текстом,
    по которому модель может исправить вызов, и ни одного процесса."""

    @pytest.mark.parametrize("case", sorted(REFUSALS))
    async def test_broken_workflow_is_refused_before_any_process(
        self, case: str, tmp_path: Path
    ) -> None:
        call, expected = REFUSALS[case]
        stand = ChannelStand(tmp_path)

        replies = await stand.turn([call])

        reply = replies["call_0"]
        error = _error(reply)
        text = error.llm_view()

        assert reply.status == "error"
        assert error.error_kind == StreamCallKind.PLAN_REFUSED, text
        for fragment in expected:
            assert fragment in text, text

        assert str(reply.content) == text
        assert not (tmp_path / "never").exists()
        assert stand.started() == []

    async def test_stream_tool_called_outside_workflow_is_told_to_use_it(
        self, tmp_path: Path
    ) -> None:
        """Модель зовёт насос отдельным вызовом, как обычный инструмент."""
        stand = ChannelStand(tmp_path)

        replies = await stand.turn(
            [{"name": "fake_emit", "args": _emit("rows", 4, 0)["args"]}]
        )

        error = _error(replies["call_0"])
        text = error.llm_view()
        assert error.error_kind == StreamCallKind.OUTSIDE_WORKFLOW, text
        assert "'fake_emit' reads or writes stream channels" in text
        assert "runs only as a node of 'workflow'" in text
        assert stand.started() == []


@pytest.mark.anyio
class TestWorkflowNodeFailures:
    """План верен, но узел не проходит схему своего инструмента: отказывает
    сам узел, его группа срывается, модель видит, какое поле чинить."""

    async def test_bad_arguments_of_a_node_name_the_field(self, tmp_path: Path) -> None:
        stand = ChannelStand(tmp_path)

        replies = await stand.turn(
            [
                _workflow(
                    src={
                        "tool": "fake_emit",
                        "args": {
                            "prefix": "m",
                            "count": "many",
                            "size": 0,
                            "out": "rows",
                        },
                    },
                    sink=_collect("rows", "never"),
                )
            ]
        )

        reply = replies["call_0"]
        nodes = _nodes(reply)
        assert reply.status == "error"

        assert nodes["src"].errored
        assert "count" in nodes["src"].content
        assert "[src] fake_emit:" in str(reply.content)

        sink = _failed(nodes["sink"])
        assert isinstance(sink, GroupFailureResult), sink
        assert sink.origin is not None
        assert sink.origin.tool == "fake_emit"
        assert "nothing was committed" in sink.llm_view()
        assert not (tmp_path / "never").exists()

    async def test_missing_argument_of_a_node_names_the_field(
        self, tmp_path: Path
    ) -> None:
        stand = ChannelStand(tmp_path)

        replies = await stand.turn(
            [
                _workflow(
                    src=_emit("rows", 4, 0),
                    sink={"tool": "fake_collect", "args": {"feed": "rows"}},
                )
            ]
        )

        nodes = _nodes(replies["call_0"])
        assert nodes["sink"].errored
        assert "marker" in nodes["sink"].content
        assert isinstance(_failed(nodes["src"]), GroupFailureResult)
