"""Стороны postgres для загрузок по COPY между конкретными движками: контракт
колонок как их описал libpq, COPY csv, text или binary наружу,
таблица-приёмник по каталогу и билдеру, COPY внутрь, разбор типов
rules.column_types описанием select null::<тип> и реестр пар по движку
источника. Сверку типов и DDL-тип
каждой колонки решает пара в своём пакете.

Ошибки:
TransferError — декларация на колонку, которой нет в ответе; тип из
    rules.column_types или из контракта сервер приёмника не знает (нет на его
    версии, нет расширения); колонка без типа при fail_on_unknown; пара для
    движка источника не установлена.
PgDescribeError — стейтмент источника не описывается сервером.
psycopg.Error — сервер отклонил стейтмент или значение.
"""

from __future__ import annotations

import asyncio
import io
from collections.abc import AsyncIterator, Iterable, Iterator, Mapping, Sequence
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
    PgTypmod,
)
from boba.db.postgres.errors import PgDescribeError
from boba.db.postgres.query import PgQuery, PgQueryBuilder
from boba.db.postgres.trace import PgCommandReport, PgSessionTrace
from boba.toolkit.contract import ContractColumn, ContractTable
from boba.toolkit.stream import Chunk
from boba.toolkit.transfer import (
    ColumnCheck,
    ColumnIssue,
    ColumnRules,
    ColumnVerdict,
    CommandJournal,
    CommandKind,
    CreateTemplate,
    DeleteOutcome,
    DeleteStrategyApply,
    Engine,
    InsertStrategyApply,
    PlannedColumn,
    SchemaCheck,
    SchemaHead,
    SchemaStrategyPlan,
    StreamWire,
    TemplateVar,
    TemplateVars,
    TransferError,
    TransferInbound,
    TransferOutbound,
    TransferReport,
    TransferSink,
    TransferTable,
    UnknownTypeApply,
    Verdict,
)

__all__ = [
    "PgCatalogColumn",
    "PgColumnDeclaration",
    "PgContract",
    "PgContracts",
    "PgCopyIn",
    "PgCopyLayout",
    "PgCopyOut",
    "PgCopyProtocol",
    "PgField",
    "PgMatch",
    "PgMatched",
    "PgMatcher",
    "PgPlannedColumn",
    "PgServerVersion",
    "PgSourceColumn",
    "PgStreamColumn",
    "PgTableFacts",
    "PgTablePlan",
    "PgTableRef",
    "PgTransfer",
    "PgTransferFactory",
    "PgTransferTable",
    "PgTransfers",
    "PgTypeResolver",
    "PgTypeRules",
    "PgTypedName",
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

    def row(self) -> dict[str, str]:
        not_null = ""
        if not self.nullable:
            not_null = ContractTable.NOT_NULL_MARK

        return {
            ContractColumn.COLUMN: self.name,
            ContractColumn.TYPE: self.known(),
            ContractColumn.NOT_NULL: not_null,
        }


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
                raise TransferError(
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

    def caption(self, wire: StreamWire) -> str:
        return (
            f"streamed out copy {wire.value} from postgres {self.version().text()}, "
            f"{len(self.columns)} columns"
        )

    def rows(self) -> list[dict[str, str]]:
        """Таблица контракта для отчёта: имя, тип и not null по колонке."""
        rows: list[dict[str, str]] = []
        for column in self.columns:
            rows.append(column.row())

        return rows

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
    совпадают со StreamWire, фрагменты стейтментов COPY живут здесь же."""

    CSV = "csv"
    TSV = "tsv"
    BINARY = "binary"

    def wire(self) -> StreamWire:
        return StreamWire(self.value)

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
            raise TransferError("copy to stdout: the server returned no result")

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

        return TransferError(
            f"copy to stdout failed without a server error: {fallback}"
        )

    def _status_of(self, results: Sequence[PGresult]) -> str:
        if len(results) != 1:
            raise TransferError(
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
    с контрактом, затем COPY (<select>) TO STDOUT в формате PgCopyLayout
    через PgCopyProtocol: строки копятся в буфере chunk_bytes и уходят в
    порт кадрами как есть."""

    INTEGER_DATETIMES: ClassVar[str] = "integer_datetimes"
    ON: ClassVar[str] = "on"

    def __init__(
        self, conn: psycopg.AsyncConnection[Any], journal: CommandJournal
    ) -> None:
        self._conn = conn
        self._journal = journal
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
        with self._journal.command(query, CommandKind.LOOKUP) as running:
            async for column in self._describe.columns(query):
                columns.append(column)

            running.status = f"{len(columns)} columns described"

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
        out: TransferOutbound,
    ) -> PgCommandReport:
        self._journal.columns(contract.rows())
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
        text = statement.text.as_string(self._conn)
        try:
            with self._journal.command(text, CommandKind.ACTION) as running:
                status = await self._protocol.run(
                    statement.text.as_bytes(self._conn), writer
                )
                running.status = status
        except BaseException:
            # закрытый raw гасит сброс остатка буфера при сборке мусора: в порт
            # после ошибки не должно уйти ни байта
            raw.close()
            raise

        return self._trace.report_status(
            contract.caption(layout.wire()),
            statement.text.as_string(self._conn),
            status,
            columns=contract.rows(),
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
    pg_attribute; запросы к каталогу пишутся в журнал вызова справкой."""

    def __init__(
        self,
        conn: psycopg.AsyncConnection[Any],
        table: PgTableRef,
        journal: CommandJournal,
    ) -> None:
        self._conn = conn
        self._table = table
        self._journal = journal

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
        text = query.text.as_string(self._conn)
        with self._journal.command(text, CommandKind.LOOKUP) as running:
            async with self._conn.cursor() as cursor:
                await cursor.execute(query.text, query.params)
                row = await cursor.fetchone()

            running.status = f"exists: {row is not None}"

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
        text = query.text.as_string(self._conn)
        with self._journal.command(text, CommandKind.LOOKUP) as running:
            async with self._conn.cursor() as cursor:
                await cursor.execute(query.text, query.params)
                rows = await cursor.fetchall()

            running.status = f"{len(rows)} columns"

        for position, row in enumerate(rows):
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

    def __init__(
        self, conn: psycopg.AsyncConnection[Any], journal: CommandJournal
    ) -> None:
        self._conn = conn
        self._describe = PgDescribe(conn)
        self._journal = journal

    async def resolve(
        self, column_types: Mapping[str, str]
    ) -> dict[str, PgDescribedColumn]:
        if not column_types:
            return {}

        query = self._query(column_types)
        text = query.text.as_string(self._conn)
        resolved: dict[str, PgDescribedColumn] = {}
        try:
            with self._journal.command(text, CommandKind.LOOKUP) as running:
                async for column in self._describe.columns(text):
                    resolved[column.name] = column

                running.status = f"{len(resolved)} types described"
        except PgDescribeError as exc:
            raise TransferError(
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
    """Колонка для create table и COPY: имя в таблице, поле потока, текст
    типа (пусто — типа нет, решает стратегия неизвестных типов), nullable,
    что известно о типе источника и объявлен ли тип правилами вызова."""

    name: str
    field: str
    type_text: str
    nullable: bool
    known: str
    declared: bool

    def planned(self) -> PlannedColumn:
        return PlannedColumn(
            name=self.name,
            field=self.field,
            type=self.type_text,
            nullable=self.nullable,
            source_type=self.known,
            notes=tuple(self._notes()),
        )

    def _notes(self) -> Iterator[str]:
        if self.field != self.name:
            yield f"renamed from {self.field}"

        if self.declared:
            yield "type from rules.column_types"

        if not self.type_text:
            yield "no target type for the source type: unknown_types decides"


@dataclass(frozen=True)
class PgTablePlan:
    """Колонки таблицы-приёмника в порядке полей потока."""

    columns: Sequence[PgPlannedColumn]

    def names(self) -> list[str]:
        return [column.name for column in self.columns]

    def planned(self) -> list[PlannedColumn]:
        planned: list[PlannedColumn] = []
        for column in self.columns:
            planned.append(column.planned())

        return planned


class PgTransferTable(TransferTable):
    """Реализация TransferTable для postgres: каталог через PgTableFacts, DDL и
    delete через PgQueryBuilder на одном соединении, условие where от
    вызова — raw_query. create table — по шаблону вызывающего: схема, имя и
    колонки подставляются экранированными psycopg фрагментами."""

    BACKUP_STAMP: ClassVar[str] = "%Y%m%d_%H%M%S_%f"
    VARCHAR: ClassVar[str] = "varchar"
    CREATE_TABLE: ClassVar[str] = "create table {schema_name}.{table_name} ({columns})"
    TEMPLATE_VARS: ClassVar[TemplateVars] = TemplateVars(
        offered=(TemplateVar.SCHEMA_NAME, TemplateVar.TABLE_NAME, TemplateVar.COLUMNS)
    )
    """Переменные, которые приёмник postgres подставляет в шаблон."""
    """Шаблон без особенностей таблицы: дефолт фасада pg_stream_in."""

    def __init__(
        self,
        conn: psycopg.AsyncConnection[Any],
        table: PgTableRef,
        plan: PgTablePlan,
        template: CreateTemplate,
        journal: CommandJournal,
    ) -> None:
        self._conn = conn
        self._table = table
        self._plan = plan
        self._template = template
        self._journal = journal
        self._facts = PgTableFacts(conn, table, journal)

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

        raise TransferError(
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

        return DeleteOutcome(effect="removed by truncate")

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

    async def _execute(self, query: PgQuery) -> int:
        """Команда в журнале вызова: статус сервера; ответ — rowcount."""
        text = query.text.as_string(self._conn)
        with self._journal.command(text, CommandKind.ACTION) as running:
            async with self._conn.cursor() as cursor:
                await cursor.execute(query.text, query.params)
                rows = cursor.rowcount
                status = cursor.statusmessage

            if status is None:
                status = "done"

            running.status = status

        return max(rows, 0)

    async def _deleted(self, query: PgQuery) -> DeleteOutcome:
        rows = await self._execute(query)

        return DeleteOutcome(effect=f"{rows} rows deleted")


class PgCopyIn(TransferSink):
    """Реализация TransferSink для postgres: тела кадров уходят в COPY таблицы
    как есть, в формате потока (csv, text или binary), ничего не перекодируется."""

    def __init__(  # noqa: PLR0913 — ширина подписи задана контрактом
        self,
        conn: psycopg.AsyncConnection[Any],
        table: PgTableRef,
        names: Sequence[str],
        layout: PgCopyLayout,
        bodies: AsyncIterator[Chunk],
        journal: CommandJournal,
    ) -> None:
        self._conn = conn
        self._table = table
        self._names = tuple(names)
        self._layout = layout
        self._bodies = bodies
        self._journal = journal

    async def load(self) -> int:
        """COPY целиком — одна команда журнала: тела идут мимо него."""
        query = self._query()
        text = query.text.as_string(self._conn)
        with self._journal.command(text, CommandKind.ACTION) as running:
            async with self._conn.cursor() as cursor:
                async with cursor.copy(query.text) as copy:
                    async for body in self._bodies:
                        await copy.write(body)

                rows = max(cursor.rowcount, 0)

            running.status = f"COPY {rows}"

        return rows

    def method(self) -> str:
        """Строки идут прямо в таблицу в транзакции вызова: пояснять нечего."""
        return ""

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


@dataclass(frozen=True)
class PgField:
    """Поле потока, как его выразила пара в терминах postgres: имя поля,
    OID и typmod из контракта postgres (0 и -1 у другого движка), текст
    типа postgres (у другого движка — перевод парой, пусто — типа нет),
    nullable и то, что известно о типе источника, для сообщений."""

    name: str
    oid: int
    typmod: int
    type_text: str
    nullable: bool
    known: str


@dataclass(frozen=True)
class PgStreamColumn:
    """Колонка потока глазами приёмника:
    - имя в таблице после rename
    - OID и typmod из контракта или из разобранного rules.column_types
    - текст типа для DDL, что известно о типе для сообщений
    - nullable
    - resolved: OID и typmod получены разбором текста типа на сервере
      приёмника (rules.column_types или type_text источника), то есть в его
      системе OID; иначе OID из контракта источника, и вне встроенного
      реестра он ничего не значит
    """

    name: str
    oid: int
    typmod: int
    type_text: str
    known: str
    nullable: bool
    resolved: bool


class PgTypeRules:
    """Совместимость колонки потока с колонкой таблицы в терминах postgres:
        - OID совпадает
        - typmod приёмника не уже (
            numeric — scale и целые разряды,
            varchar и bit — длина,
            timestamp и time — точность)
        - nullable потока при not null таблицы — ошибка.
    OID встроенных типов одинаковы на всех серверах; у пользовательских
    типов OID сравним только после разбора имени на приёмнике (resolved),
    без имени сверить нельзя — предупреждение."""

    def __init__(self, registry: TypesRegistry) -> None:
        self._registry = registry
        self._text = PgTypeText()
        self._typmod = PgTypmod()

    def compare(self, stream: PgStreamColumn, table: PgCatalogColumn) -> ColumnVerdict:
        kinds = self._kinds(stream, table)
        if kinds.level is Verdict.ERROR:
            return kinds

        nullable = self._nullable(stream, table)
        if nullable.level is Verdict.ERROR:
            return nullable

        if kinds.level is Verdict.WARNING:
            return kinds

        return nullable

    def _kinds(self, stream: PgStreamColumn, table: PgCatalogColumn) -> ColumnVerdict:
        info: TypeInfo | None = self._registry.get(stream.oid)
        table_text = self.table_text(table)
        if info is None and not stream.resolved:
            return self._unnamed(stream, table_text)

        if stream.oid != table.oid:
            return ColumnVerdict(
                Verdict.ERROR,
                f"type differs: stream {stream.known}, table {table_text}",
            )

        if stream.typmod == table.typmod:
            return ColumnVerdict(Verdict.OK, "ok")

        if info is None:
            return ColumnVerdict(
                Verdict.WARNING,
                f"type modifier differs: stream {stream.known}, table {table_text}",
            )

        return self._modifiers(info.name, stream, table, table_text)

    def _modifiers(
        self,
        type_name: str,
        stream: PgStreamColumn,
        table: PgCatalogColumn,
        table_text: str,
    ) -> ColumnVerdict:
        if self._typmod.is_numeric(type_name):
            return self._numeric(stream, table, table_text)

        if self._typmod.is_sized(type_name):
            return self._sized(type_name, stream, table, table_text)

        if self._typmod.is_precise(type_name):
            return self._precise(stream, table, table_text)

        return ColumnVerdict(
            Verdict.WARNING,
            f"type modifier differs: stream {stream.known}, table {table_text}",
        )

    def _unnamed(self, stream: PgStreamColumn, table_text: str) -> ColumnVerdict:
        """OID пользовательского типа источника на приёмнике не значит ничего:
        без имени типа сверять нечего."""
        return ColumnVerdict(
            Verdict.WARNING,
            f"type cannot be verified, the source named no type: stream "
            f"{stream.known}, table {table_text}",
        )

    def _numeric(
        self, stream: PgStreamColumn, table: PgCatalogColumn, table_text: str
    ) -> ColumnVerdict:
        if self._typmod.unbounded(stream.typmod):
            return ColumnVerdict(
                Verdict.WARNING,
                f"stream {stream.known} has no declared precision, table "
                f"{table_text} may truncate",
            )

        if self._typmod.unbounded(table.typmod):
            return ColumnVerdict(Verdict.OK, "ok")

        stream_digits = self._typmod.digits(stream.typmod)
        table_digits = self._typmod.digits(table.typmod)
        if stream_digits.scale > table_digits.scale:
            return ColumnVerdict(
                Verdict.ERROR,
                f"table {table_text} truncates the scale of stream {stream.known}",
            )

        integer_stream = stream_digits.precision - stream_digits.scale
        integer_table = table_digits.precision - table_digits.scale
        if integer_stream > integer_table:
            return ColumnVerdict(
                Verdict.ERROR,
                f"table {table_text} holds fewer integer digits than stream "
                f"{stream.known}",
            )

        return ColumnVerdict(
            Verdict.WARNING, f"table {table_text} is wider than stream {stream.known}"
        )

    def _sized(
        self,
        type_name: str,
        stream: PgStreamColumn,
        table: PgCatalogColumn,
        table_text: str,
    ) -> ColumnVerdict:
        stream_length = self._typmod.length(type_name, stream.typmod)
        table_length = self._typmod.length(type_name, table.typmod)
        if stream_length == 0:
            return ColumnVerdict(
                Verdict.WARNING,
                f"table {table_text} limits the length, the stream {stream.known} "
                f"has no limit",
            )

        if table_length == 0:
            return ColumnVerdict(Verdict.OK, "ok")

        if stream_length > table_length:
            return ColumnVerdict(
                Verdict.ERROR,
                f"table {table_text} is shorter than stream {stream.known}",
            )

        return ColumnVerdict(
            Verdict.WARNING, f"table {table_text} is longer than stream {stream.known}"
        )

    def _precise(
        self, stream: PgStreamColumn, table: PgCatalogColumn, table_text: str
    ) -> ColumnVerdict:
        stream_precision = self._typmod.precision(stream.typmod)
        table_precision = self._typmod.precision(table.typmod)
        if stream_precision > table_precision:
            return ColumnVerdict(
                Verdict.ERROR,
                f"table {table_text} is coarser than stream {stream.known}",
            )

        if stream_precision < table_precision:
            return ColumnVerdict(
                Verdict.WARNING,
                f"table {table_text} is finer than stream {stream.known}",
            )

        return ColumnVerdict(Verdict.OK, "ok")

    def _nullable(
        self, stream: PgStreamColumn, table: PgCatalogColumn
    ) -> ColumnVerdict:
        if stream.nullable and table.not_null:
            return ColumnVerdict(
                Verdict.ERROR,
                "stream is nullable, table column is not null",
                ColumnIssue.NULLABLE_INTO_NOT_NULL,
            )

        if not stream.nullable and not table.not_null:
            return ColumnVerdict(
                Verdict.WARNING,
                "stream is not null, table column is nullable",
                ColumnIssue.NOT_NULL_INTO_NULLABLE,
            )

        return ColumnVerdict(Verdict.OK, "ok")

    def table_text(self, table: PgCatalogColumn) -> str:
        """Тип колонки таблицы текстом format_type, без имени — голый OID."""
        info: TypeInfo | None = self._registry.get(table.oid)
        if info is None:
            return f"oid {table.oid}"

        return self._text.render(info, table.oid, table.typmod)


@dataclass(frozen=True)
class PgMatch:
    """Колонка приёмника с тем, что о ней известно с двух сторон: поток
    (после rename и column_types) и таблица; любая сторона может
    отсутствовать — в этом и состоит сверка."""

    name: str
    stream: PgStreamColumn | None
    table: PgCatalogColumn | None

    def check(self, rules: PgTypeRules) -> ColumnCheck:
        """Сверка колонки с типами обеих сторон для отчёта."""
        stream = ""
        if self.stream is not None:
            stream = self.stream.known

        table = ""
        if self.table is not None:
            table = rules.table_text(self.table)

        return ColumnCheck(
            name=self.name, stream=stream, table=table, verdict=self.verdict(rules)
        )

    def verdict(self, rules: PgTypeRules) -> ColumnVerdict:
        if self.stream is None:
            return ColumnVerdict(
                Verdict.ERROR,
                "in the table but not in the stream",
                ColumnIssue.NOT_IN_STREAM,
            )

        if self.table is None:
            return ColumnVerdict(
                Verdict.ERROR,
                "in the stream but not in the table",
                ColumnIssue.NOT_IN_TABLE,
            )

        return rules.compare(self.stream, self.table)


@dataclass(frozen=True)
class PgMatched:
    """Итог сверки: план таблицы по потоку, колонки потока глазами приёмника
    и вердикты для стратегии."""

    plan: PgTablePlan
    stream: Sequence[PgStreamColumn]
    check: SchemaCheck


class PgMatcher:
    """Сведение полей потока, правил приёмника и колонок таблицы по имени
    колонки приёмника: rename переименовывает поле потока, column_types
    подменяет его тип разобранным сервером, дальше каждая колонка получает
    вердикт, а поток — план таблицы в своём порядке. Поля потока пара
    выражает в терминах postgres (PgField): у источника postgres — OID и
    typmod контракта, у другого движка — текст типа postgres, который
    приёмник разбирает у себя."""

    def __init__(self, registry: TypesRegistry, rules: ColumnRules) -> None:
        self._registry = registry
        self._rules = rules
        self._type_rules = PgTypeRules(registry)

    def resolve_text_types(self, fields: Sequence[PgField]) -> dict[str, str]:
        """Merge информации о колонках,
        которая досталась нам от источника и была передана сюда, на приемник

        Вовзращает словарь: {"колонка приёмника": "текст типа"}
        Например:
        ```python
        {
           "amount": "numeric(20,6)",
            "v": "pump_transfer.mood",
        }
        ```

        Приемник резолвит типы по их OID номерам
        источник отправляет прямо oid номера, а не текст типов

        Это точно передать тип данных для "стандартных" типов
        Потому что эти oid номера зашиты в исходниках postgres и не меняются
        от версии к версии postgres

        Однако кастомные типы данных, либо типы специфичные для определенных
        версий postgres могут быть не найдены в приемнике, просто потому
        что версия postgres их не поддерживает

        В таком случае полезной может оказаться информация из self._rules.column_types
        которую заполняет внешняя сторона вызова и передает туда тип
        переопределяя пришедший, что бы можно было передать поток
        даже с потерей точности в типах. Где это может быть полезно?
        К примеру ваша версия postgres не поддерживает json,
        но ведь можно записать varchar? Внешняя сторона проставляет явно
        что тип <column name>: <column type>
        """
        # всё, что LLM задал у приёмника
        texts: dict[str, str] = dict(self._rules.column_types)
        # колонки потока с именами после rename
        for name, column in self._renamed(fields):
            # column_types важнее
            if name in texts:
                continue

            # имени нет, только OID — разбирать нечего
            if not column.type_text:
                continue

            # встроенный тип: OID общий, разбор не нужен
            if self._registry.get(column.oid) is not None:
                continue

            # пользовательский тип с именем от источника
            texts[name] = column.type_text

        # словарь {"колонка приёмника": "текст типа"}
        # Например: {
        #   "amount": "numeric(20,6)",
        #   "v": "pump_transfer.mood",
        # }.
        return texts

    def match(
        self,
        fields: Sequence[PgField],
        catalog: Sequence[PgCatalogColumn],
        resolved: Mapping[str, PgDescribedColumn],
    ) -> PgMatched:
        renamed = self._renamed(fields)
        targets = {column.name: column for column in catalog}
        matches: list[PgMatch] = []
        planned: list[PgPlannedColumn] = []
        seen: set[str] = set()
        for name, column in renamed:
            seen.add(name)
            stream = self._stream_column(name, column, resolved.get(name))
            matches.append(PgMatch(name=name, stream=stream, table=targets.get(name)))
            planned.append(
                PgPlannedColumn(
                    name=name,
                    field=column.name,
                    type_text=stream.type_text,
                    nullable=stream.nullable,
                    known=stream.known,
                    declared=name in self._rules.column_types,
                )
            )

        for column in catalog:
            if column.name in seen:
                continue

            seen.add(column.name)
            matches.append(PgMatch(name=column.name, stream=None, table=column))

        for name in self._rules.column_types:
            if name in seen:
                continue

            raise TransferError(
                f"column_types: neither the stream nor the table has a column "
                f"{name!r}; columns: {', '.join(sorted(seen))}"
            )

        streamed: list[PgStreamColumn] = []
        for match in matches:
            if match.stream is None:
                continue

            streamed.append(match.stream)

        return PgMatched(
            plan=PgTablePlan(columns=tuple(planned)),
            stream=tuple(streamed),
            check=self._check(matches),
        )

    def _stream_column(
        self,
        name: str,
        column: PgField,
        declared: PgDescribedColumn | None,
    ) -> PgStreamColumn:
        if declared is None:
            return PgStreamColumn(
                name=name,
                oid=column.oid,
                typmod=column.typmod,
                type_text=column.type_text,
                known=column.known,
                nullable=column.nullable,
                resolved=False,
            )

        text = self._rules.column_types.get(name)
        if text is None:
            text = column.type_text

        return PgStreamColumn(
            name=name,
            oid=declared.oid,
            typmod=declared.typmod,
            type_text=text,
            known=text,
            nullable=column.nullable,
            resolved=True,
        )

    def _renamed(self, fields: Sequence[PgField]) -> list[tuple[str, PgField]]:
        by_source: dict[str, str] = {}
        for name, rename_from in self._rules.rename_columns.items():
            by_source[rename_from] = name

        names = {column.name for column in fields}
        for source_name in by_source:
            if source_name not in names:
                raise TransferError(
                    f"rename_columns: the stream has no field {source_name!r}; "
                    f"stream fields: {', '.join(sorted(names))}"
                )

        renamed: list[tuple[str, PgField]] = []
        for column in fields:
            renamed.append((by_source.get(column.name, column.name), column))

        return renamed

    def _check(self, matches: Sequence[PgMatch]) -> SchemaCheck:
        columns: list[ColumnCheck] = []
        for match in matches:
            columns.append(match.check(self._type_rules))

        return SchemaCheck(columns=tuple(columns))


class PgTransfer(Protocol):
    """Пара «движок источника → postgres»: разбирает контракт своего
    источника, сверяет его с таблицей, планирует DDL и ведёт стратегии.
    Реализация в пакете пары, создаётся фабрикой из реестра."""

    async def run(  # noqa: PLR0913 — фикстуры теста
        self,
        schema_strategy: SchemaStrategyPlan,
        delete_strategy: DeleteStrategyApply,
        insert_strategy: InsertStrategyApply,
        unknown_types: UnknownTypeApply,
        rules: ColumnRules,
        create_table: CreateTemplate,
    ) -> TransferReport: ...


@runtime_checkable
class PgTransferFactory(Protocol):
    """Конструктор пары: класс с таким __init__ — соединение приёмника,
    таблица, кадр схемы, поток тел, журнал команд вызова."""

    def __call__(
        self,
        conn: psycopg.AsyncConnection[Any],
        table: PgTableRef,
        head: SchemaHead,
        feed: TransferInbound,
        journal: CommandJournal,
    ) -> PgTransfer: ...


class PgTransfers:
    """Реестр пар в postgres по entry points группы boba.transfer.postgres:
    имя записи — движок источника, значение — класс пары."""

    GROUP: ClassVar[str] = "boba.transfer.postgres"

    def __init__(self, factories: Mapping[Engine, PgTransferFactory]) -> None:
        self._factories = dict(factories)

    @classmethod
    def discover(cls) -> PgTransfers:
        factories: dict[Engine, PgTransferFactory] = {}
        for entry in entry_points(group=cls.GROUP):
            try:
                engine = Engine(entry.name)
            except ValueError as exc:
                raise TransferError(
                    f"entry point {entry.name!r} of group {cls.GROUP!r} "
                    f"({entry.value}): the name must be a source engine, one of "
                    f"{[member.value for member in Engine]}"
                ) from exc

            loaded = entry.load()
            if not isinstance(loaded, PgTransferFactory):
                raise TransferError(
                    f"entry point {entry.name!r} of group {cls.GROUP!r} "
                    f"({entry.value}): expected a transfer class, got {loaded!r}"
                )

            factories[engine] = loaded

        return cls(factories)

    def pair(self, engine: Engine) -> PgTransferFactory:
        factory = self._factories.get(engine)
        if factory is None:
            installed = ", ".join(sorted(member.value for member in self._factories))
            raise TransferError(
                f"no transfer from {engine.value} into postgres is installed "
                f"(entry point group {self.GROUP}); installed: {installed or 'none'}"
            )

        return factory
