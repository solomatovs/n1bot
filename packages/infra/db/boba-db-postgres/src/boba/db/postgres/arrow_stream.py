"""Провод arrow для postgres: источник PgArrowSource (нейтральный контракт из
описания колонок libpq с декларациями вызова, тела — поток Arrow IPC) и
приёмник для контракта любого движка: колонки каталога в нейтральном виде,
сверка по семействам, таблица как порт TransferTable, вставка пачек через
PgArrowIn и ход стратегий TransferRun одной транзакцией. Работает, когда
пары движков нет или LLM назвал формат arrow.

Ошибки:
ContractError — декларация на колонку, которой нет в ответе; правило вызова
    не сходится со схемами; тип из rules.column_types сервер не знает.
TransferError — стратегия схемы отказала; колонка без типа при
    fail_on_unknown.
PgArrowError — стейтмент не описывается сервером или тип пачки потока не
    пишется в CSV.
psycopg.Error — сервер отклонил стейтмент или значение.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, ClassVar

import psycopg
from psycopg import sql

from boba.db.postgres.arrow import PgArrowIn, PgArrowOut, PgArrowTypes
from boba.db.postgres.describe import PgDescribedColumn
from boba.db.postgres.query import PgQuery, PgQueryBuilder
from boba.db.postgres.trace import PgCommandReport
from boba.db.postgres.transfer import (
    PgColumnDeclaration,
    PgTableFacts,
    PgTableRef,
    PgTypeResolver,
)
from boba.toolkit.arrow import ArrowIpc, ArrowReader
from boba.toolkit.contract import (
    ArrowContract,
    ColumnDeclaration,
    ColumnSpec,
    ColumnType,
    ContractError,
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
    CreateTemplate,
    DeleteOutcome,
    DeleteStrategyApply,
    InsertStrategyApply,
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
    "PgArrowLoader",
    "PgArrowSink",
    "PgArrowSource",
    "PgArrowTable",
    "PgDdlTypes",
    "PgNeutralFacts",
]


class PgDdlTypes:
    """Текст типа postgres для колонки потока: ddl_type из правил приёмника
    как есть; текст типа источника, если источник — postgres и тип назван;
    семейство other — varchar по стратегии неизвестных типов или ошибка;
    иначе по нейтральному типу (bigint, numeric(p, s), character varying(n),
    timestamp(p), uuid, jsonb, interval, time(p) ...)."""

    INTEGERS: ClassVar[Mapping[int, str]] = {
        8: "smallint",
        16: "smallint",
        32: "integer",
        64: "bigint",
    }
    UNSIGNED: ClassVar[Mapping[int, str]] = {
        8: "smallint",
        16: "integer",
        32: "bigint",
        64: "numeric(20)",
    }
    FLOATS: ClassVar[Mapping[int, str]] = {32: "real", 64: "double precision"}
    PRECISION: ClassVar[Mapping[TimeUnit, int]] = {
        TimeUnit.SECOND: 0,
        TimeUnit.MILLISECOND: 3,
        TimeUnit.MICROSECOND: 6,
        TimeUnit.NANOSECOND: 6,
    }
    PLAIN: ClassVar[Mapping[TypeFamily, str]] = {
        TypeFamily.BOOLEAN: "boolean",
        TypeFamily.BINARY: "bytea",
        TypeFamily.DATE: "date",
        TypeFamily.UUID: "uuid",
        TypeFamily.JSON: "jsonb",
        TypeFamily.INTERVAL: "interval",
        TypeFamily.NETWORK: "inet",
        TypeFamily.MONEY: "money",
        TypeFamily.XML: "xml",
    }

    VARCHAR: ClassVar[str] = "varchar"

    def render(
        self, column: TableColumn, source_engine: Engine, unknown_as_varchar: bool
    ) -> str:
        if column.ddl_type:
            return column.ddl_type

        if source_engine is Engine.POSTGRES and column.source_type:
            return column.source_type

        if column.kind.family.named_only():
            return self._unknown(column, unknown_as_varchar)

        return self.of_kind(column.kind, column.char_length)

    def _unknown(self, column: TableColumn, unknown_as_varchar: bool) -> str:
        if unknown_as_varchar:
            return self.VARCHAR

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
        plain = self.PLAIN.get(kind.family)
        if plain is not None:
            return plain

        renderers: Mapping[TypeFamily, Callable[[ColumnType, int], str]] = {
            TypeFamily.INTEGER: self._integer,
            TypeFamily.FLOAT: self._float,
            TypeFamily.DECIMAL: self._decimal,
            TypeFamily.STRING: self._string,
            TypeFamily.BIT: self._bit,
            TypeFamily.TIMESTAMP: self._timestamp,
            TypeFamily.TIME: self._time,
        }

        return renderers[kind.family](kind, char_length)

    @staticmethod
    def _bit(kind: ColumnType, char_length: int) -> str:
        if char_length > 0:
            return f"bit varying({char_length})"

        return "bit varying"

    def _integer(self, kind: ColumnType, char_length: int) -> str:
        if kind.unsigned:
            return self.UNSIGNED.get(kind.bits, "numeric(20)")

        return self.INTEGERS.get(kind.bits, "numeric(20)")

    def _float(self, kind: ColumnType, char_length: int) -> str:
        return self.FLOATS.get(kind.bits, "double precision")

    @staticmethod
    def _decimal(kind: ColumnType, char_length: int) -> str:
        if kind.precision == 0:
            return "numeric"

        return f"numeric({kind.precision}, {kind.scale})"

    @staticmethod
    def _string(kind: ColumnType, char_length: int) -> str:
        if char_length > 0:
            return f"character varying({char_length})"

        return "text"

    def _timestamp(self, kind: ColumnType, char_length: int) -> str:
        precision = self.PRECISION[kind.unit]
        if kind.zoned:
            return f"timestamp({precision}) with time zone"

        return f"timestamp({precision})"

    def _time(self, kind: ColumnType, char_length: int) -> str:
        precision = self.PRECISION[kind.unit]
        if kind.zoned:
            return f"time({precision}) with time zone"

        return f"time({precision})"


class PgDeclaredTypes:
    """Типы из rules.column_types в нейтральном виде: разбор текста типа
    сервером через PgTypeResolver, семейство и параметры через PgArrowTypes."""

    def __init__(self, conn: psycopg.AsyncConnection[Any]) -> None:
        self._resolver = PgTypeResolver(conn)
        self._types = PgArrowTypes(conn.adapters.types)

    async def resolve(self, column_types: Mapping[str, str]) -> dict[str, DeclaredType]:
        try:
            resolved = await self._resolver.resolve(column_types)
        except TransferError as exc:
            raise ContractError(str(exc)) from exc

        declared: dict[str, DeclaredType] = {}
        for name, column in resolved.items():
            declared[name] = DeclaredType(
                kind=self._types.kind_of(column),
                source_type=self._types.source_type(column),
                char_length=self._types.char_length(column),
            )

        return declared


class PgNeutralFacts:
    """Колонки таблицы приёмника из каталога в нейтральном виде: те же
    семейства, что у контракта потока, через PgArrowTypes."""

    def __init__(self, conn: psycopg.AsyncConnection[Any], table: PgTableRef) -> None:
        self._facts = PgTableFacts(conn, table)
        self._types = PgArrowTypes(conn.adapters.types)

    async def exists(self) -> bool:
        return await self._facts.exists()

    async def columns(self) -> Sequence[ColumnSpec]:
        specs: list[ColumnSpec] = []
        for column in await self._facts.columns():
            specs.append(
                ColumnSpec(
                    name=column.name,
                    kind=self._types.kind_of(column),
                    nullable=not column.not_null,
                    position=column.position,
                    source_type=self._types.source_type(column),
                    char_length=self._types.char_length(column),
                )
            )

        return tuple(specs)


class PgArrowTable(TransferTable):
    """Реализация TransferTable для postgres по нейтральному плану таблицы:
    DDL и delete через PgQueryBuilder на одном соединении, условие where от
    вызова — raw_query. create table — по шаблону вызывающего: схема, имя и
    колонки подставляются экранированными psycopg фрагментами."""

    BACKUP_STAMP: ClassVar[str] = "%Y%m%d_%H%M%S_%f"

    def __init__(
        self,
        conn: psycopg.AsyncConnection[Any],
        table: PgTableRef,
        source_engine: Engine,
        spec: TableSpec,
        template: CreateTemplate,
    ) -> None:
        self._conn = conn
        self._table = table
        self._source_engine = source_engine
        self._spec = spec
        self._template = template
        self._facts = PgTableFacts(conn, table)
        self._ddl_types = PgDdlTypes()

    async def exists(self) -> bool:
        return await self._facts.exists()

    async def create(self, unknown_as_varchar: bool) -> str:
        query = self._create_query(unknown_as_varchar)
        try:
            await self._execute(query)
        except psycopg.errors.UndefinedObject as exc:
            raise TransferError(
                f"create table {self._table.text()}: the target server has no such "
                f"type: {exc.diag.message_primary}; declare another type for the "
                f"column in rules.column_types, for example text; statement: "
                f"{query.text.as_string(self._conn)}"
            ) from exc

        return query.text.as_string(self._conn)

    def _create_query(self, unknown_as_varchar: bool) -> PgQuery:
        rendered = self._template.render(
            {
                TemplateVar.SCHEMA_NAME: sql.Identifier(self._table.schema).as_string(
                    self._conn
                ),
                TemplateVar.TABLE_NAME: sql.Identifier(self._table.name).as_string(
                    self._conn
                ),
                TemplateVar.COLUMNS: self._columns_fragment(unknown_as_varchar),
            }
        )

        return PgQueryBuilder().raw_query(rendered).build()

    def _columns_fragment(self, unknown_as_varchar: bool) -> str:
        builder = PgQueryBuilder()
        for position, column in enumerate(self._spec.columns):
            builder.when(position > 0, ",")
            builder.add("{name}", name=sql.Identifier(column.name))
            builder.raw_query(
                self._ddl_types.render(column, self._source_engine, unknown_as_varchar)
            )
            builder.when(not column.nullable, "not null")

        return builder.build().text.as_string(self._conn)

    async def backup(self) -> str:
        stamp = datetime.now(UTC).strftime(self.BACKUP_STAMP)
        renamed = f"{self._table.name}_bak_{stamp}"
        query = (
            PgQueryBuilder(table=self._table.ident())
            .add(
                "alter table {table} rename to {renamed}",
                renamed=sql.Identifier(renamed),
            )
            .build()
        )
        await self._execute(query)

        return renamed

    async def drop(self, cascade: bool) -> str:
        query = (
            PgQueryBuilder(table=self._table.ident())
            .add("drop table {table}")
            .when(cascade, "cascade")
            .build()
        )
        await self._execute(query)

        return query.text.as_string(self._conn)

    async def truncate(self) -> DeleteOutcome:
        query = (
            PgQueryBuilder(table=self._table.ident())
            .add("truncate table {table}")
            .build()
        )
        await self._execute(query)

        return DeleteOutcome(
            effect="removed by truncate", statement=query.text.as_string(self._conn)
        )

    async def delete_all(self) -> DeleteOutcome:
        query = (
            PgQueryBuilder(table=self._table.ident()).add("delete from {table}").build()
        )

        return await self._deleted(query)

    async def delete_where(self, where: str) -> DeleteOutcome:
        query = (
            PgQueryBuilder(table=self._table.ident())
            .add("delete from {table} where")
            .raw_query(where)
            .build()
        )

        return await self._deleted(query)

    async def _execute(self, query: PgQuery) -> None:
        async with self._conn.cursor() as cursor:
            await cursor.execute(query.text, query.params)

    async def _deleted(self, query: PgQuery) -> DeleteOutcome:
        async with self._conn.cursor() as cursor:
            await cursor.execute(query.text, query.params)
            rows = cursor.rowcount

        rows = max(rows, 0)

        return DeleteOutcome(
            effect=f"{rows} rows deleted", statement=query.text.as_string(self._conn)
        )


class PgArrowSource:
    """Источник потока arrow: нейтральный контракт из колонок RowDescription
    с декларациями вызова поверх, схема потока из контракта, тела — поток
    Arrow IPC через PgArrowOut."""

    ENGINE: ClassVar[WireEngine] = WireEngine.POSTGRES

    def __init__(self, conn: psycopg.AsyncConnection[Any]) -> None:
        self._conn = conn
        self._out = PgArrowOut(conn)
        self._types = PgArrowTypes(conn.adapters.types)
        self._declarations = Declarations()
        self._contract = StreamContract()
        self._contract_table = ContractTable()

    def contract(
        self,
        columns: Iterable[PgDescribedColumn],
        declarations: Sequence[PgColumnDeclaration],
    ) -> tuple[ColumnSpec, ...]:
        """Нейтральный контракт: описание колонок и декларации postgres,
        переведённые в нейтральные (nullable, имя типа)."""
        declared: list[ColumnDeclaration] = []
        for declaration in declarations:
            declared.append(
                ColumnDeclaration(
                    name=declaration.name,
                    nullable=declaration.nullable,
                    source_type=declaration.type_text,
                )
            )

        specs: list[ColumnSpec] = []
        for column in columns:
            specs.append(
                ColumnSpec(
                    name=column.name,
                    kind=self._types.kind_of(column),
                    nullable=True,
                    position=column.position,
                    source_type=self._types.source_type(column),
                    char_length=self._types.char_length(column),
                )
            )

        return self._declarations.merge(specs, declared)

    async def stream(
        self,
        text: str,
        specs: Sequence[ColumnSpec],
        chunk_bytes: int,
        out: TransferOutbound,
    ) -> PgCommandReport:
        contract = ArrowContract(columns=self._contract.columns(specs))
        await out.schema(
            SchemaHead(
                kind="schema",
                source_engine=self.ENGINE,
                wire=StreamWire.ARROW,
                contract=contract.model_dump(mode="json"),
            )
        )
        schema = self._types.schema_of(specs)
        report = await self._out.stream_into(text, schema, chunk_bytes, out.writer())

        return PgCommandReport(
            summary=self._contract_table.caption(StreamWire.ARROW.value, specs),
            columns=self._contract_table.rows(specs),
            status=report.status,
            statement=report.statement,
            backend_pid=report.backend_pid,
            server_version=report.server_version,
            rows=report.rows,
            notices=report.notices,
            notifies=report.notifies,
        )


class PgCopyStatement:
    """COPY таблицы по именам колонок в порядке полей потока."""

    def __init__(self, conn: psycopg.AsyncConnection[Any], table: PgTableRef) -> None:
        self._conn = conn
        self._table = table

    def query(self, names: Sequence[str]) -> PgQuery:
        idents: list[sql.Identifier] = []
        for name in names:
            idents.append(sql.Identifier(name))

        return (
            PgQueryBuilder(table=self._table.ident())
            .add(
                "copy {table} ({columns}) from stdin (format csv)",
                columns=sql.SQL(", ").join(idents),
            )
            .build()
        )


class PgArrowSink(TransferSink):
    """Реализация TransferSink для потока Arrow: пачки в COPY таблицы по
    именам колонок в порядке полей потока через PgArrowIn."""

    def __init__(
        self,
        conn: psycopg.AsyncConnection[Any],
        table: PgTableRef,
        names: Sequence[str],
        reader: ArrowReader,
        exact_floats: bool,
    ) -> None:
        self._conn = conn
        self._table = table
        self._names = tuple(names)
        self._reader = reader
        self._arrow_in = PgArrowIn(conn, exact_floats)

    async def load(self) -> int:
        query = PgCopyStatement(self._conn, self._table).query(self._names)
        report = await self._arrow_in.copy_query(query, self._reader)

        return report.rows

    def method(self) -> str:
        """Строки идут прямо в таблицу в транзакции вызова: пояснять нечего."""
        return ""

    async def discard(self) -> int:
        async for _ in self._reader.batches:
            pass

        return 0


class PgArrowLoader:
    """Приёмник потока arrow: нейтральный контракт потока -> факты каталога
    -> сверка по семействам -> план таблицы -> ход стратегий TransferRun с
    вставкой пачек через PgArrowSink, всё одной транзакцией соединения."""

    def __init__(  # noqa: PLR0913
        self,
        conn: psycopg.AsyncConnection[Any],
        table: PgTableRef,
        contract: Sequence[ColumnSpec],
        source_engine: Engine,
        inbound: TransferInbound,
        chunk_bytes: int,
        exact_floats: bool,
    ) -> None:
        self._conn = conn
        self._table = table
        self._stream = tuple(contract)
        self._source_engine = source_engine
        self._exact = source_engine is Engine.POSTGRES
        self._inbound = inbound
        self._chunk_bytes = chunk_bytes
        self._exact_floats = exact_floats
        self._facts = PgNeutralFacts(conn, table)
        self._declared = PgDeclaredTypes(conn)
        self._ipc = ArrowIpc()

    async def run(  # noqa: PLR0913
        self,
        schema_strategy: SchemaStrategyPlan,
        delete_strategy: DeleteStrategyApply,
        insert_strategy: InsertStrategyApply,
        unknown_types: UnknownTypeApply,
        rules: ColumnRules,
        create_table: CreateTemplate,
    ) -> TransferReport:
        matcher = SchemaMatcher(rules, self._exact)
        async with self._conn.transaction():
            declared = await self._declared.resolve(rules.column_types)
            exists = await self._facts.exists()
            facts: Sequence[ColumnSpec] = ()
            if exists:
                facts = await self._facts.columns()

            diff = matcher.diff(self._stream, facts, declared)
            spec = diff.table_spec()
            table = PgArrowTable(
                self._conn, self._table, self._source_engine, spec, create_table
            )
            reader = await self._ipc.open_in(self._inbound.raw(), self._chunk_bytes)
            sink = PgArrowSink(
                self._conn, self._table, spec.names(), reader, self._exact_floats
            )
            transfer = TransferRun(
                schema_strategy, delete_strategy, insert_strategy, unknown_types
            )

            return await transfer.run(
                self._table.text(), exists, diff.check(), table, sink, True
            )
