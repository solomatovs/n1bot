"""Вызов инструмента в песочнице: call_text/call_stream и контракт ответа.

Ошибки: SandboxPayloadError — payload нарушил потоковый контракт; исключения
sink'а проходят наверх как есть — их контракт задаёт потребитель потока.
"""

from __future__ import annotations

import json
import shlex
from collections.abc import Callable, Mapping, Sequence
from typing import ClassVar, TypeVar

from pydantic import BaseModel, ValidationError

from boba.sandbox.profile import SandboxProfile
from boba.sandbox.runner import SandboxOutcome, SandboxRunner
from boba.toolkit.launcher import (
    ChunkSink,
    LauncherError,
    LaunchOutcome,
    LaunchPayload,
    ToolLauncher,
)

__all__ = [
    "SandboxCaller",
    "SandboxFrameDecoder",
    "SandboxPayload",
    "SandboxPayloadError",
]

M = TypeVar("M", bound=BaseModel)


class SandboxCaller(ToolLauncher):
    """Реализация ToolLauncher: bwrap-песочница как окружение запуска."""

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

    def call_stream(
        self,
        entry: Sequence[str],
        request: BaseModel,
        sink: ChunkSink,
        trailer: type[M],
    ) -> M:
        """Entry читает запрос со stdin; кадры идут в sink по мере появления."""
        if not entry:
            msg = "sandbox: entry must not be empty"
            raise ValueError(msg)
        decoder = SandboxFrameDecoder(self._tool, sink)
        outcome = self._runner.run(
            shlex.join(entry),
            stdin=request.model_dump_json(),
            stdout_sink=decoder.feed,
        )
        return decoder.finish(outcome, trailer)


class SandboxPayloadError(LauncherError):
    """Payload нарушил контракт: результату доверять нельзя."""


class SandboxPayload(LaunchPayload):
    """Маркеры и кодирование кадров/трейлера; разбор делает SandboxFrameDecoder."""

    STDERR_TAIL_CHARS: ClassVar[int] = 2000

    @classmethod
    def context(cls, outcome: SandboxOutcome) -> str:
        stderr = outcome.result.stderr.strip()
        tail = stderr[-cls.STDERR_TAIL_CHARS :]
        parts = [f"stderr={tail!r}"]
        if outcome.diagnostic:
            parts.append(outcome.diagnostic)
        return "; ".join(parts)


class SandboxFrameDecoder:
    """Инкрементальный разбор stdout payload'а: кадры → sink, трейлер → схема."""

    MAX_LINE_BYTES: ClassVar[int] = 4 * 1024 * 1024

    def __init__(self, tool: str, sink: ChunkSink) -> None:
        self._tool = tool
        self._sink = sink
        self._tail = bytearray()
        self._trailers: list[str] = []
        self._chunk_after_trailer = False

    def feed(self, data: bytes) -> None:
        """Приём сырых байт по мере чтения из процесса; кадры уходят сразу."""
        self._tail.extend(data)
        while True:
            index = self._tail.find(b"\n")
            if index < 0:
                break
            line = bytes(self._tail[:index]).decode("utf-8", errors="replace")
            del self._tail[: index + 1]
            self._line(line)
        if len(self._tail) > self.MAX_LINE_BYTES:
            msg = (
                f"{self._tool}: output line exceeds {self.MAX_LINE_BYTES} bytes; "
                "payload must emit data as chunk frames"
            )
            raise SandboxPayloadError(msg)

    def finish(self, outcome: SandboxOutcome, schema: type[M]) -> M:
        """Трейлер по схеме; при нарушении контракта — SandboxPayloadError."""
        self._flush_tail()
        result = outcome.result
        if result.timed_out:
            msg = f"{outcome.tool}: timed out; {SandboxPayload.context(outcome)}"
            raise SandboxPayloadError(msg)
        if result.exit_code != 0:
            msg = (
                f"{outcome.tool}: exited with code {result.exit_code}; "
                f"{SandboxPayload.context(outcome)}"
            )
            raise SandboxPayloadError(msg)

        if not self._trailers:
            msg = (
                f"{outcome.tool}: no {SandboxPayload.MARKER!r} line in output; "
                f"{SandboxPayload.context(outcome)}"
            )
            raise SandboxPayloadError(msg)
        if len(self._trailers) > 1:
            msg = (
                f"{outcome.tool}: {len(self._trailers)} {SandboxPayload.MARKER!r} "
                "lines in output, exactly one expected"
            )
            raise SandboxPayloadError(msg)
        if self._chunk_after_trailer:
            msg = f"{outcome.tool}: data chunk after trailer line"
            raise SandboxPayloadError(msg)

        try:
            data = json.loads(self._trailers[0])
        except json.JSONDecodeError as e:
            msg = f"{outcome.tool}: result is not valid JSON: {e}"
            raise SandboxPayloadError(msg) from e
        try:
            return schema.model_validate(data)
        except ValidationError as e:
            msg = f"{outcome.tool}: result does not match {schema.__name__}: {e}"
            raise SandboxPayloadError(msg) from e

    def _flush_tail(self) -> None:
        if not self._tail:
            return
        line = bytes(self._tail).decode("utf-8", errors="replace")
        self._tail.clear()
        self._line(line)

    def _line(self, line: str) -> None:
        if line.startswith(SandboxPayload.CHUNK_MARKER):
            self._chunk(line[len(SandboxPayload.CHUNK_MARKER) :])
            return
        if line.startswith(SandboxPayload.MARKER):
            self._trailers.append(line[len(SandboxPayload.MARKER) :])
            return
        # прочие строки stdout — шум библиотек, игнорируются как и раньше

    def _chunk(self, body: str) -> None:
        if self._trailers:
            self._chunk_after_trailer = True
            return
        try:
            chunk = json.loads(body)
        except json.JSONDecodeError as e:
            msg = f"{self._tool}: chunk frame is not valid JSON: {e}"
            raise SandboxPayloadError(msg) from e
        if not isinstance(chunk, str):
            msg = f"{self._tool}: chunk frame must be a JSON string"
            raise SandboxPayloadError(msg)
        self._sink.write(chunk)
