"""Вызов инструмента в песочнице: run_tool и call_text.

Ошибки:
SandboxPayloadError — канальный запуск не отдал конверт либо конверт не по
    контракту.
SandboxLaunchError/SandboxMountError — из SandboxRunner при сбое запуска.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import ClassVar

from pydantic import ValidationError

from boba.sandbox.channels import ChannelSet, TailSink
from boba.sandbox.profile import SandboxProfile
from boba.sandbox.runner import SandboxRunner
from boba.toolkit.channels import ToolChannel
from boba.toolkit.entry import REPLY, ToolCommand
from boba.toolkit.launcher import (
    LauncherError,
    LaunchOutcome,
    ToolLauncher,
    ToolOutcome,
)
from boba.toolkit.stream import ToolChannelsTap

__all__ = [
    "SandboxCaller",
    "SandboxPayloadError",
]


class _EnvelopeSink:
    """Сборка конверта tool_result: канал маленький, копится целиком."""

    def __init__(self) -> None:
        self._data = bytearray()

    def feed(self, chunk: bytes) -> None:
        self._data.extend(chunk)

    def data(self) -> bytes:
        return bytes(self._data)


class SandboxPayloadError(LauncherError):
    """Процесс инструмента нарушил контракт: результату доверять нельзя."""


class SandboxCaller(ToolLauncher):
    """Реализация ToolLauncher: bwrap-песочница как окружение запуска."""

    STDERR_TAIL_CHARS: ClassVar[int] = 2000

    def __init__(
        self,
        tool: str,
        profile: SandboxProfile,
        path_vars: Callable[[], Mapping[str, str]],
    ) -> None:
        self._tool = tool
        self._runner = SandboxRunner(tool, profile, path_vars)

    def call_text(self, command: str, stdin: str) -> LaunchOutcome:
        """Результат — сам процесс: stdout/stderr/rc отдаются как есть."""
        return self._runner.run(command, stdin=stdin, stdout_sink=None)

    def run_tool(self, command: ToolCommand) -> ToolOutcome:
        """Команда модуля инструментов: каналы врозь, конверт из tool_result.

        Приёмники журнала берутся из контекста вызова (ToolChannelsTap);
        хвост tool_stderr и конверт собираются свои — они нужны для разбора
        итога независимо от журнала.
        """
        channels = ChannelSet.open(ToolChannel)

        envelope = _EnvelopeSink()
        stderr_tail = TailSink()
        channels.add_sink(ToolChannel.RESULT, envelope.feed)
        channels.add_sink(ToolChannel.STDERR, stderr_tail.feed)

        if sinks := ToolChannelsTap.get():
            for channel in (
                ToolChannel.STDOUT,
                ToolChannel.STDERR,
                ToolChannel.RESULT,
            ):
                journal = sinks.sink_of(channel)
                channels.add_sink(channel, journal.feed)

        outcome = self._runner.run_with_channels(command.argv, command.stdin, channels)

        reply_raw = envelope.data()
        if not reply_raw:
            result = outcome.result
            wrap_tail = result.stderr.strip()[-self.STDERR_TAIL_CHARS :]
            msg = (
                f"{outcome.tool}: no envelope on tool_result "
                f"(rc={result.exit_code}, timed_out={result.timed_out}); "
                f"tool_stderr={stderr_tail.text()!r}; wrap_stderr={wrap_tail!r}"
            )

            if diagnostic := self._runner.diagnose(result, stderr_tail.text()):
                msg = f"{msg}; {diagnostic}"

            raise SandboxPayloadError(msg)

        try:
            reply = REPLY.validate_json(reply_raw)
        except ValidationError as e:
            msg = f"{outcome.tool}: envelope does not match contract: {e}"
            raise SandboxPayloadError(msg) from e

        return ToolOutcome(
            reply=reply, run=outcome.result, diagnostic=outcome.diagnostic
        )
