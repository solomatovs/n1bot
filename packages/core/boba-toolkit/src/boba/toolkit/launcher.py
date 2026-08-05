"""Порт запуска инструмента: чем инструмент пользуется, не зная про песочницу.

Инструменты зависят только от этого модуля: протокол ToolLauncher и данные одного
запуска. Реализация (bwrap, cgroup, subprocess) подставляется снаружи.

Ответ payload'а всегда потоковый: ноль и больше кадров данных плюс один трейлер.
Структурный ответ без данных — вырожденный поток (кадров нет, всё в трейлере).

Ошибки: LauncherError — исполнитель нарушил контракт, результату доверять нельзя;
PayloadFailureError — payload сообщил об ожидаемой ошибке кадром, текст готов для
пользователя; CollectorCapacityError/CollectorRowLimitError — потребитель
остановил поток по своему лимиту.
"""

from __future__ import annotations

import json
from abc import abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, ClassVar, Protocol, TypeVar

from pydantic import BaseModel, ConfigDict

__all__ = [
    "ChunkSink",
    "CollectorCapacityError",
    "CollectorRowLimitError",
    "EmptyTrailer",
    "ErrorKind",
    "LaunchOutcome",
    "LaunchPayload",
    "LauncherError",
    "LauncherFactory",
    "NoChunks",
    "PayloadFailureError",
    "RowCollector",
    "RowStream",
    "RunResult",
    "TextCollector",
    "ToolLauncher",
]

M = TypeVar("M", bound=BaseModel)


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


class EmptyTrailer(BaseModel):
    """Трейлер без полей: весь результат операции ушёл кадрами."""

    model_config = ConfigDict(extra="forbid")


class LaunchPayload:
    """Контракт ответа: кадры `sandbox-chunk:`, трейлер `sandbox-result:`
    либо кадр ожидаемой ошибки `sandbox-error:` вместо трейлера."""

    MARKER: ClassVar[str] = "sandbox-result:"
    CHUNK_MARKER: ClassVar[str] = "sandbox-chunk:"
    ERROR_MARKER: ClassVar[str] = "sandbox-error:"
    LOG_MARKER: ClassVar[str] = "sandbox-log:"
    """Кадр лога; едет в stderr — там он никому не мешает и ничего не решает."""

    @classmethod
    def encode(cls, data: BaseModel) -> str:
        """Строка трейлера; печатается payload-скриптом последней."""
        return f"{cls.MARKER}{data.model_dump_json()}"

    @classmethod
    def encode_chunk(cls, chunk: str) -> str:
        """Строка кадра данных; JSON-строка держит кадр в одной строке stdout."""
        body = json.dumps(chunk, ensure_ascii=False)
        return f"{cls.CHUNK_MARKER}{body}"

    @classmethod
    def encode_error(cls, kind: str, message: str) -> str:
        """Строка ожидаемой ошибки: причина известна, трейсбек не нужен."""
        body = json.dumps({"kind": kind, "message": message}, ensure_ascii=False)
        return f"{cls.ERROR_MARKER}{body}"

    @classmethod
    def encode_log(cls, level: str, name: str, message: str) -> str:
        """Строка лога: многострочное сообщение экранируется в одну строку."""
        body = json.dumps(
            {"lvl": level, "name": name, "msg": message},
            ensure_ascii=False,
        )
        return f"{cls.LOG_MARKER}{body}"


class ChunkSink(Protocol):
    """Потребитель кадров данных payload'а."""

    @abstractmethod
    def write(self, chunk: str) -> None:
        """Принять кадр; исключение прерывает запуск payload'а."""
        ...


class NoChunks(ChunkSink):
    """Sink trailer-only вызова: кадр данных — нарушение контракта."""

    def write(self, chunk: str) -> None:
        msg = f"unexpected data chunk from trailer-only payload: {chunk[:80]!r}"
        raise LauncherError(msg)


class CollectorCapacityError(RuntimeError):
    """Поток превысил потолок объёма потребителя."""


class CollectorRowLimitError(RuntimeError):
    """Поток дал больше строк, чем разрешил потребитель."""


class TextCollector(ChunkSink):
    """Сборка текстового потока в строку с потолками по объёму и числу строк."""

    def __init__(
        self,
        *,
        max_chars: int,
        limit_rows: int | None,
        header_lines: int,
    ) -> None:
        if max_chars <= 0:
            msg = f"max_chars must be positive, got {max_chars}"
            raise ValueError(msg)
        if limit_rows is not None and limit_rows < 0:
            msg = f"limit_rows must be >= 0, got {limit_rows}"
            raise ValueError(msg)
        if header_lines < 0:
            msg = f"header_lines must be >= 0, got {header_lines}"
            raise ValueError(msg)
        self._max_chars = max_chars
        self._limit_rows = limit_rows
        self._header_lines = header_lines
        self._parts: list[str] = []
        self._size = 0
        self._newlines = 0

    @property
    def size(self) -> int:
        return self._size

    @property
    def row_count(self) -> int:
        return max(0, self._newlines - self._header_lines)

    def write(self, chunk: str) -> None:
        end = self._size + len(chunk)
        if end > self._max_chars:
            msg = f"TextCollector: {end} chars exceeds max_chars {self._max_chars}"
            raise CollectorCapacityError(msg)
        self._parts.append(chunk)
        self._size = end
        self._newlines += chunk.count("\n")

        if self._limit_rows is not None and self.row_count > self._limit_rows:
            raise CollectorRowLimitError

    def text(self) -> str:
        return "".join(self._parts)


class RowStream:
    """Кодек строчного потока: одна запись-словарь — один NDJSON-кадр."""

    @staticmethod
    def encode(row: Mapping[str, Any]) -> str:
        return json.dumps(row, ensure_ascii=False)

    @staticmethod
    def decode(chunk: str) -> dict[str, Any]:
        try:
            row = json.loads(chunk)
        except json.JSONDecodeError as e:
            msg = f"row chunk is not valid JSON: {e}"
            raise LauncherError(msg) from e
        if not isinstance(row, dict):
            msg = f"row chunk must be a JSON object, got {type(row).__name__}"
            raise LauncherError(msg)
        return row


class RowCollector(ChunkSink):
    """Сборка строчного потока в список записей с потолками объёма и числа."""

    def __init__(self, *, max_chars: int, limit_rows: int | None) -> None:
        if max_chars <= 0:
            msg = f"max_chars must be positive, got {max_chars}"
            raise ValueError(msg)
        if limit_rows is not None and limit_rows < 0:
            msg = f"limit_rows must be >= 0, got {limit_rows}"
            raise ValueError(msg)
        self._max_chars = max_chars
        self._limit_rows = limit_rows
        self._rows: list[dict[str, Any]] = []
        self._size = 0

    @property
    def row_count(self) -> int:
        return len(self._rows)

    def write(self, chunk: str) -> None:
        end = self._size + len(chunk)
        if end > self._max_chars:
            msg = f"RowCollector: {end} chars exceeds max_chars {self._max_chars}"
            raise CollectorCapacityError(msg)
        if self._limit_rows is not None and len(self._rows) >= self._limit_rows:
            raise CollectorRowLimitError
        self._rows.append(RowStream.decode(chunk))
        self._size = end

    def rows(self) -> list[dict[str, Any]]:
        return self._rows


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

    call_text отдаёт процесс как есть, call_stream — поток кадров в sink
    и разобранный трейлер.
    """

    @abstractmethod
    def call_text(self, command: str, stdin: str) -> LaunchOutcome:
        """Выполнить команду; stdout/stderr/rc возвращаются без разбора."""
        ...

    @abstractmethod
    def call_stream(
        self,
        entry: Sequence[str],
        request: BaseModel,
        sink: ChunkSink,
        trailer: type[M],
    ) -> M:
        """Выполнить entry: запрос в stdin, кадры в sink, трейлер в схему."""
        ...


class LauncherFactory(Protocol):
    """Выдаёт исполнителя по метке инструмента; окружение выбирает приложение."""

    @abstractmethod
    def __call__(self, tool: str, /) -> ToolLauncher:
        """Исполнитель для инструмента tool (метка идёт в логи и диагностику)."""
        ...
