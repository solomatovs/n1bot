"""Контракт результата payload-инструмента: строка с маркером и JSON в stdout.

Скрипт внутри песочницы читает запрос со stdin и печатает ровно одну строку
`sandbox-result:{json}`. Всё остальное в stdout/stderr — свободный лог. Любое
отклонение от контракта (обрезанный вывод, ненулевой код, нет маркера, битый
JSON) считается ошибкой: молча подставлять частичный результат нельзя.
"""

from __future__ import annotations

import json
from typing import ClassVar, TypeVar

from pydantic import BaseModel, ValidationError

from boba.toolkit.sandbox.runner import SandboxOutcome

__all__ = ["SandboxPayload", "SandboxPayloadError"]

M = TypeVar("M", bound=BaseModel)


class SandboxPayloadError(RuntimeError):
    """Payload нарушил контракт: результату доверять нельзя."""


class SandboxPayload:
    """Кодирование и разбор результата payload-инструмента."""

    MARKER: ClassVar[str] = "sandbox-result:"
    STDERR_TAIL_CHARS: ClassVar[int] = 2000

    @classmethod
    def encode(cls, data: BaseModel) -> str:
        """Строка результата; печатается payload-скриптом в stdout."""
        body = data.model_dump_json()
        return f"{cls.MARKER}{body}"

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
