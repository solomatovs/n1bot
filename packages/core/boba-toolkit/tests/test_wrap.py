"""Обёртка запуска: вызов через порт, kind'ы отказов."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator

import pytest
from pydantic import SecretStr

from boba.stand.fake_toolmod import FakeConfig
from boba.toolkit.entry import ToolMain
from boba.toolkit.frames import ToolFrame
from boba.toolkit.launcher import (
    LaunchOutcome,
    PayloadFailureError,
    RunResult,
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
    """Вызов-заглушка: кадров у тела нет, конверт задан тестом."""

    def __init__(self, reply_json: str) -> None:
        self._reply = reply_json

    def send(self, frame: ToolFrame) -> None:
        raise NotImplementedError

    def done_sending(self) -> None:
        return

    def frames(self) -> Iterator[ToolFrame]:
        return iter(())

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

    def __init__(self, reply_json: str) -> None:
        self.commands: list[ToolCommand] = []
        self._reply = reply_json

    def open(self, command: ToolCommand) -> ToolCall:
        self.commands.append(command)
        return RecordedCall(self._reply)

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
