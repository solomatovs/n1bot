"""Вызов инструмента в песочнице: call_text/call_json и контракт ответа."""

from __future__ import annotations

import json
import shlex
from collections.abc import Callable, Mapping, Sequence
from typing import ClassVar, TypeVar

from pydantic import BaseModel, ValidationError

from boba.sandbox.profile import SandboxProfile
from boba.sandbox.runner import SandboxOutcome, SandboxRunner
from boba.toolkit.launcher import (
    LauncherError,
    LaunchOutcome,
    LaunchPayload,
    ToolLauncher,
)

__all__ = [
    "SandboxCaller",
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
        self._runner = SandboxRunner(tool, profile, path_vars)

    def call_text(self, command: str, stdin: str) -> LaunchOutcome:
        """Результат — сам процесс: stdout/stderr/rc отдаются как есть."""
        return self._runner.run(command, stdin=stdin)

    def call_json(
        self,
        entry: Sequence[str],
        request: BaseModel,
        schema: type[M],
    ) -> M:
        """Результат — структура: entry читает запрос со stdin, печатает маркер."""
        if not entry:
            msg = "sandbox: entry must not be empty"
            raise ValueError(msg)
        outcome = self._runner.run(shlex.join(entry), stdin=request.model_dump_json())
        return SandboxPayload.decode(outcome, schema)


M = TypeVar("M", bound=BaseModel)


class SandboxPayloadError(LauncherError):
    """Payload нарушил контракт: результату доверять нельзя."""


class SandboxPayload(LaunchPayload):
    """Разбор результата payload-инструмента; кодирование задаёт контракт."""

    STDERR_TAIL_CHARS: ClassVar[int] = 2000

    @classmethod
    def decode(cls, outcome: SandboxOutcome, schema: type[M]) -> M:
        """Результат из stdout по схеме; при нарушении контракта — ошибка."""
        result = outcome.result
        if result.timed_out:
            msg = f"{outcome.tool}: timed out; {cls._context(outcome)}"
            raise SandboxPayloadError(msg)
        if result.truncated_stdout:
            msg = f"{outcome.tool}: output truncated; {cls._context(outcome)}"
            raise SandboxPayloadError(msg)
        if result.exit_code != 0:
            msg = (
                f"{outcome.tool}: exited with code {result.exit_code}; "
                f"{cls._context(outcome)}"
            )
            raise SandboxPayloadError(msg)

        body = cls._extract(outcome)
        try:
            data = json.loads(body)
        except json.JSONDecodeError as e:
            msg = f"{outcome.tool}: result is not valid JSON: {e}"
            raise SandboxPayloadError(msg) from e
        try:
            return schema.model_validate(data)
        except ValidationError as e:
            msg = f"{outcome.tool}: result does not match {schema.__name__}: {e}"
            raise SandboxPayloadError(msg) from e

    @classmethod
    def _extract(cls, outcome: SandboxOutcome) -> str:
        bodies: list[str] = []
        for line in outcome.result.stdout.splitlines():
            if not line.startswith(cls.MARKER):
                continue
            bodies.append(line[len(cls.MARKER) :])
        if not bodies:
            msg = (
                f"{outcome.tool}: no {cls.MARKER!r} line in output; "
                f"{cls._context(outcome)}"
            )
            raise SandboxPayloadError(msg)
        if len(bodies) > 1:
            msg = (
                f"{outcome.tool}: {len(bodies)} {cls.MARKER!r} lines in output, "
                "exactly one expected"
            )
            raise SandboxPayloadError(msg)
        return bodies[0]

    @classmethod
    def _context(cls, outcome: SandboxOutcome) -> str:
        stderr = outcome.result.stderr.strip()
        tail = stderr[-cls.STDERR_TAIL_CHARS :]
        parts = [f"stderr={tail!r}"]
        if outcome.diagnostic:
            parts.append(outcome.diagnostic)
        return "; ".join(parts)


