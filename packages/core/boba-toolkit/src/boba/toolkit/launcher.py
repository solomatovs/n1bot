"""Порт запуска инструмента: чем инструмент пользуется, не зная про песочницу.

Инструменты зависят только от этого модуля: протокол ToolLauncher и данные одного
запуска. Реализация (bwrap, cgroup, subprocess) подставляется снаружи.

Ошибки:
LauncherError — исполнитель нарушил контракт, результату доверять нельзя.
PayloadFailureError — инструмент сообщил об ожидаемом отказе конвертом.
"""

from __future__ import annotations

import json
from abc import abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, ClassVar, Protocol

from pydantic import BaseModel, ConfigDict, TypeAdapter

from boba.toolkit.protocol import ReplyError, ReplyOk, ToolCommand

__all__ = [
    "ClippedText",
    "ErrorKind",
    "LaunchOutcome",
    "LaunchPayload",
    "LauncherError",
    "LauncherFactory",
    "PayloadFailureError",
    "RowStream",
    "RunResult",
    "ToolLauncher",
    "ToolOutcome",
]


class LauncherError(RuntimeError):
    """Исполнитель нарушил контракт: результату доверять нельзя."""


class PayloadFailureError(LauncherError):
    """Ожидаемая ошибка операции: payload объявил её и назвал причину.

    Не нарушение контракта: поток отработал штатно, операция сообщила отказ.
    Текст пригоден для показа пользователю и LLM — трейсбека в нём нет.
    """

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind


class ErrorKind:
    """Классификация ошибки для ErrorResult: имя kind'а или имя класса."""

    @staticmethod
    def of(error: Exception) -> str:
        if isinstance(error, PayloadFailureError):
            return error.kind
        return type(error).__name__


class LaunchPayload:
    """Кадры `sandbox-log:` в stderr: лог процесса песочницы для релея хоста."""

    LOG_MARKER: ClassVar[str] = "sandbox-log:"

    @classmethod
    def encode_log(cls, level: str, name: str, message: str) -> str:
        """Строка лога: многострочное сообщение экранируется в одну строку."""
        body = json.dumps(
            {"lvl": level, "name": name, "msg": message},
            ensure_ascii=False,
        )
        return f"{cls.LOG_MARKER}{body}"


class RowStream:
    """Кодек строчного потока и приведение значений драйвера к JSON-виду."""

    _ANY: ClassVar[TypeAdapter[Any]] = TypeAdapter(Any)

    @classmethod
    def plain(cls, row: Mapping[str, Any]) -> dict[str, Any]:
        """Строка драйвера -> JSON-совместимые значения (pydantic'ом).

        Руками декодируются только сырые байты: не-utf8 bytea ронял бы
        pydantic-дамп; остальное (Decimal, UUID, date, set) приводит pydantic.
        """
        decoded = {name: cls._debytes(value) for name, value in row.items()}

        plain = cls._ANY.dump_python(decoded, mode="json")
        if not isinstance(plain, dict):
            msg = f"row must dump to an object, got {type(plain).__name__}"
            raise LauncherError(msg)

        return plain

    @classmethod
    def _debytes(cls, value: Any) -> Any:
        if isinstance(value, (bytes, bytearray, memoryview)):
            return bytes(value).decode("utf-8", errors="replace")

        if isinstance(value, (list, tuple)):
            return [cls._debytes(item) for item in value]

        if isinstance(value, dict):
            return {name: cls._debytes(item) for name, item in value.items()}

        return value

    @staticmethod
    def encode(row: Mapping[str, Any]) -> str:
        return json.dumps(row, ensure_ascii=False)


class ClippedText(BaseModel):
    """Начало потока в пределах байтового бюджета плюс пометка об усечении."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    text: str
    total_bytes: int
    truncated: bool

    ENCODING: ClassVar[str] = "utf-8"
    NOTICE: ClassVar[str] = "\n…[truncated: {kept} of {total} bytes shown]"

    @classmethod
    def of(cls, text: str, max_bytes: int) -> ClippedText:
        if max_bytes <= 0:
            msg = f"max_bytes must be positive, got {max_bytes}"
            raise ValueError(msg)

        raw = text.encode(cls.ENCODING)
        total = len(raw)
        if total <= max_bytes:
            return cls(text=text, total_bytes=total, truncated=False)

        # обрезка по байтам рвёт последний символ — его отбрасываем
        head = raw[:max_bytes].decode(cls.ENCODING, errors="ignore")
        kept = len(head.encode(cls.ENCODING))
        notice = cls.NOTICE.format(kept=kept, total=total)

        return cls(text=f"{head}{notice}", total_bytes=total, truncated=True)


@dataclass(frozen=True)
class RunResult:
    """Результат запуска: код возврата, потоки, длительность, таймаут.

    spawn_ms — сколько занял сам fork/exec; first_output_ms — латентность
    первого байта любого потока от старта, None — процесс не вывел ничего.
    """

    exit_code: int
    stdout: str
    stderr: str
    duration_ms: int
    timed_out: bool
    spawn_ms: int = 0
    first_output_ms: int | None = None


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


@dataclass(frozen=True)
class ToolOutcome:
    """Итог канального запуска: разобранный конверт плюс процессные поля.

    reply не опционален: конверта нет — это LauncherError из run_tool с
    хвостами tool_stderr/wrap_stderr, а не второе состояние итога.
    """

    reply: ReplyOk | ReplyError
    run: RunResult
    diagnostic: str


class ToolLauncher(Protocol):
    """Запуск инструмента в изолированном окружении.

    run_tool исполняет команду модуля инструментов и разбирает конверт из
    канала tool_result; call_text отдаёт процесс как есть (bash).
    """

    @abstractmethod
    def run_tool(self, command: ToolCommand) -> ToolOutcome:
        """Выполнить команду модуля инструментов; конверт обязателен."""
        ...

    @abstractmethod
    def call_text(self, command: str, stdin: str) -> LaunchOutcome:
        """Выполнить команду; stdout/stderr/rc возвращаются без разбора."""
        ...


class LauncherFactory(Protocol):
    """Выдаёт исполнителя по метке инструмента; окружение выбирает приложение."""

    @abstractmethod
    def __call__(self, tool: str, /) -> ToolLauncher:
        """Исполнитель для инструмента tool (метка идёт в логи и диагностику)."""
        ...
