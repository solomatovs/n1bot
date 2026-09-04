"""Обёртка запуска: вызов через порт, kind'ы отказов."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator, Sequence

import pytest
from pydantic import SecretStr

from boba.stand.fake_toolmod import FakeConfig
from boba.toolkit.entry import ToolMain
from boba.toolkit.frames import FrameHead, ToolFrame
from boba.toolkit.launcher import (
    FrameSink,
    FrameTap,
    LaunchOutcome,
    PayloadFailureError,
    RunResult,
    TappedCall,
    ToolCall,
    ToolLauncher,
    ToolOutcome,
)
from boba.toolkit.protocol import REPLY, ReplyError, ToolCommand
from boba.toolkit.result import TextResult
from boba.toolkit.wrap import ToolProcessWrap, WrapErrorKind

CFG = FakeConfig(token=SecretStr("t0ken"), limit=5)


def run_body(body, **kwargs):
    """await coroutine-тела в тестах: Awaitable оборачивается корутиной."""

    async def go():
        return await body(**kwargs)

    return asyncio.run(go())


def fresh_tool():
    """Свежий tool-объект: guard_all подменяет тела на месте."""
    from importlib import reload

    from boba.stand import fake_toolmod

    reload(fake_toolmod)
    return ToolMain.toolset(fake_toolmod.fake_echo)[0]


class RecordedCall(ToolCall):
    """Вызов-заглушка: кадры и конверт заданы тестом."""

    def __init__(self, reply_json: str, frames: Sequence[ToolFrame] = ()) -> None:
        self._reply = reply_json
        self._frames = tuple(frames)

    def send(self, frame: ToolFrame) -> None:
        raise NotImplementedError

    def done_sending(self) -> None:
        return

    def frames(self) -> Iterator[ToolFrame]:
        return iter(self._frames)

    def result(self) -> ToolOutcome:
        return ToolOutcome(
            reply=REPLY.validate_json(self._reply),
            run=RunResult(
                exit_code=0, stdout="", stderr="", duration_ms=1, timed_out=False
            ),
            diagnostic="",
        )

    def close(self) -> None:
        return


class RecordingLauncher(ToolLauncher):
    """Порт запуска в тестах: запоминает команду, отдаёт заданный конверт."""

    def __init__(self, reply_json: str, frames: Sequence[ToolFrame] = ()) -> None:
        self.commands: list[ToolCommand] = []
        self._reply = reply_json
        self._frames = tuple(frames)

    def open(self, command: ToolCommand) -> ToolCall:
        self.commands.append(command)
        return RecordedCall(self._reply, self._frames)

    def open_tap(self, command: ToolCommand) -> TappedCall:
        raise NotImplementedError

    def call_text(self, command: str, stdin: str) -> LaunchOutcome:
        raise NotImplementedError


class TestSandboxMode:
    """С launcher'ом вызов уезжает командой; конверт разбирается в возврат."""

    OK_REPLY = (
        '{"status": "ok", "content": "done",'
        ' "artifact": {"kind": "text", "ok": true, "text": "done"}}'
    )

    def test_call_is_rendered_and_reply_returned(self) -> None:
        tool = fresh_tool()
        launcher = RecordingLauncher(self.OK_REPLY)
        ToolProcessWrap.guard_all([tool], launcher)

        if tool.coroutine is None:
            raise AssertionError("tool.coroutine is not None")
        content, artifact = run_body(tool.coroutine, text="hello", repeat=1, cfg=CFG)

        if content != "done":
            raise AssertionError('content == "done"')
        if not (isinstance(artifact, TextResult)):
            raise AssertionError("isinstance(artifact, TextResult)")

        command = launcher.commands[0]
        if "-m" not in command.argv:
            raise AssertionError('"-m" in command.argv')
        if "boba.stand.fake_toolmod" not in command.argv:
            raise AssertionError('"boba.stand.fake_toolmod" in command.argv')
        if "--text" not in command.argv:
            raise AssertionError('"--text" in command.argv')
        if "t0ken" in " ".join(command.argv):
            raise AssertionError('"t0ken" not in " ".join(command.argv)')
        if b"t0ken" not in command.config:
            raise AssertionError('b"t0ken" in command.config')

    def test_error_reply_raises_payload_failure(self) -> None:
        tool = fresh_tool()
        reply = '{"status": "error", "kind": "fake_unavailable", "message": "down"}'
        ToolProcessWrap.guard_all([tool], RecordingLauncher(reply))

        if tool.coroutine is None:
            raise AssertionError("tool.coroutine is not None")
        with pytest.raises(PayloadFailureError) as caught:
            run_body(tool.coroutine, text="x", repeat=1, cfg=CFG)

        if caught.value.kind != "fake_unavailable":
            raise AssertionError('caught.value.kind == "fake_unavailable"')
        if "down" not in str(caught.value):
            raise AssertionError('"down" in str(caught.value)')

    def test_oversized_argument_is_expected_failure(self) -> None:
        tool = fresh_tool()
        ToolProcessWrap.guard_all([tool], RecordingLauncher(self.OK_REPLY))

        if tool.coroutine is None:
            raise AssertionError("tool.coroutine is not None")
        with pytest.raises(PayloadFailureError) as caught:
            run_body(tool.coroutine, text="x" * 140_000, repeat=1, cfg=CFG)

        if caught.value.kind != str(WrapErrorKind.ARGUMENT_TOO_LARGE):
            raise AssertionError("caught.value.kind == str(WrapErrorKind.ARGUMENT_TOO…")

    def test_error_reply_never_reaches_return(self) -> None:
        """Отказ — исключение, а не «успешный» результат с ok=False."""
        tool = fresh_tool()
        reply = '{"status": "error", "kind": "k", "message": "m"}'
        launcher = RecordingLauncher(reply)
        ToolProcessWrap.guard_all([tool], launcher)

        if tool.coroutine is None:
            raise AssertionError("tool.coroutine is not None")
        with pytest.raises(PayloadFailureError):
            run_body(tool.coroutine, text="x", repeat=1, cfg=CFG)

        if not (isinstance(launcher.commands, list)):
            raise AssertionError("isinstance(launcher.commands, list)")
        if not (isinstance(REPLY.validate_json(reply), ReplyError)):
            raise AssertionError("isinstance( REPLY.validate_json(reply), ReplyError )")


class Collected(FrameSink):
    """Приёмник кадров теста: копит, что отдала обёртка."""

    def __init__(self) -> None:
        self.frames: list[ToolFrame] = []

    def take(self, frame: ToolFrame) -> None:
        self.frames.append(frame)


class TestFrameTap:
    """С приёмником кадров в контексте обёртка отдаёт кадры тела ему, без
    приёмника — дочитывает в никуда; конверт разбирается одинаково."""

    OK_REPLY = TestSandboxMode.OK_REPLY

    def test_frames_reach_the_sink_only_inside_the_tap(self) -> None:
        frames = (
            ToolFrame.of(FrameHead(kind="one"), b"a"),
            ToolFrame.of(FrameHead(kind="two"), b"bb"),
        )
        tool = fresh_tool()
        launcher = RecordingLauncher(self.OK_REPLY, frames)
        ToolProcessWrap.guard_all([tool], launcher)
        if tool.coroutine is None:
            raise AssertionError("tool.coroutine is not None")

        sink = Collected()
        with FrameTap.applied(sink):
            content, _artifact = run_body(
                tool.coroutine, text="hello", repeat=1, cfg=CFG
            )

        assert content == "done"
        assert [frame.kind for frame in sink.frames] == ["one", "two"]
        assert sink.frames[1].body == b"bb"
        assert FrameTap.get() is None

        outside = Collected()
        run_body(tool.coroutine, text="hello", repeat=1, cfg=CFG)
        assert outside.frames == []
