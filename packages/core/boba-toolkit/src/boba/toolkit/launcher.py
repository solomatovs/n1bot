"""Порт запуска инструмента: чем инструмент пользуется, не зная про песочницу.

Инструменты зависят только от этого модуля: протокол ToolLauncher и данные одного
запуска. Реализация (bwrap, cgroup, subprocess) подставляется снаружи.

Ошибки: LauncherError — исполнитель нарушил контракт, результату доверять нельзя.
"""

from __future__ import annotations

from abc import abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass
from typing import ClassVar, Protocol, TypeVar

from pydantic import BaseModel

__all__ = [
    "LaunchOutcome",
    "LaunchPayload",
    "LauncherError",
    "LauncherFactory",
    "RunResult",
    "ToolLauncher",
]

M = TypeVar("M", bound=BaseModel)


class LauncherError(RuntimeError):
    """Исполнитель нарушил контракт: результату доверять нельзя."""


class LaunchPayload:
    """Контракт ответа: ровно одна строка `sandbox-result:{json}` в stdout."""

    MARKER: ClassVar[str] = "sandbox-result:"

    @classmethod
    def encode(cls, data: BaseModel) -> str:
        """Строка результата; печатается payload-скриптом в stdout."""
        return f"{cls.MARKER}{data.model_dump_json()}"


@dataclass(frozen=True)
class RunResult:
    """Результат запуска: код возврата, потоки, факт обрезки, таймаут."""

    exit_code: int
    stdout: str
    stderr: str
    truncated_stdout: bool
    truncated_stderr: bool
    duration_ms: int
    timed_out: bool


class LaunchOutcome:
    """Результат запуска: процессные поля плюс объяснение упёршегося лимита."""

    def __init__(self, tool: str, result: RunResult, diagnostic: str) -> None:
        self.tool = tool
        self.result = result
        self.diagnostic = diagnostic

    @property
    def succeeded(self) -> bool:
        if self.result.timed_out:
            return False
        return self.result.exit_code == 0


class ToolLauncher(Protocol):
    """Запуск инструмента в изолированном окружении.

    call_text отдаёт процесс как есть, call_json — разобранную структуру ответа.
    """

    @abstractmethod
    def call_text(self, command: str, stdin: str) -> LaunchOutcome:
        """Выполнить команду; stdout/stderr/rc возвращаются без разбора."""
        ...

    @abstractmethod
    def call_json(
        self,
        entry: Sequence[str],
        request: BaseModel,
        schema: type[M],
    ) -> M:
        """Выполнить entry: запрос уходит в stdin, ответ разбирается в schema."""
        ...


class LauncherFactory(Protocol):
    """Выдаёт исполнителя по метке инструмента; окружение выбирает приложение."""

    @abstractmethod
    def __call__(self, tool: str, /) -> ToolLauncher:
        """Исполнитель для инструмента tool (метка идёт в логи и диагностику)."""
        ...
