"""Порт запуска инструмента: чем инструмент пользуется, не зная про песочницу.

Инструменты зависят только от этого модуля: порт ToolLauncher (запуск графа и
чтение головы журнала канала) и коллекторы байтовых каналов. Реализация порта
(bwrap, cgroup, WorkflowRunner) подставляется снаружи; фасад строит вырожденный
WorkflowSpec из одного узла и читает трейлер из WorkflowOutcome. Полный поток
канала живёт в журнале; в ответ модели едет только его голова.

Ошибки:
LauncherError — исполнитель нарушил контракт, результату доверять нельзя.
PayloadFailureError — стадия сообщила об ожидаемой ошибке конвертом, текст
    готов для пользователя.
WorkflowError (boba.toolkit.workflow) — спецификация графа невалидна либо
    правило графа нарушено.
StageFailureError — запуск сорвался, и к сообщению приложены итоги стадий с
    адресами журналов.
WorkflowStageError и WorkflowPayloadError — граф сорвался, и к сообщению
    виновника приложены процессные итоги стадий.
CollectorCapacityError/CollectorRowLimitError — потребитель остановил поток по
    своему лимиту.
"""

from __future__ import annotations

import codecs
from abc import abstractmethod
from collections.abc import Mapping
from typing import Any, ClassVar, Protocol, TypeVar

from pydantic import BaseModel, ConfigDict, JsonValue

from boba.toolkit.channels import (
    ByteText,
    Channel,
    ChannelError,
    ChannelSink,
    LineSplitter,
    StreamCodec,
    StreamKey,
)
from boba.toolkit.workflow import (
    EmptyTrailer,
    StageSpec,
    WorkflowError,
    WorkflowOutcome,
    WorkflowSpec,
)

__all__ = [
    "ChannelHead",
    "CollectorCapacityError",
    "CollectorRowLimitError",
    "EmptyTrailer",
    "ErrorKind",
    "LauncherError",
    "LauncherFactory",
    "PayloadFailureError",
    "RowCollector",
    "StageFailure",
    "StageFailureError",
    "StageRun",
    "TextCollector",
    "ToolLauncher",
    "WorkflowPayloadError",
    "WorkflowStageError",
]


M = TypeVar("M", bound=BaseModel)


class LauncherError(RuntimeError):
    """Исполнитель нарушил контракт: результату доверять нельзя."""


class PayloadFailureError(LauncherError):
    """Ожидаемая ошибка операции: стадия объявила её конвертом и назвала причину.

    Не нарушение контракта: каналы отработали штатно, операция сообщила отказ.
    Текст пригоден для показа пользователю и LLM — трейсбека в нём нет.
    """

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind


class StageFailureError(LauncherError):
    """Запуск сорвался, и к сообщению приложены итоги стадий с адресами журналов.

    Пустой итог означает, что стадии до запуска не дошли: читать нечего.
    """

    EMPTY: ClassVar[WorkflowOutcome] = WorkflowOutcome(stages=(), trailers={})

    def __init__(self, message: str, outcome: WorkflowOutcome = EMPTY) -> None:
        super().__init__(message)
        self.outcome = outcome


class WorkflowStageError(WorkflowError):
    """Граф сорвался: сообщение виновника плюс процессные итоги всех стадий.

    Квитанций в outcome нет — граф не доработал, трейлеры стадий не собраны;
    итоги стадий говорят, кто упал сам, а кого снял раннер каскадом.
    """

    def __init__(self, message: str, outcome: WorkflowOutcome) -> None:
        super().__init__(message)
        self.outcome = outcome


class WorkflowPayloadError(PayloadFailureError):
    """Ожидаемый отказ стадии графа: конверт виновника и итоги всех стадий."""

    def __init__(self, kind: str, message: str, outcome: WorkflowOutcome) -> None:
        super().__init__(kind, message)
        self.outcome = outcome


class ErrorKind:
    """Классификация ошибки для ErrorResult: имя kind'а или имя класса."""

    @staticmethod
    def of(error: Exception) -> str:
        if isinstance(error, PayloadFailureError):
            return error.kind
        return type(error).__name__


class StageFailure:
    """Итоги стадий, приложенные к ошибке запуска: их несут не все ошибки.

    По адресам журналов из итога фасад читает голову канала сорвавшегося
    вызова — команда могла успеть напечатать вывод до отказа.
    """

    @staticmethod
    def outcome_of(error: Exception) -> WorkflowOutcome:
        if isinstance(error, StageFailureError):
            return error.outcome

        if isinstance(error, WorkflowStageError):
            return error.outcome

        if isinstance(error, WorkflowPayloadError):
            return error.outcome

        return StageFailureError.EMPTY


class CollectorCapacityError(RuntimeError):
    """Поток превысил потолок объёма потребителя."""


class CollectorRowLimitError(RuntimeError):
    """Поток дал больше строк, чем разрешил потребитель."""


class TextCollector:
    """Сборка текстового потока байтов в строку с потолками по объёму и строкам.

    Реализует протокол ChannelSink: feed принимает байты канала, close
    сбрасывает хвост инкрементального декодера.
    """

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
        self._decoder = codecs.getincrementaldecoder(ByteText.ENCODING)(
            ByteText.ERRORS
        )
        self._parts: list[str] = []
        self._size = 0
        self._newlines = 0

    @property
    def size(self) -> int:
        return self._size

    @property
    def row_count(self) -> int:
        return max(0, self._newlines - self._header_lines)

    def feed(self, data: bytes) -> None:
        text = self._decoder.decode(data)

        if not text:
            return

        self._append(text)

    def close(self) -> None:
        text = self._decoder.decode(b"", final=True)

        if not text:
            return

        self._append(text)

    def text(self) -> str:
        return "".join(self._parts)

    def _append(self, chunk: str) -> None:
        end = self._size + len(chunk)
        if end > self._max_chars:
            msg = f"TextCollector: {end} chars exceeds max_chars {self._max_chars}"
            raise CollectorCapacityError(msg)

        self._parts.append(chunk)
        self._size = end
        self._newlines += chunk.count("\n")

        if self._limit_rows is not None and self.row_count > self._limit_rows:
            raise CollectorRowLimitError


class RowCollector:
    """Сборка NDJSON-потока байтов в список записей с потолками объёма и числа.

    Реализует протокол ChannelSink; пустые строки потока пропускаются.
    """

    def __init__(self, *, max_chars: int, limit_rows: int | None) -> None:
        if max_chars <= 0:
            msg = f"max_chars must be positive, got {max_chars}"
            raise ValueError(msg)
        if limit_rows is not None and limit_rows < 0:
            msg = f"limit_rows must be >= 0, got {limit_rows}"
            raise ValueError(msg)

        self._max_chars = max_chars
        self._limit_rows = limit_rows
        self._lines = LineSplitter()
        self._rows: list[dict[str, Any]] = []
        self._size = 0

    @property
    def row_count(self) -> int:
        return len(self._rows)

    def feed(self, data: bytes) -> None:
        for line in self._lines.feed(data):
            self._row(line)

    def close(self) -> None:
        for line in self._lines.flush():
            self._row(line)

    def rows(self) -> list[dict[str, Any]]:
        return self._rows

    def _row(self, line: str) -> None:
        if not line.strip():
            return

        end = self._size + len(line)
        if end > self._max_chars:
            msg = f"RowCollector: {end} chars exceeds max_chars {self._max_chars}"
            raise CollectorCapacityError(msg)

        if self._limit_rows is not None and len(self._rows) >= self._limit_rows:
            raise CollectorRowLimitError

        self._rows.append(self._decoded(line))
        self._size = end

    @staticmethod
    def _decoded(line: str) -> dict[str, Any]:
        """Разбор строки кодеком; нарушение формата — ошибка контракта запуска."""
        try:
            return StreamCodec.decode_row(line)
        except ChannelError as exc:
            raise LauncherError(str(exc)) from exc


class ChannelHead(BaseModel):
    """Голова канала из журнала: текст для модели и признак обрезки."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    text: str
    truncated: bool

    @classmethod
    def empty(cls) -> ChannelHead:
        """Журнала канала нет: читать нечего, обрезки тоже нет."""
        return cls(text="", truncated=False)


class ToolLauncher(Protocol):
    """Запуск инструмента в изолированном окружении.

    Путь исполнения один — граф стадий: одиночный вызов строит вырожденный
    WorkflowSpec из одного узла без рёбер и читает трейлер из WorkflowOutcome.
    """

    @abstractmethod
    def call(
        self,
        spec: WorkflowSpec,
        sinks: Mapping[str, ChannelSink] | None = None,
    ) -> WorkflowOutcome:
        """Исполнить граф; итог — квитанции стадий, процессные факты и журналы.

        sinks — приёмники tool_payload по id узла: коллекторы фасада читают
        продукт стадии по мере исполнения.
        """
        ...

    @abstractmethod
    def head(self, key: StreamKey, max_bytes: int) -> ChannelHead:
        """Голова журнала канала: max_bytes — потолок чтения, не записи.

        Адрес берётся из WorkflowOutcome.journals; файла нет — пустая голова.
        """
        ...


class StageRun:
    """Одиночный вызов узла реестра стадий: вырожденный граф из одного узла.

    id стадии в графе — имя узла: у одиночного вызова стадия одна, и её id
    едет в адреса журналов.
    """

    def __init__(self, launcher: ToolLauncher) -> None:
        self._launcher = launcher

    def call(
        self,
        node: str,
        args: Mapping[str, JsonValue],
        *,
        sink: ChannelSink | None = None,
        stdin: str | None = None,
    ) -> WorkflowOutcome:
        """Исполнить узел; sink — приёмник его продукта, stdin — литерал входа."""
        spec = WorkflowSpec(
            nodes=[StageSpec(id=node, tool=node, args=args, stdin=stdin)],
        )

        sinks: dict[str, ChannelSink] = {}
        if sink is not None:
            sinks[node] = sink

        return self._launcher.call(spec, sinks)

    def trailer(
        self,
        node: str,
        args: Mapping[str, JsonValue],
        schema: type[M],
        *,
        sink: ChannelSink | None = None,
        stdin: str | None = None,
    ) -> M:
        """Тот же вызов, но наружу отдаётся типизированная квитанция узла."""
        outcome = self.call(node, args, sink=sink, stdin=stdin)

        return outcome.trailer(node, schema)

    def head(
        self,
        outcome: WorkflowOutcome,
        node: str,
        channel: Channel,
        max_bytes: int,
    ) -> ChannelHead:
        """Голова журнала канала узла: столько его потока уезжает в модель."""
        key = outcome.journal_of(node, channel)

        if key is None:
            return ChannelHead.empty()

        return self._launcher.head(key, max_bytes)


class LauncherFactory(Protocol):
    """Выдаёт исполнителя по метке инструмента; окружение выбирает приложение."""

    @abstractmethod
    def __call__(self, tool: str, /) -> ToolLauncher:
        """Исполнитель для инструмента tool (метка идёт в логи и диагностику)."""
        ...
