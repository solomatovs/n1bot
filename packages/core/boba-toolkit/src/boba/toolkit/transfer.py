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
import string
from collections.abc import AsyncIterator, Iterator, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated, Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from boba.toolkit.ports import Chunk, Inbound, Outbound

__all__ = [
    "BackupAndCreate",
    "BackupAndCreateIfSchemaChanged",
    "ColumnRules",
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
    "TransferReport",
    "TransferRun",
    "TransferSink",
    "TransferTable",
    "UnknownTypeApply",
    "UnknownTypeStrategy",
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


class TransferOutbound:
    """Запись кадров потока из async-тела источника: schema — методом
    schema, блоки данных — методом rows или через файл writer. Сама запись
    в порт идёт в рабочем потоке, чтобы не блокировать цикл событий."""

    def __init__(self, out: Outbound[TransferFrame]) -> None:
        self._out = out

    async def schema(self, head: SchemaHead) -> None:
        await asyncio.to_thread(self._out.emit, head)

    async def rows(self, body: Chunk) -> None:
        await asyncio.to_thread(self._out.emit, RowsHead(kind="rows"), body)

    def writer(self) -> FrameWriter:
        return FrameWriter(self._out)


class TransferInbound:
    """Чтение кадров потока в async-теле приёмника: get_schema отдаёт
    первый кадр и отказывает, если это не schema; bodies отдаёт блоки
    данных из кадров rows по одному."""

    def __init__(self, feed: Inbound[TransferFrame]) -> None:
        self._frames = iter(feed)

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

            yield frame.body

    def raw(self) -> FrameBodies:
        return FrameBodies(self.sync_bodies())

    def sync_bodies(self) -> Iterator[Chunk]:
        for frame in self._frames:
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

    def __init__(self, out: Outbound[TransferFrame]) -> None:
        super().__init__()
        self._out = out

    def writable(self) -> bool:
        return True

    def write(self, data: Any) -> int:
        view = memoryview(data)
        self._out.emit(RowsHead(kind="rows"), view)

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
    """Переменные шаблона своего движка: обязательные стоят вне квадратных
    скобок, необязательные — только внутри них. Каждая обязана встретиться
    в шаблоне хотя бы раз."""

    required: tuple[TemplateVar, ...]
    optional: tuple[TemplateVar, ...] = ()


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
    """Шаблон create table от вызывающего: цельный стейтмент с переменными
    своего движка. Обязательные переменные стоят в тексте, необязательные —
    в квадратных скобках вместе с окружающим текстом: [ on cluster {cluster}]
    выпадает целиком, если кластер не передан. Других подстановок нет;
    литеральные фигурные и квадратные скобки удваиваются. Проверяется при
    создании, подставляет уже экранированные фрагменты приёмника."""

    text: str
    variables: TemplateVars

    def __post_init__(self) -> None:
        required = [member.value for member in self.variables.required]
        optional = [member.value for member in self.variables.optional]
        allowed = required + optional
        seen: set[str] = set()
        for part in TemplateParts().parse(self.text):
            names = self._names(part)
            if part.optional and not names:
                raise TransferError(
                    f"create_table template: optional part [{part.text}] has no "
                    f"variable; template: {self.text!r}"
                )

            for name in names:
                self._check(name, part, allowed, required, optional)
                seen.add(name)

        missing = [name for name in allowed if name not in seen]
        if missing:
            raise TransferError(
                f"create_table template lacks {missing}; required outside brackets: "
                f"{required}, optional inside [ ]: {optional}; template: {self.text!r}"
            )

    def render(self, values: Mapping[TemplateVar, str]) -> str:
        """Стейтмент с подставленными фрагментами; необязательный кусок с
        пустой переменной выпадает. Фрагменты уже экранированы драйвером
        приёмника."""
        rendered: list[str] = []
        for part in TemplateParts().parse(self.text):
            names = part.names()
            filled: dict[str, str] = {}
            for name in names:
                filled[name] = values[TemplateVar(name)]

            if part.optional and not all(filled.values()):
                continue

            rendered.append(part.text.format(**filled))

        return "".join(rendered)

    def _names(self, part: TemplatePart) -> list[str]:
        try:
            fields = list(string.Formatter().parse(part.text))
        except ValueError as exc:
            raise TransferError(
                f"create_table template is not parseable: {exc}; double literal "
                f"braces; template: {self.text!r}"
            ) from exc

        names: list[str] = []
        for _, name, spec, conversion in fields:
            if name is None:
                continue

            if spec or conversion:
                raise TransferError(
                    f"create_table template: variable {{{name}}} takes no format "
                    f"spec or conversion; template: {self.text!r}"
                )

            names.append(name)

        return names

    def _check(
        self,
        name: str,
        part: TemplatePart,
        allowed: Sequence[str],
        required: Sequence[str],
        optional: Sequence[str],
    ) -> None:
        if name not in allowed:
            raise TransferError(
                f"create_table template has an unknown variable {{{name}}}; "
                f"allowed: {list(allowed)}; template: {self.text!r}"
            )

        if name in required and part.optional:
            raise TransferError(
                f"create_table template: required variable {{{name}}} stands "
                f"inside [ ]; take it out of the brackets; template: {self.text!r}"
            )

        if name in optional and not part.optional:
            raise TransferError(
                f"create_table template: optional variable {{{name}}} must stand "
                f"inside [ ] with its text, for example [ on cluster {{{name}}}]; "
                f"template: {self.text!r}"
            )


@dataclass(frozen=True)
class DeleteOutcome:
    """Что сделала стратегия удаления: строк и стейтмент."""

    rows: int
    statement: str


@dataclass(frozen=True)
class SchemaCheck:
    """Итог сверки контракта с существующей таблицей от пары: ошибки не дают
    загрузить без пересоздания, предупреждения — нет; lines — по строке на
    колонку для отчёта."""

    errors: tuple[str, ...]
    warnings: tuple[str, ...]
    lines: tuple[str, ...]

    def changed(self) -> bool:
        return bool(self.errors)

    def reason(self) -> str:
        return "; ".join(self.errors)

    def render(self) -> str:
        return "\n".join(self.lines)


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
    строк, discard читает поток до конца, ничего не записывая."""

    async def load(self) -> int: ...

    async def discard(self) -> int: ...


class SchemaAction(StrEnum):
    """Что делать с таблицей по стратегии схемы."""

    CREATE = "create"
    BACKUP_THEN_CREATE = "backup_then_create"
    DROP_THEN_CREATE = "drop_then_create"
    KEEP = "keep"
    FAIL = "fail"


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
        return DeleteOutcome(rows=0, statement="")


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
    """Итог приёмника для чата: что сделано со схемой и почему, сверка по
    колонкам, что удалено, сколько вставлено."""

    table: str
    action: SchemaAction
    reason: str
    backup: str
    check: str
    deleted: DeleteOutcome
    inserted: int
    transactional: bool

    def render(self) -> str:
        lines = [
            f"{self.inserted} rows written into {self.table}",
            f"schema: {self.action.value} ({self.reason})",
        ]
        if self.backup:
            lines.append(f"backup: {self.backup}")

        if self.check:
            lines.append("columns:")
            lines.append(self.check)

        if self.deleted.statement:
            lines.append(
                f"deleted: {self.deleted.rows} rows by {self.deleted.statement}"
            )

        if not self.transactional:
            lines.append("note: the steps are not one transaction on this engine")

        return "\n".join(lines)


class TransferRun:
    """Общий ход приёмника после сверки: план стратегии схемы и его действия
    через порт таблицы, удаление, вставка, отчёт. Транзакцию и сверку делает
    пара до вызова."""

    def __init__(
        self,
        schema_strategy: SchemaStrategyPlan,
        delete_strategy: DeleteStrategyApply,
        insert_strategy: InsertStrategyApply,
        unknown_types: UnknownTypeApply,
    ) -> None:
        self._schema_strategy = schema_strategy
        self._delete_strategy = delete_strategy
        self._insert_strategy = insert_strategy
        self._unknown_types = unknown_types

    async def run(  # noqa: PLR0913
        self,
        table_name: str,
        exists: bool,
        check: SchemaCheck,
        table: TransferTable,
        sink: TransferSink,
        transactional: bool,
    ) -> TransferReport:
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

        check_text = ""
        if exists:
            check_text = check.render()

        return TransferReport(
            table=table_name,
            action=plan.action,
            reason=plan.reason,
            backup=backup,
            check=check_text,
            deleted=deleted,
            inserted=inserted,
            transactional=transactional,
        )
