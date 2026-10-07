"""Передача данных между инструментами потоком кадров и приём этого потока
в таблицу по стратегиям. Здесь общая часть, одинаковая для всех движков:
    - кадры потока: первым schema (движок источника, формат данных, контракт
      колонок), дальше rows с байтами данных;
    - порты, которыми источник пишет кадры, а приёмник читает;
    - стратегии приёмника: что делать с таблицей, что удалить, как вставить,
      что делать с колонкой неизвестного типа;
    - прогон стратегий и отчёт о нём.
Типы колонок, их сверку с таблицей и DDL знает пара «источник → приёмник» в
своём пакете; контракт колонок едет в кадре schema как JSON этого источника.

Ошибки:
TransferError — поток не начинается с кадра schema, стратегия схемы отказала,
    колонка неизвестного типа без запасного варианта.
"""

from __future__ import annotations

import asyncio
import io
import logging
import string
import threading
import time
from collections.abc import AsyncIterator, Generator, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated, Any, ClassVar, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from boba.toolkit.failure import FailurePacker, ReportedError
from boba.toolkit.ports import (
    Chunk,
    Inbound,
    Outbound,
    StageProgress,
    StreamGroup,
    ToolProgress,
)
from boba.toolkit.result import Fact, FailureResult, SqlFailureResult, SqlStatement
from boba.toolkit.timing import Elapsed

__all__ = [
    "BackupAndCreate",
    "BackupAndCreateIfSchemaChanged",
    "ColumnCheck",
    "ColumnIssue",
    "ColumnRules",
    "ColumnVerdict",
    "CommandJournal",
    "CommandKind",
    "CommandsFailedError",
    "CreateIfNotExists",
    "CreateTemplate",
    "DeleteAll",
    "DeleteNothing",
    "DeleteOutcome",
    "DeleteStrategy",
    "DeleteStrategyApply",
    "DeleteTruncate",
    "DeleteWhere",
    "DoNothing",
    "DropAndCreate",
    "DropAndCreateIfSchemaChanged",
    "Engine",
    "ErrorIfNotExists",
    "ErrorIfSchemaChanged",
    "FailOnUnknown",
    "FallbackAsVarchar",
    "FrameBodies",
    "FrameWriter",
    "InsertFull",
    "InsertNothing",
    "InsertStrategy",
    "InsertStrategyApply",
    "JournalCommand",
    "LoadColumn",
    "LoadColumns",
    "PlannedColumn",
    "RowsHead",
    "SchemaAction",
    "SchemaCheck",
    "SchemaHead",
    "SchemaPlan",
    "SchemaStrategy",
    "SchemaStrategyPlan",
    "StreamWire",
    "TemplateBrackets",
    "TemplatePart",
    "TemplateParts",
    "TemplateVar",
    "TemplateVars",
    "TransferError",
    "TransferFrame",
    "TransferInbound",
    "TransferOutbound",
    "TransferProgress",
    "TransferReport",
    "TransferRun",
    "TransferSink",
    "TransferTable",
    "UnknownTypeApply",
    "UnknownTypeStrategy",
    "Verdict",
]


class TransferError(Exception):
    """Поток кадров нарушен или стратегия приёмника отказала."""


class Engine(StrEnum):
    """Движок источника, записанный в кадре schema. По нему приёмник находит
    пару «источник → приёмник» в реестре."""

    POSTGRES = "postgres"
    CLICKHOUSE = "clickhouse"
    ORACLE = "oracle"


class StreamWire(StrEnum):
    """Формат данных в кадрах rows. Источник записывает его в кадр schema,
    приёмник по нему решает, как читать байты:
        - csv — текст, как его печатает COPY postgres в формате csv;
        - tsv — текст, как его печатает COPY postgres в формате text; те же
          байты ClickHouse читает как TabSeparated;
        - binary — двоичный формат COPY postgres. Использовать можно только
            с той же мажерной версией postgres;
        - arrow — поток Arrow IPC, колоночные пачки; единственный формат,
          который читает любой приёмник и узел преобразования потока.
    Тот же enum — параметр wire у источника: LLM называет формат, источник
    выдаёт данные в нём и о приёмнике ничего не знает; подходит ли формат
    приёмнику, проверяет сам приёмник по кадру schema."""

    CSV = "csv"
    TSV = "tsv"
    BINARY = "binary"
    ARROW = "arrow"


class SchemaHead(BaseModel):
    """Первый кадр потока. Несёт движок источника, формат данных в кадрах
    rows и контракт колонок. Для форматов csv, tsv и binary контракт записан
    так, как его описал источник, и разбирает его пара по source_engine; для
    arrow это нейтральный контракт, понятный любому приёмнику."""

    kind: Literal["schema"]
    source_engine: Engine
    wire: StreamWire
    contract: JsonValue


class RowsHead(BaseModel):
    """Кадр данных: тело — очередной блок строк в формате из первого кадра."""

    kind: Literal["rows"]


TransferFrame = SchemaHead | RowsHead
"""Кадры потока передачи: сначала один schema, дальше rows; порт различает
их по полю kind."""


class TransferProgress:
    """Ход работы насоса отчётами тела инструмента.

    Создаётся телом потокового инструмента из его ToolProgress и отдаётся
    журналу команд (CommandJournal) и концам потока (TransferOutbound,
    TransferInbound): журнал называет идущую команду сервера и её итог,
    концы потока считают прошедшие байты и кадры. Каждое изменение —
    следующий отчёт: текст несёт команду и объём; сколько всего данных,
    насос не знает. Счётчики зовут и из рабочих потоков (запись кадров,
    COPY), поэтому состояние под замком; текст отчёта собирается не чаще
    INTERVAL_SEC, а итог команды сообщается всегда.
    """

    COMMAND_CHARS: ClassVar[int] = 80
    INTERVAL_SEC: ClassVar[float] = 0.5
    MIB: ClassVar[int] = 1 << 20

    def __init__(self, progress: ToolProgress) -> None:
        self._steps = StageProgress(progress, None)
        self._lock = threading.Lock()
        self._command = ""
        self._bytes = 0
        self._frames = 0
        self._said_at = 0.0

    def command(self, text: str) -> None:
        """Началась команда сервера text."""
        line, _, _ = text.strip().partition("\n")
        with self._lock:
            self._command = line[: self.COMMAND_CHARS]
            self._say()

    def finished(self, status: str) -> None:
        """Идущая команда кончилась со статусом сервера status."""
        with self._lock:
            if status:
                self._command = f"{self._command}: {status}"

            self._say()

    def moved(self, size: int) -> None:
        """Через порт прошёл кадр данных в size байт."""
        with self._lock:
            self._bytes += size
            self._frames += 1
            if time.monotonic() - self._said_at < self.INTERVAL_SEC:
                return

            self._say()

    def _say(self) -> None:
        self._said_at = time.monotonic()
        text = self._command
        if self._frames:
            volume = f"{self._bytes / self.MIB:.1f} MiB in {self._frames} frames"
            text = f"{text} — {volume}"

        self._steps.next(text)


class TransferOutbound:
    """Запись кадров потока из async-тела источника: schema — методом
    schema, блоки данных — методом rows или через файл writer. Сама запись
    в порт идёт в рабочем потоке, чтобы не блокировать цикл событий.
    Прошедшие кадры считает ход работы насоса (TransferProgress)."""

    def __init__(
        self, out: Outbound[TransferFrame], progress: TransferProgress
    ) -> None:
        self._out = out
        self._progress = progress

    async def schema(self, head: SchemaHead) -> None:
        await asyncio.to_thread(self._out.emit, head)

    async def rows(self, body: Chunk) -> None:
        await asyncio.to_thread(self._out.emit, RowsHead(kind="rows"), body)
        self._progress.moved(len(body))

    def writer(self) -> FrameWriter:
        return FrameWriter(self._out, self._progress)


class TransferInbound:
    """Чтение кадров потока в async-теле приёмника: get_schema отдаёт
    первый кадр и отказывает, если это не schema; bodies отдаёт блоки
    данных из кадров rows по одному. committing — барьер группы связанных
    вызовов перед фиксацией результата (коммит, exchange tables): приёмник
    зовёт его, дочитав поток, и фиксирует, только если он вернулся."""

    def __init__(
        self,
        feed: Inbound[TransferFrame],
        group: StreamGroup,
        progress: TransferProgress,
    ) -> None:
        self._frames = iter(feed)
        self._group = group
        self._progress = progress

    async def committing(self) -> None:
        """Дождаться решения группы; срыв — StreamGroupAbortedError."""
        await self._group.ready()

    async def get_schema(self) -> SchemaHead:
        first = await asyncio.to_thread(next, self._frames, None)
        if first is None:
            raise TransferError(
                "transfer stream is empty: expected a schema frame first"
            )

        if isinstance(first.head, RowsHead):
            raise TransferError(
                "transfer stream starts with a rows frame, expected schema"
            )

        return first.head

    async def bodies(self) -> AsyncIterator[Chunk]:
        while True:
            frame = await asyncio.to_thread(next, self._frames, None)
            if frame is None:
                return

            self._progress.moved(len(frame.body))
            yield frame.body

    def raw(self) -> FrameBodies:
        return FrameBodies(self.sync_bodies())

    def sync_bodies(self) -> Iterator[Chunk]:
        for frame in self._frames:
            self._progress.moved(len(frame.body))
            yield frame.body


class FrameBodies(io.RawIOBase):
    """Тела кадров подряд как файл на чтение: для читателей, которые ждут
    файл. Чтение блокирующее, из потока, где стоит читатель."""

    def __init__(self, bodies: Iterator[Chunk]) -> None:
        super().__init__()
        self._bodies = bodies
        self._current = memoryview(b"")

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: Any) -> int:
        target = memoryview(buffer).cast("B")
        while len(self._current) == 0:
            try:
                self._current = memoryview(next(self._bodies))
            except StopIteration:
                return 0

        size = min(len(target), len(self._current))
        target[:size] = self._current[:size]
        self._current = self._current[size:]

        return size


class FrameWriter(io.RawIOBase):
    """Файл на запись поверх порта кадров: каждый write превращается в
    кадр rows с этими байтами. Для писателей, работающих в рабочем потоке,
    например BufferedWriter или Arrow IPC."""

    def __init__(
        self, out: Outbound[TransferFrame], progress: TransferProgress
    ) -> None:
        super().__init__()
        self._out = out
        self._progress = progress

    def writable(self) -> bool:
        return True

    def write(self, data: Any) -> int:
        view = memoryview(data)
        self._out.emit(RowsHead(kind="rows"), view)
        self._progress.moved(len(view))

        return len(view)


class ColumnRules(BaseModel):
    """Правила приёмника по колонкам: rename_columns — откуда берётся колонка,
    column_types — какой тип ей дать в DDL текстом приёмника."""

    model_config = ConfigDict(frozen=True)

    rename_columns: Mapping[str, str] = Field(
        default={},
        description=(
            "Колонка приёмника -> поле потока, которое в неё ложится: "
            '{"created_at": "created"}. Только имя, данные не меняются.'
        ),
    )
    column_types: Mapping[str, str] = Field(
        default={},
        description=(
            "Колонка приёмника -> тип для create table текстом приёмника как "
            'есть: {"v": "vector(3)", "amount": "numeric(20,6)"}. Перекрывает '
            "тип из контракта и стратегию unknown_types."
        ),
    )


class TemplateVar(StrEnum):
    """Переменные шаблона create table. Каждую подставляет приёмник уже
    экранированной своим драйвером:
        - schema_name — схема таблицы у postgres;
        - database — база таблицы у ClickHouse;
        - table_name — имя таблицы;
        - columns — колонки с типами из плана приёмника;
        - cluster — кластер ClickHouse, пусто без кластера;
        - order_by — ключ сортировки ClickHouse как в SQL."""

    SCHEMA_NAME = "schema_name"
    DATABASE = "database"
    TABLE_NAME = "table_name"
    COLUMNS = "columns"
    CLUSTER = "cluster"
    ORDER_BY = "order_by"


@dataclass(frozen=True)
class TemplateVars:
    """Переменные, которые приёмник своего движка умеет подставить в шаблон.
    Шаблон вызывающего главный: берёт из них те, что ему нужны, в любом
    месте; переменная, которой здесь нет, — ошибка."""

    offered: tuple[TemplateVar, ...]

    def names(self) -> list[str]:
        names: list[str] = []
        for variable in self.offered:
            names.append(variable.value)

        return names

    def listed(self) -> str:
        braced: list[str] = []
        for name in self.names():
            braced.append(f"{{{name}}}")

        return ", ".join(braced)


@dataclass(frozen=True)
class TemplatePart:
    """Кусок шаблона: текст для str.format и признак необязательности —
    необязательный кусок выпадает целиком, если пуста любая его переменная."""

    text: str
    optional: bool

    def names(self) -> list[str]:
        names: list[str] = []
        for _, name, _, _ in string.Formatter().parse(self.text):
            if name is not None:
                names.append(name)

        return names


class TemplateBrackets(StrEnum):
    """Скобки необязательной части шаблона и их литеральная запись."""

    OPEN = "["
    CLOSE = "]"
    LITERAL_OPEN = "[["
    LITERAL_CLOSE = "]]"


class TemplateParts:
    """Разбор шаблона на куски по квадратным скобкам: [ ... ] — необязательный
    кусок, [[ и ]] — литеральные скобки; вложенные и незакрытые скобки —
    ошибка."""

    def parse(self, text: str) -> tuple[TemplatePart, ...]:
        parts: list[TemplatePart] = []
        current: list[str] = []
        optional = False
        position = 0
        while position < len(text):
            pair = text[position : position + 2]
            char = text[position]
            if pair in (TemplateBrackets.LITERAL_OPEN, TemplateBrackets.LITERAL_CLOSE):
                current.append(char)
                position += 2
                continue

            if char == TemplateBrackets.OPEN:
                if optional:
                    raise TransferError(
                        f"create_table template: optional part opened inside another "
                        f"at position {position}; write [[ for a literal bracket; "
                        f"template: {text!r}"
                    )

                parts.append(TemplatePart("".join(current), optional=False))
                current = []
                optional = True
                position += 1
                continue

            if char == TemplateBrackets.CLOSE:
                if not optional:
                    raise TransferError(
                        f"create_table template: ] at position {position} closes "
                        f"nothing; write ]] for a literal bracket; template: {text!r}"
                    )

                parts.append(TemplatePart("".join(current), optional=True))
                current = []
                optional = False
                position += 1
                continue

            current.append(char)
            position += 1

        if optional:
            raise TransferError(
                f"create_table template: optional part is not closed with ]; "
                f"template: {text!r}"
            )

        parts.append(TemplatePart("".join(current), optional=False))

        return tuple(parts)


@dataclass(frozen=True)
class CreateTemplate:
    """Шаблон create table от вызывающего: цельный стейтмент, в который
    приёмник подставляет названные в нём переменные — уже экранированные
    фрагменты своего движка. Какие переменные взять, решает шаблон; кусок в
    квадратных скобках [ on cluster {cluster}] выпадает целиком, если
    значения его переменной нет. Литеральные фигурные и квадратные скобки
    удваиваются. При создании проверяется, что шаблон называет только то,
    что приёмник умеет подставить."""

    text: str
    variables: TemplateVars

    def __post_init__(self) -> None:
        offered = self.variables.names()
        for part in TemplateParts().parse(self.text):
            names = self._names(part)
            if part.optional and not names:
                raise TransferError(
                    f"create_table: the part [{part.text}] names no variable, so "
                    f"nothing decides when to drop it; put a variable inside, like "
                    f"[ on cluster {{cluster}}], or write [[ and ]] for literal "
                    f"brackets; template: {self.text!r}"
                )

            for name in names:
                if name in offered:
                    continue

                raise TransferError(
                    f"create_table uses {{{name}}}, but the receiver has no such "
                    f"value; it can fill {self.variables.listed()}; template: "
                    f"{self.text!r}"
                )

    def render(self, values: Mapping[TemplateVar, str]) -> str:
        """Стейтмент с подставленными фрагментами; кусок в скобках с пустой
        переменной выпадает, вне скобок пустая переменная — ошибка.

        Ошибки:
        TransferError — переменная вне [ ] осталась без значения.
        """
        rendered: list[str] = []
        for part in TemplateParts().parse(self.text):
            filled: dict[str, str] = {}
            for name in part.names():
                filled[name] = values[TemplateVar(name)]

            empty = self._empty(filled)
            if empty and part.optional:
                continue

            if empty:
                raise TransferError(
                    f"create_table uses {{{empty[0]}}} outside [ ], but no "
                    f"{empty[0]} is passed; pass it, or put the part into [ ] so it "
                    f"drops without a value; template: {self.text!r}"
                )

            rendered.append(part.text.format(**filled))

        return "".join(rendered)

    @staticmethod
    def _empty(filled: Mapping[str, str]) -> list[str]:
        empty: list[str] = []
        for name, value in filled.items():
            if not value:
                empty.append(name)

        return empty

    def _names(self, part: TemplatePart) -> list[str]:
        try:
            fields = list(string.Formatter().parse(part.text))
        except ValueError as exc:
            raise TransferError(
                f"create_table is not parseable: {exc}; double literal braces; "
                f"template: {self.text!r}"
            ) from exc

        names: list[str] = []
        for _, name, spec, conversion in fields:
            if name is None:
                continue

            if spec or conversion:
                raise TransferError(
                    f"create_table: variable {{{name}}} takes no format spec or "
                    f"conversion; template: {self.text!r}"
                )

            names.append(name)

        return names


@dataclass(frozen=True)
class DeleteOutcome:
    """Что стратегия удаления сделала с прежними строками таблицы, словами;
    выполненный SQL записывает журнал команд вызова."""

    effect: str


class Verdict(StrEnum):
    """Строгость расхождения колонки: ошибка отказывает строгим стратегиям
    схемы (error_if_schema_changed) и пересоздаёт таблицу у
    drop_and_create_if_schema_changed, предупреждение — нет."""

    OK = "ok"
    WARNING = "warning"
    ERROR = "error"


class ColumnIssue(StrEnum):
    """Вид расхождения колонки потока и таблицы; у каждого своё последствие
    для загрузки. TYPE — несовпадение типа, его смысл в тексте вердикта."""

    TYPE = "type"
    NOT_IN_STREAM = "not_in_stream"
    NOT_IN_TABLE = "not_in_table"
    ONLY_IN_RULES = "only_in_rules"
    NULLABLE_INTO_NOT_NULL = "nullable_into_not_null"
    NOT_NULL_INTO_NULLABLE = "not_null_into_nullable"

    def effect(self, message: str) -> str:
        """Что расхождение значит для загрузки; у TYPE — текст вердикта."""
        effects = {
            ColumnIssue.NOT_IN_STREAM: "not in the stream: rows get the column default",
            ColumnIssue.NOT_IN_TABLE: "the table has no such column",
            ColumnIssue.ONLY_IN_RULES: "named only in the call rules",
            ColumnIssue.NULLABLE_INTO_NOT_NULL: (
                "the stream may carry NULL, the table rejects it: a NULL would "
                "fail the load; declare nullable: false at the source"
            ),
            ColumnIssue.NOT_NULL_INTO_NULLABLE: "the table also accepts NULL",
        }

        return effects.get(self, message)


@dataclass(frozen=True)
class ColumnVerdict:
    """Итог сверки колонки: строгость, вид расхождения и объяснение."""

    level: Verdict
    message: str
    issue: ColumnIssue = ColumnIssue.TYPE


@dataclass(frozen=True)
class ColumnCheck:
    """Сверка одной колонки таблицы-приёмника: тип потока и тип таблицы
    текстом (пусто — этой стороны нет) и вердикт."""

    name: str
    stream: str
    table: str
    verdict: ColumnVerdict

    MISSING: ClassVar[str] = "—"

    def line(self) -> str:
        """Строка для сообщения об отказе: колонка, объяснение, типы сторон."""
        return (
            f"{self.name}: {self.verdict.message} "
            f"(stream {self._side(self.stream)}, table {self._side(self.table)})"
        )

    def _side(self, text: str) -> str:
        if not text:
            return self.MISSING

        return text


@dataclass(frozen=True)
class SchemaCheck:
    """Итог сверки контракта с существующей таблицей от пары: колонка за
    колонкой, со строгостью и видом расхождения."""

    columns: tuple[ColumnCheck, ...]

    def changed(self) -> bool:
        return any(self._at(Verdict.ERROR))

    def reason(self) -> str:
        lines: list[str] = []
        for column in self._at(Verdict.ERROR):
            lines.append(column.line())

        return "; ".join(lines)

    def render(self) -> str:
        """Расхождения для сообщения об отказе стратегии: по строке на колонку."""
        lines: list[str] = []
        for column in self.differences():
            lines.append(f"- {column.verdict.level.value} {column.line()}")

        return "\n".join(lines)

    def differences(self) -> list[ColumnCheck]:
        """Колонки с расхождением: сначала ошибки, затем предупреждения."""
        errors = list(self._at(Verdict.ERROR))
        warnings = list(self._at(Verdict.WARNING))

        return [*errors, *warnings]

    def _at(self, level: Verdict) -> Iterator[ColumnCheck]:
        for column in self.columns:
            if column.verdict.level is level:
                yield column


class LoadColumn(StrEnum):
    """Заголовки итоговой таблицы колонок загрузки."""

    COLUMN = "column"
    FIELD = "stream field"
    TYPE = "type"
    NULLABLE = "nullable"
    SOURCE_TYPE = "source type"
    NOTE = "note"


@dataclass(frozen=True)
class PlannedColumn:
    """Колонка, с которой пара собирается грузить: имя в таблице, поле
    потока, тип в таблице, nullable, тип у источника и пометки пары
    (переименование, ddl_type из правил, запасной varchar)."""

    name: str
    field: str
    type: str
    nullable: bool
    source_type: str
    notes: tuple[str, ...] = ()


class LoadColumns:
    """Итоговая таблица колонок загрузки: план пары и расхождения сверки в
    note, колонки, которые есть только в таблице, — отдельными строками. У
    существующей таблицы тип берётся из неё: грузится в то, что есть."""

    MISSING: ClassVar[str] = "—"
    NULLABLE: ClassVar[str] = "nullable"
    NOT_NULL: ClassVar[str] = "not null"
    NOTE_SEPARATOR: ClassVar[str] = "; "

    def rows(
        self, planned: Sequence[PlannedColumn], check: SchemaCheck
    ) -> list[dict[str, str]]:
        checks: dict[str, ColumnCheck] = {}
        for column in check.columns:
            checks[column.name] = column

        rows: list[dict[str, str]] = []
        for column in planned:
            rows.append(self._planned(column, checks.get(column.name)))

        for column in check.columns:
            if column.verdict.issue is ColumnIssue.NOT_IN_STREAM:
                rows.append(self._table_only(column))

        return rows

    def _planned(
        self, column: PlannedColumn, check: ColumnCheck | None
    ) -> dict[str, str]:
        notes = list(column.notes)
        type_text = column.type
        if check is not None:
            notes.extend(self._differs(check))
            if check.table:
                type_text = check.table

        return {
            LoadColumn.COLUMN: column.name,
            LoadColumn.FIELD: column.field,
            LoadColumn.TYPE: type_text,
            LoadColumn.NULLABLE: self._nullability(column.nullable),
            LoadColumn.SOURCE_TYPE: column.source_type,
            LoadColumn.NOTE: self.NOTE_SEPARATOR.join(notes),
        }

    def _table_only(self, check: ColumnCheck) -> dict[str, str]:
        return {
            LoadColumn.COLUMN: check.name,
            LoadColumn.FIELD: self.MISSING,
            LoadColumn.TYPE: check.table,
            LoadColumn.NULLABLE: "",
            LoadColumn.SOURCE_TYPE: "",
            LoadColumn.NOTE: check.verdict.issue.effect(check.verdict.message),
        }

    @staticmethod
    def _differs(check: ColumnCheck) -> Iterator[str]:
        if check.verdict.level is Verdict.OK:
            return

        yield check.verdict.issue.effect(check.verdict.message)

    def _nullability(self, nullable: bool) -> str:
        if nullable:
            return self.NULLABLE

        return self.NOT_NULL


class CommandKind(StrEnum):
    """Что команда делает: действие видно в чате и журнале, справка по
    каталогу — только в журнале."""

    ACTION = "action"
    LOOKUP = "lookup"


@dataclass
class JournalCommand:
    """Команда в работе: текст и статус, который выставляет исполнитель."""

    text: str
    kind: CommandKind
    status: str = ""


class CommandsFailedError(Exception, ReportedError):
    """Сбой вызова, который выполнял SQL: исходная ошибка плюс колонки и
    команды из журнала вызова; показывается видом SqlFailureResult."""

    def __init__(
        self,
        cause: Exception,
        columns: Sequence[Mapping[str, str]],
        statements: Sequence[SqlStatement],
    ) -> None:
        super().__init__(str(cause))
        self._cause = cause
        self._columns = list(columns)
        self._statements = list(statements)
        self._failures = FailurePacker()

    @property
    def cause(self) -> Exception:
        """Исходная ошибка насоса: её тип и текст решают, что случилось."""
        return self._cause

    def failure(self) -> FailureResult:
        cause = self._failures.pack(self._cause)

        return SqlFailureResult(
            error_kind=cause.error_kind,
            cause=cause,
            columns=self._columns,
            statements=self._statements,
        )


class CommandJournal:
    """Журнал команд одного вызова насоса: всё, что источник или приёмник
    выполнил на сервере, по порядку, и итоговая таблица колонок.

    Исполнители команд (таблица, вставка, источник, шаги before/after)
    получают его конструктором и оборачивают каждую команду в command():
    в журнал инструмента строка уходит до выполнения и после, со статусом и
    временем, а сама команда копится для чата. Сама загрузка — одна команда:
    строк и пачек журнал не видит. О начале и итоге каждой команды узнаёт
    ход работы насоса (TransferProgress): пользователь видит, что сейчас
    выполняется. По исключению тела failed() собирает CommandsFailedError —
    в чате видны колонки и команды вплоть до упавшей.
    """

    FAILED: ClassVar[str] = "failed"

    def __init__(self, tool: str, progress: TransferProgress) -> None:
        self._tool = tool
        self._progress = progress
        self._statements: list[SqlStatement] = []
        self._columns: list[dict[str, str]] = []
        self._logger = logging.getLogger(__name__)

    @contextmanager
    def command(
        self, text: str, kind: CommandKind
    ) -> Generator[JournalCommand, None, None]:
        running = JournalCommand(text=text, kind=kind)
        self._logger.info("%s: %s started:\n%s", self._tool, kind.value, text)
        self._progress.command(text)
        elapsed = Elapsed()
        try:
            yield running
        except BaseException as exc:
            level = logging.ERROR
            if kind is CommandKind.LOOKUP:
                # справка может отказать штатно: проба типа, есть ли таблица
                level = logging.INFO

            self._logger.log(
                level,
                "%s: %s failed in %dms: %s: %s",
                self._tool,
                kind.value,
                elapsed.ms(),
                type(exc).__name__,
                exc,
            )
            self._keep(running, f"{self.FAILED}: {type(exc).__name__}")
            raise

        self._logger.info(
            "%s: %s done in %dms: %s",
            self._tool,
            kind.value,
            elapsed.ms(),
            running.status,
        )
        self._progress.finished(running.status)
        self._keep(running, running.status)

    def columns(self, rows: Sequence[Mapping[str, str]]) -> None:
        """Итоговая таблица колонок: в журнал сразу, в чат — с отчётом."""
        self._columns = [dict(row) for row in rows]
        for row in self._columns:
            self._logger.info("%s: column %s", self._tool, row)

    def column_rows(self) -> list[dict[str, str]]:
        return list(self._columns)

    def statements(self) -> list[SqlStatement]:
        """Выполненные действия по порядку; справки по каталогу — только в журнале."""
        return list(self._statements)

    def failed(self, cause: Exception) -> CommandsFailedError:
        return CommandsFailedError(cause, self._columns, self._statements)

    def _keep(self, running: JournalCommand, status: str) -> None:
        if running.kind is not CommandKind.ACTION:
            return

        self._statements.append(SqlStatement(text=running.text, status=status))


class TransferTable(Protocol):
    """Порт таблицы-приёмника: реализация в паре строит стейтменты билдером
    своего движка. create знает колонки из плана пары; unknown_as_varchar —
    колонки без типа получают строковый тип движка, иначе отказ."""

    async def exists(self) -> bool: ...

    async def create(self, unknown_as_varchar: bool) -> str: ...

    async def backup(self) -> str: ...

    async def drop(self, cascade: bool) -> str: ...

    async def truncate(self) -> DeleteOutcome: ...

    async def delete_all(self) -> DeleteOutcome: ...

    async def delete_where(self, where: str) -> DeleteOutcome: ...


class TransferSink(Protocol):
    """Порт вставки: load кладёт все тела потока в таблицу и возвращает число
    строк, discard читает поток до конца, ничего не записывая; method —
    как движок кладёт строки, если это не очевидно (двойник и exchange у
    ClickHouse), пусто — обычная вставка в таблицу."""

    async def load(self) -> int: ...

    async def discard(self) -> int: ...

    def method(self) -> str: ...


class SchemaAction(StrEnum):
    """Что делать с таблицей по стратегии схемы."""

    CREATE = "create"
    BACKUP_THEN_CREATE = "backup_then_create"
    DROP_THEN_CREATE = "drop_then_create"
    KEEP = "keep"
    FAIL = "fail"

    def outcome(self, reason: str, backup: str) -> str:
        """Что стало с таблицей, словами для отчёта."""
        match self:
            case SchemaAction.CREATE:
                return f"created ({reason})"
            case SchemaAction.BACKUP_THEN_CREATE:
                return f"recreated ({reason}), the previous one saved as {backup}"
            case SchemaAction.DROP_THEN_CREATE:
                return f"dropped and recreated ({reason})"
            case SchemaAction.KEEP:
                return f"kept as is ({reason})"
            case SchemaAction.FAIL:
                return f"refused ({reason})"


@dataclass(frozen=True)
class SchemaPlan:
    action: SchemaAction
    reason: str
    cascade: bool = False


class SchemaStrategyPlan(Protocol):
    """Стратегия схемы: по наличию таблицы и итогу сверки решает, что с ней
    делать. Реализации — модели с kind (pydantic BaseModel не наследует
    Protocol из-за метакласса, совместимость структурная)."""

    def plan(self, exists: bool, check: SchemaCheck) -> SchemaPlan: ...


class DeleteStrategyApply(Protocol):
    """Стратегия удаления: применяет себя через порт таблицы. Реализации —
    модели с kind."""

    async def apply(self, table: TransferTable) -> DeleteOutcome: ...


class InsertStrategyApply(Protocol):
    """Стратегия вставки: применяет себя через порт вставки. Реализации —
    модели с kind."""

    async def apply(self, sink: TransferSink) -> int: ...


class UnknownTypeApply(Protocol):
    """Стратегия для колонок, тип которых пара сопоставить не может; явный
    rules.column_types перекрывает её. Реализации — модели с kind."""

    async def apply(self, table: TransferTable) -> str: ...


class CreateIfNotExists(BaseModel):
    """SchemaStrategyPlan: нет таблицы — создать по контракту; есть — оставить
    как есть, даже если схема разошлась (расхождение всплывёт при загрузке)."""

    kind: Literal["create_if_not_exists"]

    def plan(self, exists: bool, check: SchemaCheck) -> SchemaPlan:
        if not exists:
            return SchemaPlan(SchemaAction.CREATE, "table is missing")

        return SchemaPlan(SchemaAction.KEEP, "table exists")


class ErrorIfNotExists(BaseModel):
    """SchemaStrategyPlan: таблица обязана быть; схема не сверяется."""

    kind: Literal["error_if_not_exists"]

    def plan(self, exists: bool, check: SchemaCheck) -> SchemaPlan:
        if not exists:
            return SchemaPlan(SchemaAction.FAIL, "table is missing")

        return SchemaPlan(SchemaAction.KEEP, "table exists")


class ErrorIfSchemaChanged(BaseModel):
    """SchemaStrategyPlan: таблица обязана быть и сходиться с контрактом без
    ошибок сверки."""

    kind: Literal["error_if_schema_changed"]

    def plan(self, exists: bool, check: SchemaCheck) -> SchemaPlan:
        if not exists:
            return SchemaPlan(SchemaAction.FAIL, "table is missing")

        if check.changed():
            return SchemaPlan(SchemaAction.FAIL, check.reason())

        return SchemaPlan(SchemaAction.KEEP, "schema matches")


class DropAndCreateIfSchemaChanged(BaseModel):
    """SchemaStrategyPlan: нет таблицы — создать; схема разошлась — удалить
    и создать заново."""

    kind: Literal["drop_and_create_if_schema_changed"]
    cascade: bool = Field(
        default=False, description="drop ... cascade — снести и зависимые view"
    )

    def plan(self, exists: bool, check: SchemaCheck) -> SchemaPlan:
        if not exists:
            return SchemaPlan(SchemaAction.CREATE, "table is missing")

        if check.changed():
            return SchemaPlan(
                SchemaAction.DROP_THEN_CREATE, check.reason(), self.cascade
            )

        return SchemaPlan(SchemaAction.KEEP, "schema matches")


class BackupAndCreateIfSchemaChanged(BaseModel):
    """SchemaStrategyPlan: нет таблицы — создать; схема разошлась —
    переименовать старую с суффиксом даты и создать новую."""

    kind: Literal["backup_and_create_if_schema_changed"]

    def plan(self, exists: bool, check: SchemaCheck) -> SchemaPlan:
        if not exists:
            return SchemaPlan(SchemaAction.CREATE, "table is missing")

        if check.changed():
            return SchemaPlan(SchemaAction.BACKUP_THEN_CREATE, check.reason())

        return SchemaPlan(SchemaAction.KEEP, "schema matches")


class DropAndCreate(BaseModel):
    """SchemaStrategyPlan: всегда пересоздать таблицу по контракту."""

    kind: Literal["drop_and_create"]
    cascade: bool = Field(
        default=False, description="drop ... cascade — снести и зависимые view"
    )

    def plan(self, exists: bool, check: SchemaCheck) -> SchemaPlan:
        if not exists:
            return SchemaPlan(SchemaAction.CREATE, "table is missing")

        return SchemaPlan(SchemaAction.DROP_THEN_CREATE, "table exists", self.cascade)


class BackupAndCreate(BaseModel):
    """SchemaStrategyPlan: всегда старую таблицу — в бэкап с суффиксом даты,
    новую — по контракту."""

    kind: Literal["backup_and_create"]

    def plan(self, exists: bool, check: SchemaCheck) -> SchemaPlan:
        if not exists:
            return SchemaPlan(SchemaAction.CREATE, "table is missing")

        return SchemaPlan(SchemaAction.BACKUP_THEN_CREATE, "table exists")


class DoNothing(BaseModel):
    """SchemaStrategyPlan: таблицу не трогать и не сверять."""

    kind: Literal["do_nothing"]

    def plan(self, exists: bool, check: SchemaCheck) -> SchemaPlan:
        return SchemaPlan(SchemaAction.KEEP, "schema strategy is do_nothing")


SchemaStrategy = Annotated[
    CreateIfNotExists
    | ErrorIfNotExists
    | ErrorIfSchemaChanged
    | DropAndCreateIfSchemaChanged
    | BackupAndCreateIfSchemaChanged
    | DropAndCreate
    | BackupAndCreate
    | DoNothing,
    Field(discriminator="kind"),
]
"""Стратегия схемы: что делать с таблицей-приёмником перед загрузкой."""


class DeleteNothing(BaseModel):
    """DeleteStrategyApply: данные приёмника не трогать."""

    kind: Literal["nothing"]

    async def apply(self, table: TransferTable) -> DeleteOutcome:
        return DeleteOutcome(effect="kept, the stream is appended")


class DeleteTruncate(BaseModel):
    """DeleteStrategyApply: truncate table — быстро, без счётчика строк, вне
    транзакции у движков, где truncate не откатывается."""

    kind: Literal["truncate"]

    async def apply(self, table: TransferTable) -> DeleteOutcome:
        return await table.truncate()


class DeleteAll(BaseModel):
    """DeleteStrategyApply: delete from table — все строки, со счётчиком и в
    транзакции."""

    kind: Literal["delete_all"]

    async def apply(self, table: TransferTable) -> DeleteOutcome:
        return await table.delete_all()


class DeleteWhere(BaseModel):
    """DeleteStrategyApply: delete from table where <условие> — условие как в
    SQL приёмника."""

    kind: Literal["delete_where"]
    where: str = Field(
        min_length=1,
        description="Условие после where, как в SQL приёмника: dt >= date '2024-01-01'",
    )

    async def apply(self, table: TransferTable) -> DeleteOutcome:
        return await table.delete_where(self.where)


DeleteStrategy = Annotated[
    DeleteNothing | DeleteTruncate | DeleteAll | DeleteWhere,
    Field(discriminator="kind"),
]
"""Стратегия удаления: что убрать из приёмника перед вставкой."""


class InsertNothing(BaseModel):
    """InsertStrategyApply: поток прочитать до конца и ничего не вставлять."""

    kind: Literal["nothing"]

    async def apply(self, sink: TransferSink) -> int:
        return await sink.discard()


class InsertFull(BaseModel):
    """InsertStrategyApply: все тела потока — в таблицу как есть."""

    kind: Literal["full"]

    async def apply(self, sink: TransferSink) -> int:
        return await sink.load()


InsertStrategy = Annotated[
    InsertNothing | InsertFull,
    Field(discriminator="kind"),
]
"""Стратегия вставки: как положить поток в приёмник."""


class FailOnUnknown(BaseModel):
    """UnknownTypeApply: колонка без типа у приёмника — ошибка с тем, что о
    ней известно, и подсказкой объявить тип в rules.column_types или взять
    fallback_as_varchar; LLM решает сам."""

    kind: Literal["fail_on_unknown"]

    async def apply(self, table: TransferTable) -> str:
        return await table.create(False)


class FallbackAsVarchar(BaseModel):
    """UnknownTypeApply: колонка без типа у приёмника — строковый тип движка
    без предела длины (varchar у postgres, String у ClickHouse)."""

    kind: Literal["fallback_as_varchar"]

    async def apply(self, table: TransferTable) -> str:
        return await table.create(True)


UnknownTypeStrategy = Annotated[
    FailOnUnknown | FallbackAsVarchar,
    Field(discriminator="kind"),
]
"""Стратегия неизвестных типов: что делать с колонками без типа у приёмника."""


@dataclass(frozen=True)
class TransferReport:
    """Итог приёмника для чата: сколько строк загружено и куда, что стало с
    таблицей и с её прежними строками, как движок клал строки и итоговая
    таблица колонок с пометками. Выполненные команды показывает журнал
    вызова рядом с этим итогом."""

    table: str
    action: SchemaAction
    reason: str
    backup: str
    differences: tuple[ColumnCheck, ...]
    columns: tuple[Mapping[str, str], ...]
    deleted: DeleteOutcome
    inserted: int
    method: str
    transactional: bool

    NOT_TRANSACTIONAL: ClassVar[str] = (
        "the steps are not one transaction on this engine"
    )
    NOT_ACTED_ON: ClassVar[str] = (
        "the table was kept, so the differences in the notes were only reported"
    )

    def summary(self) -> SqlStatement:
        """Итог загрузки: подпись, факты и таблица колонок."""
        rows: list[Mapping[str, str]] | None = None
        if self.columns:
            rows = list(self.columns)

        return SqlStatement(
            status=f"{self.inserted} rows loaded into {self.table}",
            rows=rows,
            facts=list(self._facts()),
            note=self._note(),
        )

    def _facts(self) -> Iterator[Fact]:
        yield Fact(key="table", value=self.action.outcome(self.reason, self.backup))
        yield Fact(key="previous rows", value=self.deleted.effect)

        if self.method:
            yield Fact(key="load", value=self.method)

    def _note(self) -> str:
        notes: list[str] = []
        if self.differences and self.action is SchemaAction.KEEP:
            notes.append(self.NOT_ACTED_ON)

        if not self.transactional:
            notes.append(self.NOT_TRANSACTIONAL)

        return "; ".join(notes)


class TransferRun:
    """Общий ход приёмника после сверки: итоговая таблица колонок в журнал,
    план стратегии схемы и его действия через порт таблицы, удаление,
    вставка, отчёт. Транзакцию и сверку делает пара до вызова; команды
    записывают в журнал сами порты таблицы и вставки."""

    def __init__(
        self,
        schema_strategy: SchemaStrategyPlan,
        delete_strategy: DeleteStrategyApply,
        insert_strategy: InsertStrategyApply,
        unknown_types: UnknownTypeApply,
        journal: CommandJournal,
    ) -> None:
        self._schema_strategy = schema_strategy
        self._delete_strategy = delete_strategy
        self._insert_strategy = insert_strategy
        self._unknown_types = unknown_types
        self._journal = journal
        self._columns = LoadColumns()

    async def run(  # noqa: PLR0913
        self,
        table_name: str,
        exists: bool,
        check: SchemaCheck,
        planned: Sequence[PlannedColumn],
        table: TransferTable,
        sink: TransferSink,
        transactional: bool,
    ) -> TransferReport:
        self._journal.columns(self._columns.rows(planned, check))

        plan = self._schema_strategy.plan(exists, check)
        backup = ""
        match plan.action:
            case SchemaAction.CREATE:
                await self._unknown_types.apply(table)
            case SchemaAction.BACKUP_THEN_CREATE:
                backup = await table.backup()
                await self._unknown_types.apply(table)
            case SchemaAction.DROP_THEN_CREATE:
                await table.drop(plan.cascade)
                await self._unknown_types.apply(table)
            case SchemaAction.KEEP:
                pass
            case SchemaAction.FAIL:
                raise TransferError(
                    f"schema strategy refused {table_name}: {plan.reason}\n"
                    f"{check.render()}"
                )

        deleted = await self._delete_strategy.apply(table)
        inserted = await self._insert_strategy.apply(sink)

        differences: list[ColumnCheck] = []
        if exists:
            differences = check.differences()

        return TransferReport(
            table=table_name,
            action=plan.action,
            reason=plan.reason,
            backup=backup,
            differences=tuple(differences),
            columns=tuple(self._journal.column_rows()),
            deleted=deleted,
            inserted=inserted,
            method=sink.method(),
            transactional=transactional,
        )
