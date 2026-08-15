"""Обёртка запуска: локальный режим, песочный через порт, kind'ы отказов."""

from __future__ import annotations

import asyncio

import pytest
from fake_toolmod import FakeConfig
from pydantic import SecretStr

from boba.toolkit.entry import REPLY, ReplyError, ToolCommand, ToolMain
from boba.toolkit.launcher import (
    LaunchOutcome,
    PayloadFailureError,
    RunResult,
    ToolOutcome,
)
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

    import fake_toolmod

    reload(fake_toolmod)
    return ToolMain.toolset(fake_toolmod.fake_echo)[0]


class RecordingLauncher:
    """Порт запуска в тестах: запоминает команду, отдаёт заданный конверт."""

    def __init__(self, reply_json: str) -> None:
        self.commands: list[ToolCommand] = []
        self._reply = reply_json

    def run_tool(self, command: ToolCommand) -> ToolOutcome:
        self.commands.append(command)
        return ToolOutcome(
            reply=REPLY.validate_json(self._reply),
            run=RunResult(
                exit_code=0, stdout="", stderr="", duration_ms=1, timed_out=False
            ),
            diagnostic="",
        )

    def call_text(self, command: str, stdin: str) -> LaunchOutcome:
        raise NotImplementedError


class TestLocalMode:
    """Без launcher'а тело зовётся в процессе, EXPECTED — тем же kind'ом."""

    def test_body_runs_and_returns(self) -> None:
        tool = fresh_tool()
        ToolProcessWrap.guard_all([tool], None)

        assert tool.coroutine is not None
        content, artifact = run_body(tool.coroutine, text="hi", repeat=2, cfg=CFG)

        assert "hi hi|t0ken" in content
        assert isinstance(artifact, TextResult)

    def test_expected_error_maps_to_kind(self) -> None:
        tool = fresh_tool()
        ToolProcessWrap.guard_all([tool], None)

        assert tool.coroutine is not None
        with pytest.raises(PayloadFailureError) as caught:
            run_body(tool.coroutine, text="boom", repeat=1, cfg=CFG)

        assert caught.value.kind == "fake_unavailable"

    def test_unexpected_error_passes_through(self) -> None:
        tool = fresh_tool()
        ToolProcessWrap.guard_all([tool], None)

        assert tool.coroutine is not None
        with pytest.raises(RuntimeError):
            run_body(tool.coroutine, text="crash", repeat=1, cfg=CFG)


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

        assert tool.coroutine is not None
        content, artifact = run_body(tool.coroutine, text="hello", repeat=1, cfg=CFG)

        assert content == "done"
        assert isinstance(artifact, TextResult)

        command = launcher.commands[0]
        assert "-m" in command.argv
        assert "fake_toolmod" in command.argv
        assert "--text" in command.argv
        assert "t0ken" not in " ".join(command.argv)
        assert b"t0ken" in command.stdin

    def test_error_reply_raises_payload_failure(self) -> None:
        tool = fresh_tool()
        reply = '{"status": "error", "kind": "fake_unavailable", "message": "down"}'
        ToolProcessWrap.guard_all([tool], RecordingLauncher(reply))

        assert tool.coroutine is not None
        with pytest.raises(PayloadFailureError) as caught:
            run_body(tool.coroutine, text="x", repeat=1, cfg=CFG)

        assert caught.value.kind == "fake_unavailable"
        assert "down" in str(caught.value)

    def test_oversized_argument_is_expected_failure(self) -> None:
        tool = fresh_tool()
        ToolProcessWrap.guard_all([tool], RecordingLauncher(self.OK_REPLY))

        assert tool.coroutine is not None
        with pytest.raises(PayloadFailureError) as caught:
            run_body(tool.coroutine, text="x" * 140_000, repeat=1, cfg=CFG)

        assert caught.value.kind == str(WrapErrorKind.ARGUMENT_TOO_LARGE)

    def test_error_reply_never_reaches_return(self) -> None:
        """Отказ — исключение, а не «успешный» результат с ok=False."""
        tool = fresh_tool()
        reply = '{"status": "error", "kind": "k", "message": "m"}'
        launcher = RecordingLauncher(reply)
        ToolProcessWrap.guard_all([tool], launcher)

        assert tool.coroutine is not None
        with pytest.raises(PayloadFailureError):
            run_body(tool.coroutine, text="x", repeat=1, cfg=CFG)

        assert isinstance(launcher.commands, list)
        assert isinstance(
            REPLY.validate_json(reply), ReplyError
        )
