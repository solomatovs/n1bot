"""Обёртка запуска: вызов через порт, ошибки конвертом результата-ошибки."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator, Sequence

import pytest
from pydantic import SecretStr

from boba.stand_core.fake_toolmod import FakeConfig, fake_echo
from boba.toolkit.chain import CallAmbient
from boba.toolkit.frames import ToolFrame
from boba.toolkit.launcher import (
    CallGate,
    CallInputPort,
    PayloadFailureError,
    RunResult,
    TappedCall,
    ToolCall,
    ToolLauncher,
    ToolOutcome,
)
from boba.toolkit.manifest import ManifestBuilder
from boba.toolkit.protocol import REPLY, ReplyError, ToolCommand
from boba.toolkit.result import ErrorResult, MarkdownResult, ToolResultBase
from boba.toolrun.hosted import HostedTool, PluginSection
from boba.toolrun.injected import InjectedConfig, StaticSection
from boba.toolrun.wrap import ToolProcessWrap

CFG = FakeConfig(token=SecretStr("t0ken"), limit=5)


def called(tool: HostedTool, text: str) -> ToolResultBase:
    """Один вызов инструмента под обёрткой запуска с аргументом text."""
    return asyncio.run(tool.run("call-1", {"text": text, "repeat": 1}))


def wrapped(launcher: ToolLauncher) -> HostedTool:
    """Инструмент хоста из объявления fake_echo с переносом вызова
    исполнителю launcher и секцией плагина на месте конфига."""
    decl = ManifestBuilder().tool(fake_echo)
    section = PluginSection(values=CFG.revealed())

    tool = ToolProcessWrap(CallAmbient()).hosted(decl, launcher, section)
    InjectedConfig(StaticSection()).bind_all([tool])

    return tool


class RecordedCall(ToolCall):
    """Вызов-заглушка: кадры и конверт заданы тестом."""

    def __init__(self, reply_json: str, frames: Sequence[ToolFrame] = ()) -> None:
        self._reply = reply_json
        self._frames = tuple(frames)

    def inputs(self) -> Sequence[CallInputPort]:
        return ()

    def gate(self) -> CallGate:
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


class TestSandboxMode:
    """С launcher'ом вызов уезжает командой; конверт разбирается в возврат."""

    OK_REPLY = (
        '{"status": "ok", "content": "done",'
        ' "artifact": {"kind": "markdown", "ok": true, "text": "done"}}'
    )

    def test_call_is_rendered_and_reply_returned(self) -> None:
        launcher = RecordingLauncher(self.OK_REPLY)

        artifact = called(wrapped(launcher), "hello")

        if not (isinstance(artifact, MarkdownResult)):
            raise AssertionError("isinstance(artifact, MarkdownResult)")
        if artifact.text != "done":
            raise AssertionError('artifact.text == "done"')

        command = launcher.commands[0]
        if command.argv != ("fake_echo",):
            raise AssertionError(f"argv names only the tool: {command.argv!r}")
        if b'"text": "hello"' not in command.args:
            raise AssertionError(f"model argument in args: {command.args!r}")
        if b"t0ken" not in command.args:
            raise AssertionError('b"t0ken" in command.args')

    def test_error_reply_raises_payload_failure(self) -> None:
        failure = ErrorResult(message="down", error_kind="fake_unavailable")
        reply = ReplyError(failure=failure).model_dump_json()
        tool = wrapped(RecordingLauncher(reply))

        with pytest.raises(PayloadFailureError) as caught:
            called(tool, "x")

        if caught.value.failure() != failure:
            raise AssertionError(f"failure: {caught.value.failure()!r}")
        if "down" not in str(caught.value):
            raise AssertionError('"down" in str(caught.value)')

    def test_large_argument_rides_the_channel(self) -> None:
        """Аргументы едут каналом аргументов: потолка argv у них нет."""
        launcher = RecordingLauncher(self.OK_REPLY)

        called(wrapped(launcher), "x" * 140_000)

        if len(launcher.commands[0].args) < 140_000:
            raise AssertionError("the large argument reached the command whole")

    def test_error_reply_never_reaches_return(self) -> None:
        """Отказ — исключение, а не «успешный» результат с ok=False."""
        reply = ReplyError(
            failure=ErrorResult(message="m", error_kind="k")
        ).model_dump_json()
        launcher = RecordingLauncher(reply)
        tool = wrapped(launcher)

        with pytest.raises(PayloadFailureError):
            called(tool, "x")

        if not (isinstance(launcher.commands, list)):
            raise AssertionError("isinstance(launcher.commands, list)")
        if not (isinstance(REPLY.validate_json(reply), ReplyError)):
            raise AssertionError("isinstance( REPLY.validate_json(reply), ReplyError )")
