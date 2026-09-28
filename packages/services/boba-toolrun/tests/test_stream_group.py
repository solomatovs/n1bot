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
    StreamPlan,
    StreamTimings,
)
from boba.toolkit.entry import ToolArgv
from boba.toolkit.launcher import CollectedCall, PayloadFailureError
from boba.toolkit.ports import StreamSpec
from boba.toolkit.protocol import CallInputSpec, ReplyOk, ToolCommand
from boba.toolkit.wrap import ToolProcessWrap
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
    """Вызов группы в тесте: инструмент, аргументы модели и каналы."""

    tool: str
    args: Mapping[str, object]
    output: str | None
    inputs: Sequence[tuple[str, str]]


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
        ):
            copy = tool.model_copy()
            self._tools[copy.name] = copy

        ToolProcessWrap.guard_all(list(self._tools.values()), _launcher(workdir))

    def plan(self, calls: Mapping[str, Call]) -> StreamPlan:
        nodes: list[StreamNode] = []
        for key, call in calls.items():
            nodes.append(self._node(key, call))

        return StreamPlan(nodes)

    def _node(self, key: str, call: Call) -> StreamNode:
        inputs: list[StreamInput] = []
        for port, channel in call.inputs:
            inputs.append(StreamInput(port=port, channel=channel))

        schema = ToolArgv.schema_of(self._tools[call.tool])
        return StreamNode(
            key=key,
            tool=call.tool,
            spec=StreamSpec.of_schema(schema),
            output=call.output,
            inputs=tuple(inputs),
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
        coroutine = self._tools[call.tool].coroutine
        assert coroutine is not None

        token = PipelineSlot.set(group.slot(key))
        try:
            return await coroutine(**call.args, cfg=CFG)
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
    assert result.kind == StreamFailureKind.GROUP_FAILED
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
            message = str(_failure(results[key]))
            assert "fake_emit (src)" in message
            assert "scripted failure after 20 frames" in message

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

        assert "fake_emit (src)" in str(_failure(results["loose"]))
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
            assert "fake_collect (bad)" in str(_failure(results[key]))

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
        group.refuse("ghost", "access denied: role DEV lacks fake_collect")

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
            source.frames_fd,
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
                    target=self._collect, args=(reader.frames_fd, sink), daemon=True
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
