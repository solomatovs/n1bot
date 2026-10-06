"""Группа связанных каналами вызовов на настоящих субпроцессах.

Вызовы идут тем же путём, что из чата: обёртка запуска (ToolProcessWrap)
видит ручку группы в CallAmbient, открывает вызов потоково и отдаёт группе
каналы; раздача ChannelFanOut соединяет выход писателя со входами читателей.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import threading
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, SecretStr

from boba.identity.context import CallContexts
from boba.identity.run import Runs
from boba.stand.toolstand import ProbeBody, ProbeTools, ToolStand
from boba.stand_core.fake_toolmod import (
    FakeConfig,
    fake_collect,
    fake_deaf,
    fake_echo,
    fake_emit,
    fake_head,
    fake_merge,
    fake_shard,
    fake_split,
    fake_stream,
)
from boba.toolkit.chain import (
    CallAmbient,
    ChannelFanOut,
    GroupCall,
    GroupFailureResult,
    GroupVerdict,
    PipeTee,
    StreamFailureKind,
    StreamGroupRun,
    StreamPlan,
    StreamPlanError,
    StreamTimings,
)
from boba.toolkit.dag import DagNode, DagPlanner, DagSpec, NodeOutcome
from boba.toolkit.launcher import (
    CallGate,
    CallInputPort,
    CollectedCall,
    LauncherError,
    PayloadFailureError,
    ToolCall,
    ToolFrame,
    ToolOutcome,
)
from boba.toolkit.protocol import CallInputSpec, CallOutputSpec, ReplyOk, ToolCommand
from boba.toolkit.result import (
    ErrorResult,
    MarkdownResult,
    ToolResultBase,
)
from boba.toolrun.dag_run import DagOutcome, DagRunner
from boba.toolrun.dev_null import DevNullTool
from boba.toolrun.hosted import DirectCalls, HostedCall, HostedTool, ToolSchema
from boba.toolrun.injected import InjectedConfig, StaticConfig
from boba.toolrun.process import ProcessLauncherConfig, ProcessToolCaller
from boba.toolrun.stream_calls import StreamChannelFields, StreamGroupsConfig
from boba.toolrun.streams import CallJournals

CFG = FakeConfig(token=SecretStr("t0ken"), limit=5)
MODULE = "boba.stand_core.fake_toolmod"
FAST = StreamTimings(open_sec=20.0, stall_sec=3.0, poll_sec=0.1)
STREAM_CFG = StreamGroupsConfig(
    open_sec=20.0,
    stall_sec=3.0,
    poll_sec=0.1,
    pipe_bytes=65536,
    pipe_bytes_max=1 << 30,
)


def _launcher(workdir: Path, ambient: CallAmbient) -> ProcessToolCaller:
    cfg = ProcessLauncherConfig(
        provider="process",
        workdir=str(workdir),
        timeout_sec=60.0,
        channel_limit_bytes=4_000_000,
        stderr_tail_bytes=8192,
        kill_grace_sec=0.5,
    )
    return ProcessToolCaller("stream-group", cfg, CallContexts(), ambient)


@dataclass(frozen=True)
class Call:
    """Вызов группы в тесте: инструмент, аргументы модели и каналы.

    output — канал единственного выходного порта; вызов с несколькими
    выходами называет их парами (порт, канал) в outputs."""

    tool: str
    args: Mapping[str, object]
    output: str | None
    inputs: Sequence[tuple[str, str]]
    outputs: Sequence[tuple[str, str]] = ()
    pipe_bytes: int = 0


class GroupStand:
    """Инструменты стенда, собранные как в приложении (обёртка запуска,
    injected-конфиг, поля каналов), и прогон DAG исполнителем."""

    def __init__(self, workdir: Path) -> None:
        self._workdir = workdir
        self._ambient = CallAmbient()
        contexts = CallContexts()
        stand = ToolStand(
            STREAM_CFG,
            contexts,
            CallJournals(None, Runs(contexts)),
            self._ambient,
            (InjectedConfig(self._config_of, StaticConfig()),),
        )
        stand.launch(
            (
                fake_echo,
                fake_emit,
                fake_collect,
                fake_head,
                fake_stream,
                fake_merge,
                fake_deaf,
                fake_split,
                fake_shard,
            ),
            stand.process_launcher("stream-group", workdir, 60.0),
        )
        self._specs = stand.specs()
        self._tools: dict[str, HostedTool] = {}
        for tool in stand.tools():
            self._tools[tool.name] = tool

        StreamChannelFields(STREAM_CFG).attach_all(
            list(self._tools.values()), self._specs
        )

    @staticmethod
    def _config_of(name: str, annotation: object) -> object:
        return CFG

    def adopt(self, extra: HostedTool) -> None:
        """Готовый инструмент хоста рядом с инструментами стенда."""
        self._tools[extra.name] = extra

    def dag(self, calls: Mapping[str, Call]) -> DagSpec:
        """Вызовы стенда — узлы DAG."""
        nodes: list[DagNode] = []
        for key, call in calls.items():
            nodes.append(self._node(key, call))

        return DagSpec(name="stand", version=1, nodes=nodes)

    def plan(self, calls: Mapping[str, Call]) -> StreamPlan:
        """План единственной группы: вызовы стенда связаны каналами."""
        plans = DagPlanner(self._specs.of, STREAM_CFG.pipe_bytes).plan(self.dag(calls))
        if len(plans) != 1:
            msg = f"stand calls must form one group, got {len(plans)}"
            raise AssertionError(msg)

        return plans[0]

    def _node(self, key: str, call: Call) -> DagNode:
        """Узел из вызова: каналы ложатся в аргументы полями портов —
        строкой у одиночного порта, списком у порта-списка."""
        spec = self._specs.of(call.tool)

        args: dict[str, object] = dict(call.args)
        if call.output is not None:
            args[spec.outbound()[0].name] = call.output

        bound: dict[str, list[str]] = {}
        for port, channel in (*call.inputs, *call.outputs):
            bound.setdefault(port, []).append(channel)

        for port, channels in bound.items():
            if spec.port(port).many:
                args[port] = channels
                continue

            args[port] = channels[0]

        if call.pipe_bytes:
            args[DagPlanner.PIPE_FIELD] = call.pipe_bytes

        return DagNode.model_validate({"key": key, "tool": call.tool, "args": args})

    async def run(
        self,
        calls: Mapping[str, Call],
        timings: StreamTimings = FAST,
    ) -> tuple[DagOutcome, dict[str, NodeOutcome]]:
        """Прогон DAG исполнителем: итог целиком и итоги по ключам узлов."""
        runner = DagRunner(
            self._tools,
            ToolSchema().cards_of(self._tools.values()),
            self._specs,
            self._ambient,
            timings,
            STREAM_CFG.pipe_bytes,
            DirectCalls(),
        )

        outcome = await asyncio.wait_for(runner.run(self.dag(calls)), timeout=60)

        results: dict[str, NodeOutcome] = {}
        for node in outcome.nodes:
            results[node.key] = node

        return outcome, results

    async def one(self, group: StreamGroupRun, key: str, call: Call) -> Any:
        """Один вызов под ручкой группы мимо исполнителя: тесты, где часть
        вызовов группы намеренно не стартует."""
        tool = self._tools[call.tool]

        with self._ambient.in_slot(group.slot(key)):
            try:
                sent = HostedCall(tool=tool.name, key=key, kwargs=dict(call.args))

                return await tool.body(sent)
            except PayloadFailureError as exc:
                return exc

    def marker(self, name: str) -> Path:
        return self._workdir / name


def _emit(
    prefix: str, count: int, size: int, *, fail: bool = False
) -> dict[str, object]:
    return {"prefix": prefix, "count": count, "size": size, "fail_midway": fail}


def _expected(prefix: str, count: int, size: int) -> str:
    digest = hashlib.sha256()
    total = 0
    for seq in range(count):
        body = f"{prefix}{seq}".encode() + b"x" * size
        digest.update(body)
        total += len(body)

    return f"collected {count} frames {total} bytes {digest.hexdigest()[:16]}"


def _content(result: NodeOutcome) -> str:
    assert not result.failed(), result.content
    return result.content


def _failure(result: NodeOutcome) -> GroupFailureResult:
    """Срыв группы глазами узла."""
    assert result.failed(), result.content
    assert isinstance(result.artifact, GroupFailureResult), result.artifact
    assert result.artifact.error_kind == StreamFailureKind.GROUP_FAILED
    return result.artifact
    return result


class PlainArgs(BaseModel):
    """Аргументы инструментов без портов: один текст."""

    text: str


class PlainTools:
    """Инструменты хоста без портов и обёртки запуска: тело в процессе теста."""

    def echo(self) -> HostedTool:
        return self._hosted("plain_echo", self._echo)

    def crash(self) -> HostedTool:
        return self._hosted("plain_crash", self._crash)

    @staticmethod
    def _hosted(name: str, body: ProbeBody) -> HostedTool:
        return ProbeTools().hosted(name, PlainArgs, body)

    @staticmethod
    async def _echo(text: str) -> ToolResultBase:
        return MarkdownResult(text=f"echo {text}")

    @staticmethod
    async def _crash(text: str) -> ToolResultBase:
        msg = f"plain crashed on {text}"
        raise RuntimeError(msg)


class TestIndependentGroups:
    """Один DAG несёт несколько групп: вызовы, связанные каналами, и группы
    из одного вызова без портов; сбой остаётся внутри своей группы."""

    @staticmethod
    def _pipeline(prefix: str, channel: str, *, fail: bool) -> dict[str, Call]:
        return {
            f"{prefix}_src": Call(
                "fake_emit", _emit(prefix, 8, 1024, fail=fail), channel, ()
            ),
            f"{prefix}_sink": Call(
                "fake_collect",
                {"marker": f"{prefix}_sink", "fail": False, "gated": True},
                None,
                (("feed", channel),),
            ),
        }

    @pytest.mark.anyio
    async def test_calls_without_ports_fail_on_their_own(self, tmp_path: Path) -> None:
        """Вызовы без портов — группы из одного узла: упавший и отказанный по
        аргументам не мешают соседу, их итог — ошибка вызова со статусом
        error."""
        stand = GroupStand(tmp_path)
        stand.adopt(PlainTools().echo())
        stand.adopt(PlainTools().crash())
        calls = {
            "ok": Call("plain_echo", {"text": "hi"}, None, ()),
            "crash": Call("plain_crash", {"text": "boom"}, None, ()),
            "bad_args": Call("plain_echo", {}, None, ()),
        }

        outcome, results = await stand.run(calls)

        assert _content(results["ok"]) == "echo hi"
        assert not results["ok"].errored

        assert results["crash"].failed()
        assert results["crash"].errored
        assert "plain crashed on boom" in results["crash"].content
        assert not isinstance(results["crash"].artifact, GroupFailureResult)

        assert results["bad_args"].errored
        assert "text" in results["bad_args"].content

        failed: list[str] = []
        for node in outcome.failures():
            failed.append(node.key)

        assert failed == ["crash", "bad_args"]

    @pytest.mark.anyio
    async def test_failed_call_without_ports_leaves_a_pipeline_alone(
        self, tmp_path: Path
    ) -> None:
        stand = GroupStand(tmp_path)
        stand.adopt(PlainTools().crash())
        calls = self._pipeline("g", "a", fail=False)
        calls["crash"] = Call("plain_crash", {"text": "boom"}, None, ())

        _outcome, results = await stand.run(calls)

        assert results["crash"].failed()
        assert _content(results["g_sink"]).startswith(_expected("g", 8, 1024))
        assert "emitted 8" in _content(results["g_src"])

    @pytest.mark.anyio
    async def test_process_call_without_ports_runs_as_a_group_of_one(
        self, tmp_path: Path
    ) -> None:
        """Инструмент-процесс без портов идёт тем же путём, что и группа
        каналов: под ручкой своей группы. Его сбой — его собственная ошибка,
        а не срыв группы, и соседний такой же вызов он не трогает."""
        stand = GroupStand(tmp_path)
        calls = {
            "ok": Call("fake_echo", {"text": "hi", "repeat": 2}, None, ()),
            "down": Call("fake_echo", {"text": "boom", "repeat": 1}, None, ()),
        }

        _outcome, results = await stand.run(calls)

        assert _content(results["ok"]) == "hi hi|t0ken"

        assert results["down"].failed()
        assert not isinstance(results["down"].artifact, GroupFailureResult)
        assert "fake backend is down" in results["down"].content

    @pytest.mark.anyio
    async def test_group_failure_stays_inside_its_group(self, tmp_path: Path) -> None:
        """Писатель одной группы упал: сорвана только она — вторая группа и
        вызов без портов того же DAG доработали."""
        stand = GroupStand(tmp_path)
        stand.adopt(PlainTools().echo())
        calls = self._pipeline("bad", "a", fail=True)
        calls.update(self._pipeline("good", "b", fail=False))
        calls["single"] = Call("plain_echo", {"text": "alone"}, None, ())

        _outcome, results = await stand.run(calls)

        failure = _failure(results["bad_sink"])
        assert failure.origin == GroupCall(tool="fake_emit", call_id="bad_src")
        assert not stand.marker("bad_sink").exists()

        assert _content(results["good_sink"]).startswith(_expected("good", 8, 1024))
        assert stand.marker("good_sink").exists()
        assert _content(results["single"]) == "echo alone"


class TestFanOut:
    @pytest.mark.anyio
    async def test_one_writer_feeds_three_readers_byte_for_byte(
        self, tmp_path: Path
    ) -> None:
        """Выход писателя раздаётся трём читателям через tee: каждый получает
        поток целиком, больше буфера пайпа в разы."""
        stand = GroupStand(tmp_path)
        calls: dict[str, Call] = {
            "src": Call("fake_emit", _emit("s", 96, 65536), "a", ()),
        }
        for index in range(3):
            args = {"marker": f"sink{index}", "fail": False, "gated": True}
            calls[f"sink{index}"] = Call("fake_collect", args, None, (("feed", "a"),))

        _group, results = await stand.run(calls)

        expected = _expected("s", 96, 65536)
        for index in range(3):
            assert _content(results[f"sink{index}"]).startswith(expected)
            assert stand.marker(f"sink{index}").read_text() == expected

        assert "emitted 96" in _content(results["src"])

    @pytest.mark.anyio
    async def test_reader_that_stops_early_does_not_break_the_others(
        self, tmp_path: Path
    ) -> None:
        """Читатель взял первый кадр и вышел: раздача его отцепляет, второй
        читатель дочитывает поток целиком, группа успешна."""
        stand = GroupStand(tmp_path)
        calls = {
            "src": Call("fake_emit", _emit("h", 64, 65536), "a", ()),
            "head": Call("fake_head", {}, None, (("feed", "a"),)),
            "full": Call(
                "fake_collect",
                {"marker": "full", "fail": False, "gated": True},
                None,
                (("feed", "a"),),
            ),
        }

        _group, results = await stand.run(calls)

        assert _content(results["head"]).startswith("head b'h0")
        assert _content(results["full"]).startswith(_expected("h", 64, 65536))


class TestGraphs:
    @pytest.mark.anyio
    async def test_chain_through_a_transform(self, tmp_path: Path) -> None:
        stand = GroupStand(tmp_path)
        calls = {
            "src": Call("fake_emit", _emit("c", 3, 0), "raw", ()),
            "mid": Call("fake_stream", {"prefix": "t:"}, "cooked", (("feed", "raw"),)),
            "sink": Call("fake_merge", {}, None, (("feeds", "cooked"),)),
        }

        _group, results = await stand.run(calls)

        assert _content(results["sink"]) == "merged 1:t:c0t:c1t:c2|t0ken"
        assert "streamed 3" in _content(results["mid"])

    @pytest.mark.anyio
    async def test_diamond_splits_and_joins(self, tmp_path: Path) -> None:
        """Ромб: источник делится на два трансформа, оба сходятся в одном
        потребителе нескольких потоков."""
        stand = GroupStand(tmp_path)
        calls = {
            "src": Call("fake_emit", _emit("d", 3, 0), "raw", ()),
            "left": Call("fake_stream", {"prefix": "L"}, "l", (("feed", "raw"),)),
            "right": Call("fake_stream", {"prefix": "R"}, "r", (("feed", "raw"),)),
            "join": Call("fake_merge", {}, None, (("feeds", "l"), ("feeds", "r"))),
        }

        _group, results = await stand.run(calls)

        assert _content(results["join"]) == "merged 2:Ld0Ld1Ld2;Rd0Rd1Rd2|t0ken"


class TestAllOrNothing:
    @pytest.mark.anyio
    async def test_writer_failure_reaches_no_reader_as_a_clean_end(
        self, tmp_path: Path
    ) -> None:
        """Писатель упал посреди потока: читатели не получают EOF за целый
        поток, ни один не фиксирует результат, все вызовы отвечают срывом."""
        stand = GroupStand(tmp_path)
        calls = {
            "src": Call("fake_emit", _emit("f", 40, 1024, fail=True), "a", ()),
            "one": Call(
                "fake_collect",
                {"marker": "one", "fail": False, "gated": True},
                None,
                (("feed", "a"),),
            ),
            "two": Call(
                "fake_collect",
                {"marker": "two", "fail": False, "gated": True},
                None,
                (("feed", "a"),),
            ),
        }

        _group, results = await stand.run(calls)

        for key in ("src", "one", "two"):
            failure = _failure(results[key])
            assert isinstance(failure, GroupFailureResult), failure
            assert failure.origin == GroupCall(tool="fake_emit", call_id="src")
            assert "scripted failure after 20 frames" in failure.llm_view()

        assert not stand.marker("one").exists()
        assert not stand.marker("two").exists()

    @pytest.mark.anyio
    async def test_ungated_reader_never_sees_a_cut_stream_as_whole(
        self, tmp_path: Path
    ) -> None:
        """Читатель без барьера фиксирует результат по EOF: оборванный
        писателем поток не должен закончиться для него EOF — он добит."""
        stand = GroupStand(tmp_path)
        args = {"marker": "loose", "fail": False, "gated": False}
        calls = {
            "src": Call("fake_emit", _emit("u", 40, 1024, fail=True), "a", ()),
            "loose": Call("fake_collect", args, None, (("feed", "a"),)),
        }

        _group, results = await stand.run(calls)

        failure = _failure(results["loose"])
        assert isinstance(failure, GroupFailureResult), failure
        assert failure.origin == GroupCall(tool="fake_emit", call_id="src")
        assert not stand.marker("loose").exists()

    @pytest.mark.anyio
    async def test_reader_failure_refuses_the_commit_of_the_others(
        self, tmp_path: Path
    ) -> None:
        """Один читатель упал, дочитав поток: второй стоит на барьере и
        получает отказ — фиксации нет ни у кого."""
        stand = GroupStand(tmp_path)
        calls = {
            "src": Call("fake_emit", _emit("r", 8, 128), "a", ()),
            "bad": Call(
                "fake_collect",
                {"marker": "bad", "fail": True, "gated": True},
                None,
                (("feed", "a"),),
            ),
            "good": Call(
                "fake_collect",
                {"marker": "good", "fail": False, "gated": True},
                None,
                (("feed", "a"),),
            ),
        }

        _group, results = await stand.run(calls)

        for key in ("src", "bad", "good"):
            failure = _failure(results[key])
            assert isinstance(failure, GroupFailureResult), failure
            assert failure.origin == GroupCall(tool="fake_collect", call_id="bad")

        assert not stand.marker("good").exists()

    @pytest.mark.anyio
    async def test_call_refused_before_launch_fails_the_group(
        self, tmp_path: Path
    ) -> None:
        stand = GroupStand(tmp_path)
        calls = {
            "src": Call("fake_emit", _emit("x", 4, 0), "a", ()),
            "ghost": Call(
                "fake_collect",
                {"marker": "ghost", "fail": False, "gated": True},
                None,
                (("feed", "a"),),
            ),
        }

        group = StreamGroupRun(stand.plan(calls), FAST)
        denied = ErrorResult(
            message="access denied: role DEV lacks fake_collect", error_kind="denied"
        )
        group.refuse("ghost", denied)

        result = await stand.one(group, "src", calls["src"])

        assert isinstance(result, PayloadFailureError), result
        assert "access denied" in str(result)

    @pytest.mark.anyio
    async def test_call_that_never_opens_times_the_group_out(
        self, tmp_path: Path
    ) -> None:
        stand = GroupStand(tmp_path)
        calls = {
            "src": Call("fake_emit", _emit("x", 4, 0), "a", ()),
            "ghost": Call(
                "fake_collect",
                {"marker": "ghost", "fail": False, "gated": True},
                None,
                (("feed", "a"),),
            ),
        }
        timings = StreamTimings(open_sec=1.0, stall_sec=3.0, poll_sec=0.1)

        group = StreamGroupRun(stand.plan(calls), timings)

        result = await stand.one(group, "src", calls["src"])

        assert isinstance(result, PayloadFailureError), result
        message = str(result)
        assert "fake_collect (ghost)" in message
        assert "did not open their channels within 1s" in message


class TestStall:
    @pytest.mark.anyio
    async def test_reader_that_never_reads_stalls_the_group_loudly(
        self, tmp_path: Path
    ) -> None:
        """Читатель не читает: данные встают, сторож застоя срывает группу и
        называет читателя с непрочитанным входом."""
        stand = GroupStand(tmp_path)
        calls = {
            "src": Call("fake_emit", _emit("z", 64, 65536), "a", ()),
            "deaf": Call("fake_deaf", {"sleep_sec": 30}, None, (("feed", "a"),)),
        }
        timings = StreamTimings(open_sec=20.0, stall_sec=2.0, poll_sec=0.1)

        _group, results = await stand.run(calls, timings)

        for key in ("src", "deaf"):
            message = _failure(results[key]).llm_view()
            assert "stream group stalled" in message
            assert "fake_deaf (deaf) input #0" in message


class TestMultiOut:
    """Несколько выходов у вызова: делитель и шардер."""

    @staticmethod
    def _collect(marker: str) -> dict[str, object]:
        return {"marker": marker, "fail": False, "gated": True}

    @pytest.mark.anyio
    async def test_split_sends_each_reader_its_own_half(self, tmp_path: Path) -> None:
        stand = GroupStand(tmp_path)
        even = tmp_path / "even"
        odd = tmp_path / "odd"
        calls = {
            "src": Call("fake_emit", _emit("s", 6, 0), "raw", ()),
            "cut": Call(
                "fake_split",
                {},
                None,
                (("feed", "raw"),),
                outputs=(("even", "e"), ("odd", "o")),
            ),
            "le": Call("fake_collect", self._collect("even"), None, (("feed", "e"),)),
            "lo": Call("fake_collect", self._collect("odd"), None, (("feed", "o"),)),
        }

        _group, results = await stand.run(calls)

        assert "split 3+3" in _content(results["cut"])
        assert even.read_text().startswith("collected 4 frames 6 bytes")
        assert odd.read_text().startswith("collected 4 frames 6 bytes")
        assert even.read_text() != odd.read_text()

    @pytest.mark.anyio
    async def test_shard_feeds_three_readers_round_robin(self, tmp_path: Path) -> None:
        stand = GroupStand(tmp_path)
        markers = [tmp_path / name for name in ("a", "b", "c")]
        calls = {
            "src": Call("fake_emit", _emit("s", 7, 0), "raw", ()),
            "shard": Call(
                "fake_shard",
                {},
                None,
                (("feed", "raw"),),
                outputs=(("outs", "a"), ("outs", "b"), ("outs", "c")),
            ),
        }
        for marker in markers:
            calls[marker.name] = Call(
                "fake_collect",
                self._collect(marker.name),
                None,
                (("feed", marker.name),),
            )

        _group, results = await stand.run(calls)

        assert "sharded 7 into 3" in _content(results["shard"])
        assert markers[0].read_text().startswith("collected 4 frames 6 bytes")
        assert markers[1].read_text().startswith("collected 3 frames 4 bytes")
        assert markers[2].read_text().startswith("collected 3 frames 4 bytes")

    def test_unread_output_refuses_the_plan(self, tmp_path: Path) -> None:
        stand = GroupStand(tmp_path)
        calls = {
            "src": Call("fake_emit", _emit("s", 2, 0), "raw", ()),
            "cut": Call(
                "fake_split",
                {},
                None,
                (("feed", "raw"),),
                outputs=(("even", "e"), ("odd", "o")),
            ),
            "le": Call("fake_collect", self._collect("e"), None, (("feed", "e"),)),
        }

        with pytest.raises(StreamPlanError, match="has no readers"):
            stand.plan(calls)


class TestPipeBytes:
    """Размер пайпов канала задаёт писатель; невыполнимый размер срывает
    группу до данных."""

    @pytest.mark.anyio
    async def test_writer_size_is_applied_end_to_end(self, tmp_path: Path) -> None:
        stand = GroupStand(tmp_path)
        marker = tmp_path / "sized"
        calls = {
            "src": Call("fake_emit", _emit("s", 8, 1024), "a", (), pipe_bytes=131072),
            "sink": Call(
                "fake_collect",
                {"marker": "sized", "fail": False, "gated": True},
                None,
                (("feed", "a"),),
            ),
        }

        _group, results = await stand.run(calls)

        assert marker.read_text() == _expected("s", 8, 1024)
        assert "emitted 8" in _content(results["src"])

    @pytest.mark.anyio
    async def test_unsatisfiable_size_fails_the_group_loudly(
        self, tmp_path: Path
    ) -> None:
        limit = int(Path("/proc/sys/fs/pipe-max-size").read_text())
        stand = GroupStand(tmp_path)
        calls = {
            "src": Call("fake_emit", _emit("s", 2, 0), "a", (), pipe_bytes=limit * 2),
            "sink": Call(
                "fake_collect",
                {"marker": "never", "fail": False, "gated": True},
                None,
                (("feed", "a"),),
            ),
        }

        _group, results = await stand.run(calls)

        # размер требуют и выход писателя, и вход читателя: кто первым
        # получил отказ ядра, тот и сорвал группу — порядок не фиксирован
        texts = [_failure(results[key]).llm_view() for key in ("src", "sink")]
        assert any("pipe-user-pages-soft" in text for text in texts), texts
        for text in texts:
            assert "setting the pipe buffer" in text

        assert not (tmp_path / "never").exists()


class TestDevNull:
    """Встроенный слив: явный читатель каналов, которые некому читать."""

    @pytest.mark.anyio
    async def test_unwanted_channel_is_discarded_with_a_byte_count(
        self, tmp_path: Path
    ) -> None:
        stand = GroupStand(tmp_path)
        marker = tmp_path / "even"
        calls = {
            "src": Call("fake_emit", _emit("s", 6, 0), "raw", ()),
            "cut": Call(
                "fake_split",
                {},
                None,
                (("feed", "raw"),),
                outputs=(("even", "e"), ("odd", "o")),
            ),
            "keep": Call(
                "fake_collect",
                {"marker": "even", "fail": False, "gated": True},
                None,
                (("feed", "e"),),
            ),
            "drop": Call("dev_null", {"feeds": ["o"]}, None, (("feeds", "o"),)),
        }

        _group, results = await stand.run(calls)

        assert marker.read_text().startswith("collected 4 frames 6 bytes")
        dropped = results["drop"].artifact
        assert isinstance(dropped, MarkdownResult), dropped
        assert "discarded bytes by channel" in dropped.text
        assert "o: " in dropped.text

    @pytest.mark.anyio
    async def test_dev_null_outside_a_group_is_refused(self) -> None:
        built = DevNullTool(CallAmbient()).build()
        coroutine = built.coroutine
        assert coroutine is not None

        with pytest.raises(PayloadFailureError, match="only as a node of workflow"):
            await coroutine(feeds=["ghost"])


class IdleGate(CallGate):
    """Барьер вызова-заглушки: сигналов нет, ответы ничего не делают."""

    def claim(self, on_ready: Callable[[], None]) -> None:
        return

    def release(self) -> None:
        return

    def refuse(self) -> None:
        return


class IdleCall(ToolCall):
    """Вызов без процесса: группе нужны только его барьер и close. Условие
    теста — отказ ядра в буфере промежуточного пайпа — настоящим процессом
    не воспроизвести: тот же размер раньше отвергнет пайп самого вызова."""

    def __init__(self) -> None:
        self.closed = False

    def inputs(self) -> Sequence[CallInputPort]:
        return ()

    def done_sending(self) -> None:
        return

    def gate(self) -> CallGate:
        return IdleGate()

    def frames(self) -> Iterator[ToolFrame]:
        return iter(())

    def result(self) -> ToolOutcome:
        raise LauncherError("idle call has no envelope")

    def close(self) -> None:
        self.closed = True


class TestFanOutStartFailure:
    """Ядро не дало буфер промежуточному пайпу раздачи (бюджет пайпов
    пользователя исчерпан): раздача не стартует, группа срывается и
    заканчивается, а не ждёт конца канала вечно."""

    @staticmethod
    def _too_big() -> int:
        return int(Path("/proc/sys/fs/pipe-max-size").read_text()) * 2

    @staticmethod
    def _sink(marker: str) -> dict[str, object]:
        return {"marker": marker, "fail": False, "gated": True}

    def test_fanout_that_cannot_start_closes_its_pipes(self) -> None:
        source_r, source_w = os.pipe()
        first_r, first_w = os.pipe()
        second_r, second_w = os.pipe()
        drained = threading.Event()
        errors: list[str] = []

        fanout = ChannelFanOut(
            "raw",
            source_r,
            [first_w, second_w],
            PipeTee(),
            on_error=errors.append,
            on_drained=drained.set,
            pipe_bytes=self._too_big(),
        )
        try:
            with pytest.raises(LauncherError, match="pipe-user-pages-soft"):
                fanout.start()

            with pytest.raises(OSError, match="Bad file descriptor"):
                os.fstat(source_r)

            assert not drained.is_set()
            assert errors == []
        finally:
            for fd in (source_w, first_r, first_w, second_r, second_w):
                os.close(fd)

    def test_group_settles_when_a_fanout_cannot_start(self, tmp_path: Path) -> None:
        stand = GroupStand(tmp_path)
        calls = {
            "src": Call(
                "fake_emit", _emit("s", 4, 0), "a", (), pipe_bytes=self._too_big()
            ),
            "one": Call("fake_collect", self._sink("one"), None, (("feed", "a"),)),
            "two": Call("fake_collect", self._sink("two"), None, (("feed", "a"),)),
        }
        group = StreamGroupRun(stand.plan(calls), FAST)

        source_r, source_w = os.pipe()
        first_r, first_w = os.pipe()
        second_r, second_w = os.pipe()
        attached = {"src": IdleCall(), "one": IdleCall(), "two": IdleCall()}
        try:
            group.attach("src", attached["src"], [source_r], [])
            group.attach("one", attached["one"], [], [first_w])
            group.attach("two", attached["two"], [], [second_w])

            for key in attached:
                group.done(key, cause=None, stopped=False)

            settled = threading.Event()
            verdicts: list[GroupVerdict] = []

            def wait() -> None:
                verdicts.append(group.verdict())
                settled.set()

            threading.Thread(target=wait, daemon=True).start()

            assert settled.wait(timeout=10), "the group did not settle"
            failure = verdicts[0].failure
            assert failure is not None
            assert "pipe-user-pages-soft" in failure.llm_view()
            assert attached["src"].closed
            assert attached["one"].closed
        finally:
            for fd in (source_w, first_r, second_r):
                os.close(fd)


class TestRawFanOut:
    def test_raw_stream_reaches_every_reader_verbatim(self, tmp_path: Path) -> None:
        """Сырой поток fake_relay раздаётся двум fake_relay напрямую через
        ChannelFanOut: байты у обоих совпадают с исходными."""
        launcher = _launcher(tmp_path, CallAmbient())
        payload = os.urandom(3 << 20)

        def command() -> ToolCommand:
            return ToolCommand(
                argv=("python3", "-m", MODULE, "fake_relay"),
                config=b'{"cfg": {"token": "t0ken", "limit": 5}}',
                inputs=(CallInputSpec(port="feed", raw=True),),
                outputs=(CallOutputSpec(port="out", raw=True),),
            )

        source = launcher.open_tap(command())
        readers = [launcher.open_tap(command()), launcher.open_tap(command())]

        drained = threading.Event()
        errors: list[str] = []
        reader_fds: list[int] = []
        for reader in readers:
            reader_fds.append(reader.call.inputs()[0].take_fd())

        fanout = ChannelFanOut(
            "raw",
            source.frames_fds[0],
            reader_fds,
            PipeTee(),
            on_error=errors.append,
            on_drained=drained.set,
        )
        collected: list[bytearray] = []
        collectors: list[threading.Thread] = []
        for reader in readers:
            sink = bytearray()
            collected.append(sink)
            collectors.append(
                threading.Thread(
                    target=self._collect, args=(reader.frames_fds[0], sink), daemon=True
                )
            )

        for collector in collectors:
            collector.start()

        fanout.start()

        feed = source.call.inputs()[0].take_fd()
        view = memoryview(payload)
        while view:
            written = os.write(feed, view)
            view = view[written:]

        os.close(feed)
        source.call.result()

        assert drained.wait(timeout=30), "fan-out did not drain"

        for fd in reader_fds:
            os.close(fd)

        for reader, collector, sink in zip(readers, collectors, collected, strict=True):
            collector.join(timeout=30)
            reader.call.result()

            assert bytes(sink) == payload

        assert errors == []
        assert fanout.moved() == 2 * len(payload)

    @staticmethod
    def _collect(fd: int, sink: bytearray) -> None:
        while chunk := os.read(fd, 1 << 20):
            sink.extend(chunk)

        os.close(fd)


class TestGateOutsideGroup:
    def test_lone_gated_call_commits_without_waiting(self, tmp_path: Path) -> None:
        """Вне группы барьер отвечает сразу (CallGateMode.AUTO): тело с
        StreamGroup фиксирует результат как обычно."""
        launcher = _launcher(tmp_path, CallAmbient())
        command = ToolCommand(
            argv=(
                "python3",
                "-m",
                MODULE,
                "fake_collect",
                "--marker",
                "lone",
                "--fail",
                "false",
                "--gated",
                "true",
            ),
            config=b'{"cfg": {"token": "t0ken", "limit": 5}}',
            inputs=(CallInputSpec(port="feed", raw=False),),
        )

        outcome = CollectedCall.of(launcher, command)

        assert isinstance(outcome.reply, ReplyOk), outcome.reply
        assert (tmp_path / "lone").read_text().startswith("collected 0 frames")
