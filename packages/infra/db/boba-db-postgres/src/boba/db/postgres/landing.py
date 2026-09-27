"""Стороны postgres для загрузок по COPY между конкретными движками: контракт
колонок как их описал libpq, COPY csv, text или binary наружу,
таблица-приёмник по каталогу и билдеру, COPY внутрь, разбор типов
rules.column_types описанием select null::<тип> и реестр пар по движку
источника. Сверку типов и DDL-тип
каждой колонки решает пара в своём пакете.

Ошибки:
LandingError — декларация на колонку, которой нет в ответе; тип из
    rules.column_types или из контракта сервер приёмника не знает (нет на его
    версии, нет расширения); колонка без типа при fail_on_unknown; пара для
    движка источника не установлена.
PgDescribeError — стейтмент источника не описывается сервером.
psycopg.Error — сервер отклонил стейтмент или значение.
"""

from __future__ import annotations

import asyncio
import io
from collections.abc import AsyncIterator, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from importlib.metadata import entry_points
from typing import Any, ClassVar, Protocol, runtime_checkable

import psycopg
import psycopg.errors
from psycopg import pq, sql
from psycopg._typeinfo import TypeInfo, TypesRegistry
from psycopg.pq.abc import PGconn, PGresult
from pydantic import BaseModel, ConfigDict, Field

from boba.db.postgres.describe import (
    PgDescribe,
    PgDescribedColumn,
    PgTypedColumn,
    PgTypeText,
)
from boba.db.postgres.errors import PgDescribeError
from boba.db.postgres.query import PgQuery, PgQueryBuilder
from boba.db.postgres.trace import PgCommandReport, PgSessionTrace
from boba.toolkit.landing import (
    ColumnRules,
    CreateTemplate,
    DeleteOutcome,
    DeleteStrategyApply,
    Engine,
    InsertStrategyApply,
    LandingError,
    LandingInbound,
    LandingOutbound,
    LandingReport,
    LandingSink,
    LandingTable,
    SchemaHead,
    SchemaStrategyPlan,
    SyncWire,
    UnknownTypeApply,
)
from boba.toolkit.stream import Chunk

__all__ = [
    "PgCatalogColumn",
    "PgColumnDeclaration",
    "PgContract",
    "PgContracts",
    "PgCopyIn",
    "PgCopyLayout",
    "PgCopyOut",
    "PgCopyProtocol",
    "PgLandingTable",
    "PgPlannedColumn",
    "PgServerVersion",
    "PgSourceColumn",
    "PgTableFacts",
    "PgTablePlan",
    "PgTableRef",
    "PgTypeResolver",
    "PgTypedName",
    "PostgresLanding",
    "PostgresLandingFactory",
    "PostgresLandings",
    "Relkind",
]


@dataclass(frozen=True)
class PgTableRef:
    """Таблица-приёмник: схема и имя как идентификаторы."""

    schema: str
    name: str

    def ident(self) -> sql.Identifier:
        return sql.Identifier(self.schema, self.name)

    def text(self) -> str:
        return f"{self.schema}.{self.name}"


class PgSourceColumn(BaseModel):
    """Колонка контракта источника postgres: позиция и имя из RowDescription,
    OID и typmod как отдал сервер, текст типа как печатает format_type
    (пусто — тип вне встроенного реестра, у сервера есть только OID),
    nullable — по декларации LLM, серверу у выборки неизвестен."""

    model_config = ConfigDict(frozen=True)

    position: int
    name: str
    oid: int
    typmod: int
    type_text: str
    nullable: bool

    def known(self) -> str:
        """Что известно о типе для сообщений: текст или голый OID."""
        if self.type_text:
            return self.type_text

        return f"oid {self.oid}"

    def describe(self) -> str:
        parts = [self.known()]
        if not self.nullable:
            parts.append("not null")

        return " ".join(parts)


class PgColumnDeclaration(BaseModel):
    """Декларация LLM для колонки выборки: заданное перекрывает то, что отдал
    сервер, незаданное остаётся его."""

    name: str = Field(min_length=1, description="Имя колонки в ответе запроса")
    nullable: bool | None = Field(
        default=None,
        description="Допустим ли NULL; серверу у выборки неизвестен, по умолчанию да",
    )
    type_text: str | None = Field(
        default=None,
        description=(
            "Текст типа postgres, когда сервер отдал только OID (enum, "
            "составные, расширения): sales.mood, hstore, vector(3). Схему "
            "указывать явно."
        ),
    )


@dataclass(frozen=True)
class PgServerVersion:
    """Версия сервера числом server_version из стартового пакета соединения:
    160004 — 16.4, 90604 — 9.6.4. Мажорная часть — до 10 две цифры (9.6),
    дальше одна (16)."""

    number: int

    OLD_STYLE_BELOW: ClassVar[int] = 100000

    def major(self) -> str:
        if self.number < self.OLD_STYLE_BELOW:
            return f"{self.number // 10000}.{self.number // 100 % 100}"

        return str(self.number // 10000)

    def text(self) -> str:
        if self.number < self.OLD_STYLE_BELOW:
            return f"{self.major()}.{self.number % 100}"

        return f"{self.major()}.{self.number % 10000}"


class PgContract(BaseModel):
    """Контракт источника postgres: колонки выборки в порядке RowDescription,
    версия сервера и integer_datetimes — их сверяет пара, когда тела идут
    binary. Едет в кадре schema как есть и разбирается парой на приёмнике."""

    model_config = ConfigDict(frozen=True)

    columns: Sequence[PgSourceColumn]
    server_version: int
    integer_datetimes: bool

    def version(self) -> PgServerVersion:
        return PgServerVersion(self.server_version)

    def declared(self, declarations: Sequence[PgColumnDeclaration]) -> PgContract:
        by_name = {column.name: column for column in self.columns}
        for declaration in declarations:
            if declaration.name not in by_name:
                raise LandingError(
                    f"columns: the query has no column {declaration.name!r}; "
                    f"columns of the result: {', '.join(by_name)}"
                )

            by_name[declaration.name] = self._applied(
                by_name[declaration.name], declaration
            )

        merged: list[PgSourceColumn] = []
        for column in self.columns:
            merged.append(by_name[column.name])

        return self.model_copy(update={"columns": merged})

    def render(self, wire: SyncWire) -> str:
        lines = [
            f"streamed out copy {wire.value} from postgres {self.version().text()}, "
            f"{len(self.columns)} columns:"
        ]
        for column in self.columns:
            lines.append(f"  {column.name}: {column.describe()}")

        return "\n".join(lines)

    @staticmethod
    def _applied(
        column: PgSourceColumn, declaration: PgColumnDeclaration
    ) -> PgSourceColumn:
        nullable = column.nullable
        if declaration.nullable is not None:
            nullable = declaration.nullable

        type_text = column.type_text
        if declaration.type_text is not None:
            type_text = declaration.type_text

        return column.model_copy(update={"nullable": nullable, "type_text": type_text})


class PgContracts:
    """Контракт из колонок RowDescription: текст типа по встроенному реестру
    psycopg и typmod, у типов вне реестра — пусто."""

    def __init__(self, registry: TypesRegistry) -> None:
        self._registry = registry
        self._text = PgTypeText()

    def of(
        self,
        columns: Iterable[PgDescribedColumn],
        server_version: int,
        integer_datetimes: bool,
    ) -> PgContract:
        specs: list[PgSourceColumn] = []
        for column in columns:
            specs.append(
                PgSourceColumn(
                    position=column.position,
                    name=column.name,
                    oid=column.oid,
                    typmod=column.typmod,
                    type_text=self.type_text(column),
                    nullable=True,
                )
            )

        return PgContract(
            columns=specs,
            server_version=server_version,
            integer_datetimes=integer_datetimes,
        )

    def type_text(self, column: PgTypedColumn) -> str:
        info: TypeInfo | None = self._registry.get(column.oid)
        if info is None:
            return ""

        return self._text.render(info, column.oid, column.typmod)


class PgCopyLayout(StrEnum):
    """Раскладки COPY postgres: csv, text (tsv потока) и binary. Значения
    совпадают с SyncWire, фрагменты стейтментов COPY живут здесь же."""

    CSV = "csv"
    TSV = "tsv"
    BINARY = "binary"

    def wire(self) -> SyncWire:
        return SyncWire(self.value)

    def to_stdout(self) -> str:
        return f") to stdout (format {self._format()})"

    def from_stdin(self) -> str:
        return f"from stdin (format {self._format()})"

    def _format(self) -> str:
        if self is PgCopyLayout.TSV:
            return "text"

        return self.value


class PgCopyProtocol:
    """COPY TO STDOUT напрямую через libpq в рабочем потоке на соединении
    psycopg: PQsendQuery, PQgetResult со статусом COPY_OUT, цикл PQgetCopyData
    в блокирующем режиме с записью каждого сообщения в буферизованный файл,
    затем PQgetResult до конца. Сервер шлёт одно сообщение CopyData на
    строку, мельче libpq не отдаёт, поэтому цикл держится на уровне C без
    машины ожидания psycopg на каждую строку; цикл событий ждёт поток и
    соединение не трогает. Ошибка сервера посреди COPY — исключение psycopg
    из его результата; ошибка записи (обрыв трубы) — отмена запроса и
    дочитывание, чтобы соединение вернулось в чистое состояние."""

    def __init__(self, conn: psycopg.AsyncConnection[Any]) -> None:
        self._conn = conn

    async def run(self, statement: bytes, sink: io.BufferedIOBase) -> str:
        """Статус команды сервера (COPY n) после полной передачи."""
        return await asyncio.to_thread(self._run, statement, sink)

    def _run(self, statement: bytes, sink: io.BufferedIOBase) -> str:
        pgconn = self._conn.pgconn
        pgconn.send_query(statement)
        started = pgconn.get_result()
        if started is None:
            raise LandingError("copy to stdout: the server returned no result")

        if started.status != pq.ExecStatus.COPY_OUT:
            self._results(pgconn)
            raise self._failure_of([started], pq.ExecStatus(started.status).name)

        try:
            self._pump(pgconn, sink)
        except psycopg.OperationalError as exc:
            raise self._failure_of(self._results(pgconn), str(exc)) from exc
        except BaseException:
            self._cancel_and_drain(pgconn)
            raise

        return self._status_of(self._results(pgconn))

    @staticmethod
    def _pump(pgconn: PGconn, sink: io.BufferedIOBase) -> None:
        while True:
            nbytes, data = pgconn.get_copy_data(0)
            if nbytes < 0:
                break

            sink.write(data)

        sink.flush()

    @staticmethod
    def _results(pgconn: PGconn) -> list[PGresult]:
        results: list[PGresult] = []
        while (result := pgconn.get_result()) is not None:
            results.append(result)

        return results

    def _failure_of(self, results: Sequence[PGresult], fallback: str) -> Exception:
        for result in results:
            if result.status == pq.ExecStatus.FATAL_ERROR:
                return psycopg.errors.error_from_result(
                    result, encoding=self._conn.info.encoding
                )

        return LandingError(f"copy to stdout failed without a server error: {fallback}")

    def _status_of(self, results: Sequence[PGresult]) -> str:
        if len(results) != 1:
            raise LandingError(
                f"copy to stdout: expected one result after the data, got "
                f"{len(results)}"
            )

        result = results[0]
        if result.status != pq.ExecStatus.COMMAND_OK:
            raise self._failure_of(results, pq.ExecStatus(result.status).name)

        status = result.command_status
        if status is None:
            return ""

        return status.decode(self._conn.info.encoding)

    def _cancel_and_drain(self, pgconn: PGconn) -> None:
        pgconn.get_cancel().cancel()
        try:
            self._pump(pgconn, io.BytesIO())
        except psycopg.OperationalError:
            # ответ сервера на нашу же отмену: COPY снят, остались результаты
            self._results(pgconn)

            return

        self._results(pgconn)


class PgCopyOut:
    """Источник: колонки выборки от PgDescribe (без выполнения), кадр schema
    с контрактом, затем COPY (<select>) TO STDOUT в раскладке PgCopyLayout
    через PgCopyProtocol: строки копятся в буфере chunk_bytes и уходят в
    порт кадрами как есть."""

    INTEGER_DATETIMES: ClassVar[str] = "integer_datetimes"
    ON: ClassVar[str] = "on"

    def __init__(self, conn: psycopg.AsyncConnection[Any]) -> None:
        self._conn = conn
        self._describe = PgDescribe(conn)
        self._contracts = PgContracts(conn.adapters.types)
        self._trace = PgSessionTrace(conn)
        self._protocol = PgCopyProtocol(conn)

    async def contract(
        self, query: str, declarations: Sequence[PgColumnDeclaration]
    ) -> PgContract:
        return self.contract_of(await self.describe(query), declarations)

    async def describe(self, query: str) -> tuple[PgDescribedColumn, ...]:
        """Колонки выборки от PgDescribe одним описанием стейтмента."""
        columns: list[PgDescribedColumn] = []
        async for column in self._describe.columns(query):
            columns.append(column)

        return tuple(columns)

    def contract_of(
        self,
        columns: Sequence[PgDescribedColumn],
        declarations: Sequence[PgColumnDeclaration],
    ) -> PgContract:
        info = self._conn.info
        integer_datetimes = info.parameter_status(self.INTEGER_DATETIMES) == self.ON

        return self._contracts.of(
            columns, info.server_version, integer_datetimes
        ).declared(declarations)

    async def stream(
        self,
        query: str,
        layout: PgCopyLayout,
        contract: PgContract,
        chunk_bytes: int,
        out: LandingOutbound,
    ) -> PgCommandReport:
        await out.schema(
            SchemaHead(
                kind="schema",
                source_engine=Engine.POSTGRES,
                wire=layout.wire(),
                contract=contract.model_dump(mode="json"),
            )
        )
        statement = (
            PgQueryBuilder()
            .add("copy (")
            .raw_query(query)
            .add(layout.to_stdout())
            .build()
        )
        raw = out.writer()
        writer = io.BufferedWriter(raw, buffer_size=chunk_bytes)
        try:
            status = await self._protocol.run(
                statement.text.as_bytes(self._conn), writer
            )
        except BaseException:
            # закрытый raw гасит сброс остатка буфера при сборке мусора: в порт
            # после ошибки не должно уйти ни байта
            raw.close()
            raise

        return self._trace.report_status(
            contract.render(layout.wire()), statement.text.as_string(self._conn), status
        )


class Relkind(StrEnum):
    """Виды отношений pg_class, которые приёмник считает таблицей."""

    TABLE = "r"
    PARTITIONED = "p"


@dataclass(frozen=True)
class PgCatalogColumn(PgTypedColumn):
    """Колонка таблицы приёмника из pg_attribute: позиция (с нуля, в порядке
    attnum без удалённых), имя, OID типа (atttypid), typmod (atttypmod), OID
    таблицы (attrelid), attnum (с единицы), размер типа (attlen), not null
    (attnotnull) — то, чего у RowDescription нет."""

    position: int
    name: str
    oid: int
    typmod: int
    table_oid: int
    attnum: int
    size: int
    not_null: bool


class PgTableFacts:
    """Таблица по каталогу postgres: есть ли она (pg_class) и её колонки из
    pg_attribute."""

    def __init__(self, conn: psycopg.AsyncConnection[Any], table: PgTableRef) -> None:
        self._conn = conn
        self._table = table

    async def exists(self) -> bool:
        query = (
            PgQueryBuilder()
            .add(
                "select 1 from pg_catalog.pg_class c "
                "join pg_catalog.pg_namespace n on n.oid = c.relnamespace "
                "where n.nspname = %(schema)s and c.relname = %(table)s "
                "and c.relkind in (%(table_kind)s, %(partitioned_kind)s)",
                schema=self._table.schema,
                table=self._table.name,
                table_kind=Relkind.TABLE.value,
                partitioned_kind=Relkind.PARTITIONED.value,
            )
            .build()
        )
        async with self._conn.cursor() as cursor:
            await cursor.execute(query.text, query.params)
            row = await cursor.fetchone()

        return row is not None

    async def columns(self) -> Sequence[PgCatalogColumn]:
        query = (
            PgQueryBuilder()
            .add(
                "select a.attname, a.atttypid, a.atttypmod, a.attnotnull, a.attnum, "
                "a.attrelid, a.attlen "
                "from pg_catalog.pg_attribute a "
                "join pg_catalog.pg_class c on c.oid = a.attrelid "
                "join pg_catalog.pg_namespace n on n.oid = c.relnamespace "
                "where n.nspname = %(schema)s and c.relname = %(table)s "
                "and a.attnum > 0 and not a.attisdropped order by a.attnum",
                schema=self._table.schema,
                table=self._table.name,
            )
            .build()
        )
        columns: list[PgCatalogColumn] = []
        async with self._conn.cursor() as cursor:
            await cursor.execute(query.text, query.params)
            for position, row in enumerate(await cursor.fetchall()):
                columns.append(
                    PgCatalogColumn(
                        position=position,
                        name=str(row[0]),
                        oid=int(row[1]),
                        typmod=int(row[2]),
                        table_oid=int(row[5]),
                        attnum=int(row[4]),
                        size=int(row[6]),
                        not_null=bool(row[3]),
                    )
                )

        return tuple(columns)


class PgTypeResolver:
    """Типы из rules.column_types, разобранные самим приёмником: сервер
    описывает стейтмент select null::<тип> as <колонка> без выполнения, и
    текст типа LLM превращается в OID и typmod. Неизвестный серверу тип —
    ошибка до любого DDL."""

    def __init__(self, conn: psycopg.AsyncConnection[Any]) -> None:
        self._conn = conn
        self._describe = PgDescribe(conn)

    async def resolve(
        self, column_types: Mapping[str, str]
    ) -> dict[str, PgDescribedColumn]:
        if not column_types:
            return {}

        query = self._query(column_types)
        resolved: dict[str, PgDescribedColumn] = {}
        try:
            async for column in self._describe.columns(
                query.text.as_string(self._conn)
            ):
                resolved[column.name] = column
        except PgDescribeError as exc:
            raise LandingError(
                f"postgres does not accept the declared types {dict(column_types)}: "
                f"{exc}"
            ) from exc

        return resolved

    def _query(self, column_types: Mapping[str, str]) -> PgQuery:
        builder = PgQueryBuilder().add("select")
        for position, (name, text) in enumerate(column_types.items()):
            builder.when(position > 0, ",")
            builder.add("null::")
            builder.raw_query(text)
            builder.add("as {name}", name=sql.Identifier(name))

        return builder.build()


@dataclass(frozen=True)
class PgPlannedColumn:
    """Колонка для create table и COPY: имя в таблице, текст типа (пусто —
    типа нет, решает стратегия неизвестных типов), nullable и что известно
    о типе источника для сообщения об отказе."""

    name: str
    type_text: str
    nullable: bool
    known: str


@dataclass(frozen=True)
class PgTablePlan:
    """Колонки таблицы-приёмника в порядке полей потока."""

    columns: Sequence[PgPlannedColumn]

    def names(self) -> list[str]:
        return [column.name for column in self.columns]


class PgLandingTable(LandingTable):
    """Реализация LandingTable для postgres: каталог через PgTableFacts, DDL и
    delete через PgQueryBuilder на одном соединении, условие where от
    вызова — raw_query. create table — по шаблону вызывающего: схема, имя и
    колонки подставляются экранированными psycopg фрагментами."""

    BACKUP_STAMP: ClassVar[str] = "%Y%m%d_%H%M%S_%f"
    VARCHAR: ClassVar[str] = "varchar"
    CREATE_TABLE: ClassVar[str] = "create table {schema_name}.{table_name} ({columns})"
    """Шаблон без особенностей таблицы: дефолт фасада pg_sync_in."""

    def __init__(
        self,
        conn: psycopg.AsyncConnection[Any],
        table: PgTableRef,
        plan: PgTablePlan,
        template: CreateTemplate,
    ) -> None:
        self._conn = conn
        self._table = table
        self._plan = plan
        self._template = template
        self._facts = PgTableFacts(conn, table)

    async def exists(self) -> bool:
        return await self._facts.exists()

    async def create(self, unknown_as_varchar: bool) -> str:
        query = self._create_query(unknown_as_varchar)
        try:
            await self._execute(query)
        except psycopg.errors.UndefinedObject as exc:
            raise LandingError(
                f"create table {self._table.text()}: the target server has no such "
                f"type: {exc.diag.message_primary}; declare another type for the "
                f"column in rules.column_types, for example text; statement: "
                f"{query.text.as_string(self._conn)}"
            ) from exc

        return query.text.as_string(self._conn)

    def _create_query(self, unknown_as_varchar: bool) -> PgQuery:
        rendered = self._template.render(
            schema_name=sql.Identifier(self._table.schema).as_string(self._conn),
            table_name=sql.Identifier(self._table.name).as_string(self._conn),
            columns=self._columns_fragment(unknown_as_varchar),
        )

        return PgQueryBuilder().raw_query(rendered).build()

    def _columns_fragment(self, unknown_as_varchar: bool) -> str:
        builder = PgQueryBuilder()
        for position, column in enumerate(self._plan.columns):
            builder.when(position > 0, ",")
            builder.add("{name}", name=sql.Identifier(column.name))
            builder.raw_query(self._type_text(column, unknown_as_varchar))
            builder.when(not column.nullable, "not null")

        return builder.build().text.as_string(self._conn)

    def _type_text(self, column: PgPlannedColumn, unknown_as_varchar: bool) -> str:
        if column.type_text:
            return column.type_text

        if unknown_as_varchar:
            return self.VARCHAR

        raise LandingError(
            f"column {column.name}: the target cannot map the source type "
            f"{column.known}; declare the target type in "
            f'rules.column_types["{column.name}"] or take unknown_types '
            f"fallback_as_varchar"
        )

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

        return DeleteOutcome(rows=0, statement=query.text.as_string(self._conn))

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

        return DeleteOutcome(rows=rows, statement=query.text.as_string(self._conn))


class PgCopyIn(LandingSink):
    """Реализация LandingSink для postgres: тела кадров уходят в COPY таблицы
    как есть, csv или text по раскладке потока, ничего не перекодируется."""

    def __init__(
        self,
        conn: psycopg.AsyncConnection[Any],
        table: PgTableRef,
        names: Sequence[str],
        layout: PgCopyLayout,
        bodies: AsyncIterator[Chunk],
    ) -> None:
        self._conn = conn
        self._table = table
        self._names = tuple(names)
        self._layout = layout
        self._bodies = bodies

    async def load(self) -> int:
        query = self._query()
        async with self._conn.cursor() as cursor:
            async with cursor.copy(query.text) as copy:
                async for body in self._bodies:
                    await copy.write(body)

            rows = cursor.rowcount

        rows = max(rows, 0)

        return rows

    async def discard(self) -> int:
        async for _ in self._bodies:
            pass

        return 0

    def _query(self) -> PgQuery:
        names: list[sql.Identifier] = []
        for name in self._names:
            names.append(sql.Identifier(name))

        return (
            PgQueryBuilder(table=self._table.ident())
            .add("copy {table} ({columns})", columns=sql.SQL(", ").join(names))
            .add(self._layout.from_stdin())
            .build()
        )


@dataclass(frozen=True)
class PgTypedName:
    """Колонка для проверки binary: имя, OID и что известно о типе."""

    name: str
    oid: int
    known: str


class PostgresLanding(Protocol):
    """Пара «движок источника → postgres»: разбирает контракт своего
    источника, сверяет его с таблицей, планирует DDL и ведёт стратегии.
    Реализация в пакете пары, создаётся фабрикой из реестра."""

    async def run(  # noqa: PLR0913
        self,
        schema_strategy: SchemaStrategyPlan,
        delete_strategy: DeleteStrategyApply,
        insert_strategy: InsertStrategyApply,
        unknown_types: UnknownTypeApply,
        rules: ColumnRules,
        create_table: CreateTemplate,
    ) -> LandingReport: ...


@runtime_checkable
class PostgresLandingFactory(Protocol):
    """Конструктор пары: класс с таким __init__ — соединение приёмника,
    таблица, кадр схемы, поток тел."""

    def __call__(
        self,
        conn: psycopg.AsyncConnection[Any],
        table: PgTableRef,
        head: SchemaHead,
        feed: LandingInbound,
    ) -> PostgresLanding: ...


class PostgresLandings:
    """Реестр пар в postgres по entry points группы boba.landing.postgres:
    имя записи — движок источника, значение — класс пары."""

    GROUP: ClassVar[str] = "boba.landing.postgres"

    def __init__(self, factories: Mapping[Engine, PostgresLandingFactory]) -> None:
        self._factories = dict(factories)

    @classmethod
    def discover(cls) -> PostgresLandings:
        factories: dict[Engine, PostgresLandingFactory] = {}
        for entry in entry_points(group=cls.GROUP):
            try:
                engine = Engine(entry.name)
            except ValueError as exc:
                raise LandingError(
                    f"entry point {entry.name!r} of group {cls.GROUP!r} "
                    f"({entry.value}): the name must be a source engine, one of "
                    f"{[member.value for member in Engine]}"
                ) from exc

            loaded = entry.load()
            if not isinstance(loaded, PostgresLandingFactory):
                raise LandingError(
                    f"entry point {entry.name!r} of group {cls.GROUP!r} "
                    f"({entry.value}): expected a landing class, got {loaded!r}"
                )

            factories[engine] = loaded

        return cls(factories)

    def pair(self, engine: Engine) -> PostgresLandingFactory:
        factory = self._factories.get(engine)
        if factory is None:
            installed = ", ".join(sorted(member.value for member in self._factories))
            raise LandingError(
                f"no landing from {engine.value} into postgres is installed "
                f"(entry point group {self.GROUP}); installed: {installed or 'none'}"
            )

        return factory
