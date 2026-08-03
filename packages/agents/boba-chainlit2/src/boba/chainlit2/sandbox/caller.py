"""Единая точка вызова инструмента в песочнице.

Запуск у всех инструментов один — SandboxRunner. Различается только разбор
вывода: bash отдаёт процесс как есть (call_text), python-инструменты идут по
payload-контракту (call_json).
"""

from __future__ import annotations

import shlex
from collections.abc import Callable, Mapping, Sequence
from typing import TypeVar

from pydantic import BaseModel

from boba.chainlit2.sandbox.payload import SandboxPayload
from boba.chainlit2.sandbox.profile import SandboxProfile
from boba.chainlit2.sandbox.runner import SandboxOutcome, SandboxRunner

__all__ = ["SandboxCaller"]

M = TypeVar("M", bound=BaseModel)


class SandboxCaller:
    """Вызывает инструмент в песочнице и приводит его вывод к результату."""

    def __init__(
        self,
        tool: str,
        profile: SandboxProfile,
        path_vars: Callable[[], Mapping[str, str]],
    ) -> None:
        self._runner = SandboxRunner(tool, profile, path_vars)

    def call_text(self, command: str, stdin: str) -> SandboxOutcome:
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
