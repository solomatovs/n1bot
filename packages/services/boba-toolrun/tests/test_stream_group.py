"""Группа связанных каналами вызовов на настоящих субпроцессах.

Вызовы идут тем же путём, что из чата: обёртка запуска (ToolProcessWrap)
видит ручку группы в PipelineSlot, открывает вызов потоково и отдаёт группе
каналы; раздача ChannelFanOut соединяет выход писателя со входами читателей.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

from boba.stand_core.fake_toolmod import (
    FakeConfig,
    fake_collect,
    fake_deaf,
    fake_emit,
    fake_head,
    fake_merge,
    fake_shard,
    fake_split,
    fake_stream,
)
from boba.toolkit.chain import (
    ChannelFanOut,
    PipelineSlot,
    PipeTee,
    StreamFailureKind,
    StreamGroupRun,
    StreamInput,
    StreamNode,
    StreamOutput,
    StreamPlan,
    StreamPlanError,
    StreamTimings,
)
from boba.toolkit.entry import ToolArgv
from boba.toolkit.launcher import CollectedCall, PayloadFailureError
from boba.toolkit.ports import ToolStreamSpecs
from boba.toolkit.protocol import CallInputSpec, CallOutputSpec, ReplyOk, ToolCommand
from boba.toolkit.result import (
    ErrorResult,
    GroupCall,
    GroupFailureResult,
    MarkdownResult,
)
from boba.toolkit.wrap import ToolProcessWrap
from boba.toolrun.dev_null import DevNullTool
from boba.toolrun.process import ProcessLauncherConfig, ProcessToolCaller

CFG = FakeConfig(token=SecretStr("t0ken"), limit=5)
MODULE = "boba.stand_core.fake_toolmod"
FAST = StreamTimings(open_sec=20.0, stall_sec=3.0, poll_sec=0.1)


def _launcher(workdir: Path) -> ProcessToolCaller:
    cfg = ProcessLauncherConfig(
        provider="process",
        workdir=str(workdir),
        timeout_sec=60.0,
        channel_limit_bytes=4_000_000,
        stderr_tail_bytes=8192,
        kill_grace_sec=0.5,
    )
    return ProcessToolCaller("stream-group", cfg)


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
    """Инструменты стенда под обёрткой запуска и прогон группы вызовов."""

    def __init__(self, workdir: Path) -> None:
        self._workdir = workdir
        self._tools: dict[str, Any] = {}
        for tool in (
            fake_emit,
            fake_collect,
            fake_head,
            fake_stream,
            fake_merge,
            fake_deaf,
            fake_split,
            fake_shard,
        ):
            copy = tool.model_copy()
            self._tools[copy.name] = copy

        ToolProcessWrap.guard_all(list(self._tools.values()), _launcher(workdir))

        built = DevNullTool.build()
        self._tools[built.name] = built

    def plan(self, calls: Mapping[str, Call]) -> StreamPlan:
        nodes: list[StreamNode] = []
        for key, call in calls.items():
            nodes.append(self._node(key, call))

        return StreamPlan(nodes)

    def _node(self, key: str, call: Call) -> StreamNode:
        inputs: list[StreamInput] = []
        for port, channel in call.inputs:
            inputs.append(StreamInput(port=port, channel=channel))

        spec = ToolStreamSpecs.of(call.tool)

        outputs: list[StreamOutput] = []
        if call.output is not None:
            outputs.append(
                StreamOutput(port=spec.outbound()[0].name, channel=call.output)
            )

        for port, channel in call.outputs:
            outputs.append(StreamOutput(port=port, channel=channel))

        return StreamNode(
            key=key,
            tool=call.tool,
            spec=spec,
            outputs=tuple(outputs),
            inputs=tuple(inputs),
            pipe_bytes=call.pipe_bytes,
        )

    async def run(
        self,
        calls: Mapping[str, Call],
        timings: StreamTimings = FAST,
        skip: Sequence[str] = (),
    ) -> tuple[StreamGroupRun, dict[str, Any]]:
        group = StreamGroupRun(self.plan(calls), timings)

        tasks: dict[str, asyncio.Task[Any]] = {}
        for key, call in calls.items():
            if key in skip:
                continue

            tasks[key] = asyncio.create_task(self._one(group, key, call))

        results: dict[str, Any] = {}
        for key, task in tasks.items():
            results[key] = await asyncio.wait_for(task, timeout=60)

        return group, results

    async def _one(self, group: StreamGroupRun, key: str, call: Call) -> Any:
        tool = self._tools[call.tool]
        coroutine = tool.coroutine
        assert coroutine is not None

        kwargs = dict(call.args)
        if "cfg" in ToolArgv.schema_of(tool).model_fields:
            kwargs["cfg"] = CFG

        token = PipelineSlot.set(group.slot(key))
        try:
            return await coroutine(**kwargs)
        except PayloadFailureError as exc:
            return exc
        finally:
            PipelineSlot.reset(token)

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


def _content(result: Any) -> str:
    assert not isinstance(result, BaseException), result
    content, _artifact = result
    return str(content)


def _failure(result: Any) -> PayloadFailureError:
    assert isinstance(result, PayloadFailureError), result
    assert result.failure().error_kind == StreamFailureKind.GROUP_FAILED
    return result


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
            failure = _failure(results[key]).failure()
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

        failure = _failure(results["loose"]).failure()
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
            failure = _failure(results[key]).failure()
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

        token = PipelineSlot.set(group.slot("src"))
        try:
            coroutine = stand._tools["fake_emit"].coroutine
            with pytest.raises(PayloadFailureError, match="access denied"):
                await coroutine(**calls["src"].args, cfg=CFG)
        finally:
            PipelineSlot.reset(token)

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

        _group, results = await stand.run(calls, timings, skip=("ghost",))

        message = str(_failure(results["src"]))
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
            message = str(_failure(results[key]))
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
        texts = [str(_failure(results[key])) for key in ("src", "sink")]
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
        dropped = results["drop"]
        assert isinstance(dropped, MarkdownResult), dropped
        assert "discarded bytes by channel" in dropped.text
        assert "o: " in dropped.text

    @pytest.mark.anyio
    async def test_dev_null_outside_a_group_is_refused(self) -> None:
        built = DevNullTool.build()
        coroutine = built.coroutine
        assert coroutine is not None

        with pytest.raises(PayloadFailureError, match="only in a group"):
            await coroutine(feeds=["ghost"])


class TestRawFanOut:
    def test_raw_stream_reaches_every_reader_verbatim(self, tmp_path: Path) -> None:
        """Сырой поток fake_relay раздаётся двум fake_relay напрямую через
        ChannelFanOut: байты у обоих совпадают с исходными."""
        launcher = _launcher(tmp_path)
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
        launcher = _launcher(tmp_path)
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
