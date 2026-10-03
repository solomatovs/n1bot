"""Приёмник потока arrow в Oracle для нейтрального контракта любого движка.

Что делает:
    - колонки таблицы и типы из rules.column_types описывает сам сервер
      (parse без выполнения: `select * from <таблица>`, `select cast(null as
      <тип>) from dual`), в нейтральный вид их переводит OraColumnKinds по
      метаданным драйвера — тем же, из которых источник строит контракт;
    - поток проецируется на то, что Oracle хранит: текстовые uuid и time —
      строки, json/inet/money/xml/bit/interval — строки, date — момент,
      двоичное семейство, которое источник прислал текстом, — строка,
      строки чужих движков — nullable;
    - сверка по семействам, DDL по шаблону вызывающего с {schema_name},
      {table_name}, {columns}; типы без пары у Oracle — CLOB по стратегии
      неизвестных типов или отказ;
    - вставка пачек через executemany: драйвер берёт bind'ы прямо из
      массивов Arrow, приёмник значения не переписывает. Поле, которое
      драйвер как есть не положит (uuid-расширение, time, duration,
      bool до 23), отвергается до DDL с подсказкой, что написать в запросе
      источника.

Все стейтменты собирает OraQueryBuilder. DDL Oracle фиксирует сам, поэтому
создание, бэкап и drop идут вне транзакции; удаление и вставка — одна
транзакция соединения, commit делает вызывающий.

Ошибки:
ContractError — декларация на колонку, которой нет в ответе; правило вызова не
    сходится со схемами.
TransferError — стратегия схемы отказала; тип из rules.column_types сервер
    не знает; колонка без типа при fail_on_unknown.
OracleQueryError — сервер отклонил стейтмент или значение.
ArrowStreamError — входной поток не читается как Arrow IPC.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import ClassVar

import oracledb
import pyarrow
from oracledb import AsyncConnection

from boba.db.oracle.errors import OracleMissingObjectError, OracleQueryError
from boba.db.oracle.payload import OraColumnKinds, PayloadOracle
from boba.db.oracle.query import (
    OraBindMarks,
    OraIdentifier,
    OraIdentifiers,
    OraQuery,
    OraQueryBuilder,
)
from boba.db.oracle.target import OraTableRef
from boba.db.oracle.trace import OraSessionTrace
from boba.toolkit.arrow import ArrowIpc, ArrowReader
from boba.toolkit.contract import (
    ArrowContract,
    ColumnDeclaration,
    ColumnSpec,
    ColumnType,
    ContractTable,
    Declarations,
    DeclaredType,
    Engine,
    SchemaMatcher,
    StreamContract,
    TableColumn,
    TableSpec,
    TimeUnit,
    TypeFamily,
)
from boba.toolkit.transfer import (
    ColumnRules,
    CommandJournal,
    CommandKind,
    CreateTemplate,
    DeleteOutcome,
    DeleteStrategyApply,
    InsertStrategyApply,
    PlannedColumn,
    SchemaHead,
    SchemaStrategyPlan,
    StreamWire,
    TemplateVar,
    TransferError,
    TransferInbound,
    TransferOutbound,
    TransferReport,
    TransferRun,
    TransferSink,
    TransferTable,
    UnknownTypeApply,
)
from boba.toolkit.transfer import Engine as WireEngine

__all__ = [
    "OraArrowLoader",
    "OraArrowSink",
    "OraArrowSource",
    "OraArrowTable",
    "OraDdlTypes",
    "OraDeclaredTypes",
    "OraNeutralFacts",
    "OraStreamProjection",
]


class OraArrowSource:
    """Источник потока arrow Oracle: контракт по описанию стейтмента (parse
    без выполнения) с декларациями вызова поверх, кадр schema, затем пачки
    драйвера потоком Arrow IPC как есть (PayloadOracle.arrow_into). Пара к
    OraArrowLoader, как PgArrowSource и ChArrowSource у своих движков."""

    ENGINE: ClassVar[WireEngine] = WireEngine.ORACLE

    def __init__(
        self,
        conn: AsyncConnection,
        payload: PayloadOracle,
        trace: OraSessionTrace,
        journal: CommandJournal,
    ) -> None:
        self._conn = conn
        self._payload = payload
        self._trace = trace
        self._journal = journal
        self._declarations = Declarations()
        self._contract = StreamContract()
        self._contract_table = ContractTable()

    async def stream(
        self,
        text: str,
        declared: Sequence[ColumnDeclaration],
        out: TransferOutbound,
    ) -> tuple[ColumnSpec, ...]:
        """Контракт и тела потока в выходной порт; возвращает контракт для
        отчёта."""
        with self._journal.command(text, CommandKind.LOOKUP) as running:
            described = await self._payload.describe_specs(self._conn, text)
            running.status = f"{len(described)} columns described"

        specs = self._declarations.merge(described, declared)
        self._journal.columns(self._contract_table.rows(specs))
        contract = ArrowContract(columns=self._contract.columns(specs))
        await out.schema(
            SchemaHead(
                kind="schema",
                source_engine=self.ENGINE,
                wire=StreamWire.ARROW,
                contract=contract.model_dump(mode="json"),
            )
        )
        with self._journal.command(text, CommandKind.ACTION) as running:
            await self._payload.arrow_into(self._conn, text, out.writer(), self._trace)
            running.status = "streamed out"

        return specs


class OraBindHint(StrEnum):
    """Каким типом Arrow прислать поле, которое драйвер python-oracledb
    bind'ом в Oracle не кладёт. Подсказка говорит только о типах Arrow:
    как получить такой тип в запросе своего движка, знает источник."""

    UUID = "the driver binds no uuid extension: send utf8 text or binary"
    TIME = "the driver binds no time: send utf8 text"
    DURATION = "the driver binds no duration: send an integer or decimal of seconds"
    INTERVAL = "the driver binds no interval: send a number of seconds or utf8 text"
    FIXED = "the driver binds no fixed_size_binary: send binary or utf8 text"
    DICTIONARY = "the driver binds no dictionary: send the plain value type"
    BOOLEAN = "the server has no BOOLEAN before 23: send an integer 0 or 1"


class OraArrowBinds:
    """Поля потока, которые драйвер положит bind'ом как есть: числа,
    decimal, float, строки, двоичные, date32, timestamp любой единицы и
    пояса, bool на 23. Остальное приёмник не переписывает, а отвергает до
    DDL — привести значение должен запрос источника (OraBindHint)."""

    def __init__(self, has_boolean: bool) -> None:
        self._has_boolean = has_boolean

    def refusals(self, schema: pyarrow.Schema) -> list[str]:
        refused: list[str] = []
        for field in schema:
            hint = self._hint(field.type)
            if not hint:
                continue

            refused.append(f"column {field.name}: {field.type} — {hint}")

        return refused

    def _hint(self, arrow: pyarrow.DataType) -> str:
        if isinstance(arrow, pyarrow.UuidType):
            return OraBindHint.UUID.value

        checks: Sequence[tuple[Callable[[pyarrow.DataType], bool], OraBindHint]] = (
            (pyarrow.types.is_time, OraBindHint.TIME),
            (pyarrow.types.is_duration, OraBindHint.DURATION),
            (pyarrow.types.is_interval, OraBindHint.INTERVAL),
            (pyarrow.types.is_fixed_size_binary, OraBindHint.FIXED),
            (pyarrow.types.is_dictionary, OraBindHint.DICTIONARY),
        )
        for check, hint in checks:
            if check(arrow):
                return hint.value

        if pyarrow.types.is_boolean(arrow) and not self._has_boolean:
            return OraBindHint.BOOLEAN.value

        return ""


class OraNeutralFacts:
    """Таблица приёмника по описанию сервера: `select * from <таблица>`
    разбирается без выполнения, колонки — в нейтральном виде через
    OraColumnKinds (имена строчными, not null из null_ok); ORA-00942 —
    таблицы нет. Мажорная версия сервера решает, есть ли BOOLEAN."""

    BOOLEAN_SINCE: ClassVar[int] = 23
    LOBS: ClassVar[frozenset[str]] = frozenset({"CLOB", "NCLOB", "BLOB"})

    def __init__(
        self,
        conn: AsyncConnection,
        table: OraTableRef,
        payload: PayloadOracle,
        journal: CommandJournal,
    ) -> None:
        self._conn = conn
        self._table = table
        self._payload = payload
        self._journal = journal
        self._kinds = OraColumnKinds()

    def major(self) -> int:
        release, *_ = str(self._conn.version).split(".")

        return int(release)

    def has_boolean(self) -> bool:
        return self.major() >= self.BOOLEAN_SINCE

    async def exists(self) -> bool:
        text = self._select_all().text
        try:
            with self._journal.command(text, CommandKind.LOOKUP) as running:
                await self._payload.describe(self._conn, text)
                running.status = "exists: True"
        except OracleMissingObjectError:
            return False

        return True

    async def columns(self) -> tuple[ColumnSpec, ...]:
        text = self._select_all().text
        with self._journal.command(text, CommandKind.LOOKUP) as running:
            described = await self._payload.describe(self._conn, text)
            running.status = f"{len(described)} columns described"

        return self._kinds.specs(described)

    async def lobs(self) -> frozenset[str]:
        """Колонки LOB таблицы: их bind'ы идут последними (ORA-24816)."""
        names: set[str] = set()
        for column in await self.columns():
            if column.source_type in self.LOBS:
                names.add(column.name)

        return frozenset(names)

    def _select_all(self) -> OraQuery:
        return OraQueryBuilder().add("select * from ", self._table).build()


class OraLobProbe(StrEnum):
    """Выражения типа LOB для описания сервером: cast(null as CLOB) Oracle не
    принимает (ORA-00932, ORA-22849), поэтому LOB из rules.column_types
    описывается своим пустым значением."""

    CLOB = "empty_clob()"
    NCLOB = "to_nclob(empty_clob())"
    BLOB = "empty_blob()"


class OraDeclaredTypes:
    """Типы из rules.column_types, описанные сервером приёмника: parse
    `select cast(null as <тип>) from dual` без выполнения, LOB — своим
    пустым значением; дальше OraColumnKinds по метаданным драйвера, как у
    колонок таблицы."""

    def __init__(
        self, conn: AsyncConnection, payload: PayloadOracle, journal: CommandJournal
    ) -> None:
        self._conn = conn
        self._payload = payload
        self._journal = journal
        self._kinds = OraColumnKinds()

    async def resolve(self, column_types: Mapping[str, str]) -> dict[str, DeclaredType]:
        declared: dict[str, DeclaredType] = {}
        for name, text in column_types.items():
            probe = self._probe(text).text
            try:
                with self._journal.command(probe, CommandKind.LOOKUP) as running:
                    described = await self._payload.describe(self._conn, probe)
                    running.status = "type described"
            except OracleQueryError as exc:
                raise TransferError(
                    f'rules.column_types["{name}"]: the target server does not '
                    f"accept the type {text!r}: {exc}"
                ) from exc

            spec = self._kinds.specs(described)[0]
            declared[name] = DeclaredType(
                kind=spec.kind,
                source_type=spec.source_type,
                char_length=spec.char_length,
            )

        return declared

    @staticmethod
    def _probe(text: str) -> OraQuery:
        lob = OraLobProbe.__members__.get(text.strip().upper())
        if lob is not None:
            return (
                OraQueryBuilder().add("select ", lob.value, ' as "c" from dual').build()
            )

        return (
            OraQueryBuilder()
            .add("select cast(null as")
            .raw_query(text)
            .add(') as "c" from dual')
            .build()
        )


class OraStreamProjection:
    """Поле нейтрального потока так, как его хранит Oracle, по контракту и
    настоящему типу поля Arrow: uuid и time, пришедшие текстом, — строки
    из 36 и 18 символов, json, inet, interval, money, xml, bit — строки,
    date — момент в секундах (DATE), двоичное семейство, которое источник
    прислал текстом (utf8), — строка. Строки чужого движка
    всегда nullable: пустую строку Oracle хранит как NULL, и not null для
    неё невыполним; у источника Oracle пустых строк не бывает. Имена полей
    строчными, как имена колонок, которые описывает сервер."""

    AS_STRING: ClassVar[frozenset[TypeFamily]] = frozenset(
        {
            TypeFamily.JSON,
            TypeFamily.NETWORK,
            TypeFamily.INTERVAL,
            TypeFamily.MONEY,
            TypeFamily.XML,
            TypeFamily.BIT,
        }
    )
    LENGTHS: ClassVar[Mapping[TypeFamily, int]] = {
        TypeFamily.UUID: 36,
        TypeFamily.TIME: 18,
    }

    def __init__(self, source_engine: Engine) -> None:
        self._strings_nullable = source_engine is not Engine.ORACLE

    def project(
        self, columns: Sequence[ColumnSpec], schema: pyarrow.Schema
    ) -> tuple[ColumnSpec, ...]:
        if len(columns) != len(schema):
            raise TransferError(
                f"the contract has {len(columns)} columns, the arrow stream has "
                f"{len(schema)} fields"
            )

        projected: list[ColumnSpec] = []
        for column, field in zip(columns, schema, strict=True):
            projected.append(self._column(column, field))

        return tuple(projected)

    def _column(self, column: ColumnSpec, field: pyarrow.Field) -> ColumnSpec:
        name = column.name.lower()
        family = column.kind.family
        text = column.kind.text
        if family in self.AS_STRING:
            return self._string(column, name, text, 0)

        if family is TypeFamily.BINARY and self._is_text(field.type):
            return self._string(column, name, text, 0)

        if family in self.LENGTHS:
            return self._string(column, name, text, self.LENGTHS[family])

        if family is TypeFamily.STRING:
            return self._string(column, name, text, column.char_length)

        if family is TypeFamily.DATE:
            return replace(
                column,
                name=name,
                kind=ColumnType(TypeFamily.TIMESTAMP, text, unit=TimeUnit.SECOND),
            )

        return replace(column, name=name)

    @staticmethod
    def _is_text(arrow: pyarrow.DataType) -> bool:
        if pyarrow.types.is_string(arrow):
            return True

        return pyarrow.types.is_large_string(arrow)

    def _string(
        self, column: ColumnSpec, name: str, text: str, char_length: int
    ) -> ColumnSpec:
        nullable = column.nullable
        if self._strings_nullable:
            nullable = True

        return replace(
            column,
            name=name,
            nullable=nullable,
            kind=ColumnType(TypeFamily.STRING, text),
            char_length=char_length,
        )


class OraDdlTypes:
    """Текст типа Oracle для колонки потока: ddl_type из правил приёмника
    как есть; текст типа источника, если источник — Oracle и тип назван;
    семейство other — CLOB по стратегии неизвестных типов или ошибка; иначе
    по нейтральному типу: целые — NUMBER(p) по ширине, decimal —
    NUMBER(p, s), float — BINARY_FLOAT/BINARY_DOUBLE, строки —
    VARCHAR2(n CHAR) до 4000 символов и CLOB дальше или без длины,
    двоичные — RAW(n) до 2000 байт и BLOB, timestamp — TIMESTAMP(p) [WITH
    TIME ZONE], момент в секундах без пояса — DATE, boolean — BOOLEAN или
    NUMBER(1)."""

    INTEGERS: ClassVar[Mapping[int, str]] = {
        1: "NUMBER(1)",
        8: "NUMBER(3)",
        16: "NUMBER(5)",
        32: "NUMBER(10)",
        64: "NUMBER(19)",
    }
    UNSIGNED: ClassVar[Mapping[int, str]] = {
        1: "NUMBER(1)",
        8: "NUMBER(3)",
        16: "NUMBER(5)",
        32: "NUMBER(10)",
        64: "NUMBER(20)",
    }
    FLOATS: ClassVar[Mapping[int, str]] = {32: "BINARY_FLOAT", 64: "BINARY_DOUBLE"}
    PRECISION: ClassVar[Mapping[TimeUnit, int]] = {
        TimeUnit.SECOND: 0,
        TimeUnit.MILLISECOND: 3,
        TimeUnit.MICROSECOND: 6,
        TimeUnit.NANOSECOND: 9,
    }
    VARCHAR_MAX: ClassVar[int] = 4000
    RAW_MAX: ClassVar[int] = 2000
    NUMBER_MAX: ClassVar[int] = 38
    CLOB: ClassVar[str] = "CLOB"
    BLOB: ClassVar[str] = "BLOB"
    DATE: ClassVar[str] = "DATE"
    BOOLEAN: ClassVar[str] = "BOOLEAN"
    BOOLEAN_AS_NUMBER: ClassVar[str] = "NUMBER(1)"

    def __init__(self, has_boolean: bool) -> None:
        self._has_boolean = has_boolean

    def render(
        self, column: TableColumn, source_engine: Engine, unknown_as_varchar: bool
    ) -> str:
        if column.ddl_type:
            return column.ddl_type

        if source_engine is Engine.ORACLE and column.source_type:
            return column.source_type

        if column.kind.family.named_only():
            return self._unknown(column, unknown_as_varchar)

        return self.of_kind(column.kind, column.char_length)

    def planned(self, column: TableColumn, source_engine: Engine) -> str:
        """Тип колонки в плане загрузки; пусто — типа нет, решит стратегия
        неизвестных типов."""
        if column.ddl_type:
            return column.ddl_type

        if source_engine is Engine.ORACLE and column.source_type:
            return column.source_type

        if column.kind.family.named_only():
            return ""

        return self.of_kind(column.kind, column.char_length)

    def _unknown(self, column: TableColumn, unknown_as_varchar: bool) -> str:
        if unknown_as_varchar:
            return self.CLOB

        known = column.source_type
        if not known:
            known = column.kind.text

        raise TransferError(
            f"column {column.name}: the target cannot map the source type "
            f"{known} (family {column.kind.family.value}); declare the target "
            f'type in rules.column_types["{column.name}"] or take unknown_types '
            f"fallback_as_varchar"
        )

    def of_kind(self, kind: ColumnType, char_length: int) -> str:
        renderers: Mapping[TypeFamily, Callable[[ColumnType, int], str]] = {
            TypeFamily.INTEGER: self._integer,
            TypeFamily.FLOAT: self._float,
            TypeFamily.DECIMAL: self._decimal,
            TypeFamily.STRING: self._string,
            TypeFamily.BINARY: self._binary,
            TypeFamily.TIMESTAMP: self._timestamp,
            TypeFamily.BOOLEAN: self._boolean,
        }
        render = renderers.get(kind.family)
        if render is None:
            raise TransferError(
                f"the target has no type for the stream family "
                f"{kind.family.value} ({kind.text})"
            )

        return render(kind, char_length)

    def _integer(self, kind: ColumnType, char_length: int) -> str:
        if kind.unsigned:
            return self.UNSIGNED.get(kind.bits, "NUMBER(20)")

        return self.INTEGERS.get(kind.bits, "NUMBER(19)")

    def _float(self, kind: ColumnType, char_length: int) -> str:
        return self.FLOATS.get(kind.bits, "BINARY_DOUBLE")

    def _decimal(self, kind: ColumnType, char_length: int) -> str:
        if kind.precision == 0:
            return "NUMBER"

        if kind.precision > self.NUMBER_MAX:
            return "NUMBER"

        return f"NUMBER({kind.precision},{kind.scale})"

    def _string(self, kind: ColumnType, char_length: int) -> str:
        if char_length == 0:
            return self.CLOB

        if char_length > self.VARCHAR_MAX:
            return self.CLOB

        return f"VARCHAR2({char_length} CHAR)"

    def _binary(self, kind: ColumnType, char_length: int) -> str:
        if char_length == 0:
            return self.BLOB

        if char_length > self.RAW_MAX:
            return self.BLOB

        return f"RAW({char_length})"

    def _timestamp(self, kind: ColumnType, char_length: int) -> str:
        precision = self.PRECISION[kind.unit]
        if kind.zoned:
            return f"TIMESTAMP({precision}) WITH TIME ZONE"

        if kind.unit is TimeUnit.SECOND:
            return self.DATE

        return f"TIMESTAMP({precision})"

    def _boolean(self, kind: ColumnType, char_length: int) -> str:
        if self._has_boolean:
            return self.BOOLEAN

        return self.BOOLEAN_AS_NUMBER


class OraArrowTable(TransferTable):
    """Реализация TransferTable для Oracle по нейтральному плану таблицы:
    create table по шаблону вызывающего (схема, имя и колонки подставляются
    экранированными драйвером), бэкап — rename в _bak_<время>, drop с
    purge, truncate и delete; стейтменты собирает OraQueryBuilder, условие
    where от вызова — raw_query. DDL Oracle фиксирует сам."""

    BACKUP_STAMP: ClassVar[str] = "%Y%m%d_%H%M%S_%f"
    NO_SUCH_TYPE: ClassVar[str] = "ORA-00902"

    def __init__(  # noqa: PLR0913 — фикстуры теста
        self,
        conn: AsyncConnection,
        table: OraTableRef,
        source_engine: Engine,
        spec: TableSpec,
        template: CreateTemplate,
        facts: OraNeutralFacts,
        journal: CommandJournal,
    ) -> None:
        self._conn = conn
        self._table = table
        self._source_engine = source_engine
        self._spec = spec
        self._template = template
        self._facts = facts
        self._journal = journal
        self._ddl_types = OraDdlTypes(facts.has_boolean())

    def planned(self) -> list[PlannedColumn]:
        """Колонки загрузки с типами Oracle для отчёта."""
        planned: list[PlannedColumn] = []
        for column in self._spec.columns:
            type_text = self._ddl_types.planned(column, self._source_engine)
            planned.append(column.planned(type_text))

        return planned

    async def exists(self) -> bool:
        return await self._facts.exists()

    async def create(self, unknown_as_varchar: bool) -> str:
        rendered = self._template.render(
            {
                TemplateVar.SCHEMA_NAME: OraIdentifier(self._table.schema).render(),
                TemplateVar.TABLE_NAME: OraIdentifier(self._table.name).render(),
                TemplateVar.COLUMNS: self._columns_fragment(unknown_as_varchar),
            }
        )
        query = OraQueryBuilder().raw_query(rendered).build()
        try:
            await self._execute(query)
        except OracleQueryError as exc:
            if self.NO_SUCH_TYPE not in str(exc):
                raise

            raise TransferError(
                f"create table {self._table.text()}: the target server has no such "
                f"type: {exc}; declare another type for the column in "
                f"rules.column_types, for example CLOB; statement: {query.text}"
            ) from exc

        return query.text

    def _columns_fragment(self, unknown_as_varchar: bool) -> str:
        builder = OraQueryBuilder()
        for position, column in enumerate(self._spec.columns):
            type_text = self._ddl_types.render(
                column, self._source_engine, unknown_as_varchar
            )
            builder.when(position > 0, ",")
            builder.add(OraIdentifier(column.name))
            builder.raw_query(type_text)
            builder.when(not column.nullable, "not null")

        return builder.build().text

    async def backup(self) -> str:
        stamp = datetime.now(UTC).strftime(self.BACKUP_STAMP)
        renamed = f"{self._table.name}_bak_{stamp}"
        query = (
            OraQueryBuilder()
            .add("alter table ", self._table, " rename to ", OraIdentifier(renamed))
            .build()
        )
        await self._execute(query)

        return renamed

    async def drop(self, cascade: bool) -> str:
        query = (
            OraQueryBuilder()
            .add("drop table ", self._table)
            .when(cascade, "cascade constraints")
            .add("purge")
            .build()
        )
        await self._execute(query)

        return query.text

    async def truncate(self) -> DeleteOutcome:
        query = OraQueryBuilder().add("truncate table ", self._table).build()
        await self._execute(query)

        return DeleteOutcome(effect="removed by truncate")

    async def delete_all(self) -> DeleteOutcome:
        query = OraQueryBuilder().add("delete from ", self._table).build()

        return await self._deleted(query)

    async def delete_where(self, where: str) -> DeleteOutcome:
        query = (
            OraQueryBuilder()
            .add("delete from ", self._table, " where")
            .raw_query(where)
            .build()
        )

        return await self._deleted(query)

    async def _execute(self, query: OraQuery) -> int:
        """Команда в журнале вызова: число затронутых строк."""
        with self._journal.command(query.text, CommandKind.ACTION) as running:
            cursor = self._conn.cursor()
            try:
                await cursor.execute(query.text, query.params)
                rows = cursor.rowcount
            except oracledb.Error as exc:
                raise OracleQueryError(
                    f"statement on oracle failed: {type(exc).__name__}: {exc}; "
                    f"statement: {query.text!r}"
                ) from exc
            finally:
                cursor.close()

            rows = max(rows, 0)
            running.status = f"{rows} rows"

        return rows

    async def _deleted(self, query: OraQuery) -> DeleteOutcome:
        rows = await self._execute(query)

        return DeleteOutcome(effect=f"{rows} rows deleted")


@dataclass(frozen=True)
class OraInsertPlan:
    """Стейтмент insert и порядок полей потока под него: колонки LOB стоят
    последними, потому что Oracle не принимает обычный bind после LOB
    (ORA-24816); драйвер раздаёт bind'ы по порядку появления в тексте, так
    что и колонки пачки переставляются в тот же порядок."""

    query: OraQuery
    order: tuple[int, ...]


class OraArrowSink(TransferSink):
    """Реализация TransferSink для потока Arrow: пачки в insert таблицы по
    именам колонок через executemany, bind'ы драйвер берёт из массивов
    пачки как есть. Колонки LOB стоят в стейтменте последними, их массивы в
    пачке — тоже: select колонок пачки не копирует значения."""

    def __init__(  # noqa: PLR0913 — фикстуры теста
        self,
        conn: AsyncConnection,
        table: OraTableRef,
        names: Sequence[str],
        reader: ArrowReader,
        payload: PayloadOracle,
        trace: OraSessionTrace,
        facts: OraNeutralFacts,
        journal: CommandJournal,
    ) -> None:
        self._conn = conn
        self._table = table
        self._names = tuple(names)
        self._reader = reader
        self._payload = payload
        self._trace = trace
        self._facts = facts
        self._journal = journal

    async def load(self) -> int:
        """Вставка потока целиком — одна команда журнала: пачки идут мимо него."""
        plan = self._plan(await self._facts.lobs())
        rows = 0
        with self._journal.command(plan.query.text, CommandKind.ACTION) as running:
            async for batch in self._reader.batches:
                rows += await self._payload.executemany_arrow(
                    self._conn,
                    plan.query.text,
                    batch.select(list(plan.order)),
                    self._trace,
                )

            running.status = f"{rows} rows inserted"

        return rows

    def method(self) -> str:
        """Строки идут прямо в таблицу в транзакции вызова: пояснять нечего."""
        return ""

    async def discard(self) -> int:
        async for _ in self._reader.batches:
            pass

        return 0

    def _plan(self, lobs: frozenset[str]) -> OraInsertPlan:
        plain: list[int] = []
        deferred: list[int] = []
        for index, name in enumerate(self._names):
            if name in lobs:
                deferred.append(index)
                continue

            plain.append(index)

        order = tuple(plain + deferred)
        names: list[str] = []
        for index in order:
            names.append(self._names[index])

        query = (
            OraQueryBuilder()
            .add("insert into ", self._table, " (", OraIdentifiers(names), ")")
            .add("values (", OraBindMarks(len(names)), ")")
            .build()
        )

        return OraInsertPlan(query=query, order=order)


class OraArrowLoader:
    """Приёмник потока arrow в Oracle: нейтральный контракт потока в
    представлении Oracle -> колонки таблицы по описанию сервера -> сверка по
    семействам -> план таблицы текстами типов Oracle -> ход стратегий
    TransferRun со вставкой пачек через OraArrowSink. Сессия переводится в
    UTC, чтобы моменты без пояса ложились в колонки WITH TIME ZONE как UTC.
    Транзакция соединения покрывает удаление и вставку; DDL сервер
    фиксирует сам."""

    SESSION_UTC: ClassVar[str] = "alter session set time_zone = 'UTC'"

    def __init__(  # noqa: PLR0913 — фикстуры теста
        self,
        conn: AsyncConnection,
        table: OraTableRef,
        contract: Sequence[ColumnSpec],
        source_engine: Engine,
        inbound: TransferInbound,
        chunk_bytes: int,
        payload: PayloadOracle,
        trace: OraSessionTrace,
        journal: CommandJournal,
    ) -> None:
        self._conn = conn
        self._table = table
        self._journal = journal
        self._facts = OraNeutralFacts(conn, table, payload, journal)
        self._contract = tuple(contract)
        self._projection = OraStreamProjection(source_engine)
        self._binds = OraArrowBinds(self._facts.has_boolean())
        self._source_engine = source_engine
        self._exact = source_engine is Engine.ORACLE
        self._inbound = inbound
        self._chunk_bytes = chunk_bytes
        self._payload = payload
        self._trace = trace
        self._declared = OraDeclaredTypes(conn, payload, journal)
        self._ipc = ArrowIpc()

    async def run(  # noqa: PLR0913 — фикстуры теста
        self,
        schema_strategy: SchemaStrategyPlan,
        delete_strategy: DeleteStrategyApply,
        insert_strategy: InsertStrategyApply,
        unknown_types: UnknownTypeApply,
        rules: ColumnRules,
        create_table: CreateTemplate,
    ) -> TransferReport:
        reader = await self._ipc.open_in(self._inbound.raw(), self._chunk_bytes)
        refused = self._binds.refusals(reader.schema)
        if refused:
            listed = "\n".join(refused)
            raise TransferError(
                f"ora_stream_in into {self._table.text()}: the driver does not bind "
                f"these arrow fields as they are, convert them in the source "
                f"query:\n{listed}"
            )

        stream = self._projection.project(self._contract, reader.schema)
        with self._journal.command(self.SESSION_UTC, CommandKind.ACTION) as running:
            async with self._payload.rows(self._conn, self.SESSION_UTC) as session:
                if session.warning:
                    self._trace.warn(session.warning)

            running.status = "session altered"

        declared = await self._declared.resolve(rules.column_types)
        exists = await self._facts.exists()
        facts: Sequence[ColumnSpec] = ()
        if exists:
            facts = await self._facts.columns()

        diff = SchemaMatcher(rules, self._exact).diff(stream, facts, declared)
        spec = diff.table_spec()
        table = OraArrowTable(
            self._conn,
            self._table,
            self._source_engine,
            spec,
            create_table,
            self._facts,
            self._journal,
        )
        sink = OraArrowSink(
            self._conn,
            self._table,
            spec.names(),
            reader,
            self._payload,
            self._trace,
            self._facts,
            self._journal,
        )
        transfer = TransferRun(
            schema_strategy,
            delete_strategy,
            insert_strategy,
            unknown_types,
            self._journal,
        )

        return await transfer.run(
            self._table.text(),
            exists,
            diff.check(),
            table.planned(),
            table,
            sink,
            False,
        )
