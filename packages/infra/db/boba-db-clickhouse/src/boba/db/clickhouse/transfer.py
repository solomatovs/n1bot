"""Стороны ClickHouse для потоков передачи между конкретными движками:
контракт колонок из шапки TabSeparatedWithNamesAndTypes того же запроса,
TabSeparated наружу, таблица-приёмник по каталогу system.columns, разбор
текста типа ClickHouse, разбор типов rules.column_types сервером приёмника,
двойник таблицы и вставка потока через input().

Транзакций у ClickHouse нет, поэтому приёмник грузит не в таблицу, а в её
двойник <table>__ex той же структуры и после загрузки меняет их местами
одним exchange tables: читатели не видят частично загруженную таблицу,
прежняя версия остаётся в двойнике. exchange tables работает только в базе
с движком Atomic.

Ошибки:
TransferError — база не Atomic; тип из rules.column_types сервер не знает;
    пара для движка источника не установлена.
ClickHouseQueryError — сервер отклонил запрос приёмника.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from importlib.metadata import entry_points
from typing import Any, ClassVar, Protocol, runtime_checkable

from clickhouse_connect.driver.asyncclient import AsyncClient
from clickhouse_connect.driver.exceptions import ClickHouseError as DriverError
from pydantic import BaseModel, ConfigDict

from boba.db.clickhouse.errors import ClickHouseQueryError
from boba.db.clickhouse.formats.lines import Lines
from boba.db.clickhouse.formats.tsv import TsvHeader
from boba.db.clickhouse.payload import PayloadClickHouse, ReadTuning
from boba.db.clickhouse.query import (
    ChIdentifier,
    ChIdentifiers,
    ChQuery,
    ChQueryBuilder,
)
from boba.db.clickhouse.target import ChCluster, ChPlacement, ChTableRef, ChTableRole
from boba.db.clickhouse.trace import ChCommandReport
from boba.toolkit.sync import ColumnType, TimeUnit, TypeFamily
from boba.toolkit.transfer import (
    ColumnRules,
    CreateTemplate,
    DeleteOutcome,
    DeleteStrategyApply,
    Engine,
    InsertStrategyApply,
    SchemaHead,
    SchemaStrategyPlan,
    StreamWire,
    TemplateVar,
    TransferError,
    TransferInbound,
    TransferOutbound,
    TransferReport,
    TransferSink,
    TransferTable,
    UnknownTypeApply,
)

__all__ = [
    "ChCatalogColumn",
    "ChContract",
    "ChContractTypes",
    "ChInputSink",
    "ChInputTypes",
    "ChNullableTwinTypes",
    "ChParsedType",
    "ChPlannedColumn",
    "ChSourceColumn",
    "ChStatements",
    "ChTableFacts",
    "ChTablePlan",
    "ChTransferTable",
    "ChTsvOut",
    "ChTwin",
    "ChTypeResolver",
    "ChTypes",
    "ClickHouseTransfer",
    "ClickHouseTransferFactory",
    "ClickHouseTransfers",
]


class ChSourceColumn(BaseModel):
    """Колонка контракта источника ClickHouse: позиция и имя из шапки ответа,
    текст типа как его напечатал сервер (Nullable внутри текста)."""

    model_config = ConfigDict(frozen=True)

    position: int
    name: str
    type_text: str


class ChContract(BaseModel):
    """Контракт источника ClickHouse в кадре schema: колонки из шапки
    TabSeparatedWithNamesAndTypes того же запроса, что везёт данные, и имя
    сервера из заголовка ответа."""

    model_config = ConfigDict(frozen=True)

    columns: tuple[ChSourceColumn, ...]
    server: str

    def names(self) -> list[str]:
        return [column.name for column in self.columns]

    def render(self, wire: StreamWire) -> str:
        lines = [
            f"streamed out {wire.value} from clickhouse {self.server}, "
            f"{len(self.columns)} columns:"
        ]
        for column in self.columns:
            lines.append(f"  {column.name}: {column.type_text}")

        return "\n".join(lines)


class ChTsvOut:
    """Источник: запрос выполняется один раз в формате
    TabSeparatedWithNamesAndTypes, из начала ответа снимаются две строки
    шапки — имена и типы — они уходят кадром schema, остальные байты уходят
    кадрами rows как чистый TabSeparated без разбора."""

    FORMAT: ClassVar[str] = "TabSeparatedWithNamesAndTypes"
    HEADER_LINES: ClassVar[int] = 2

    def __init__(self, client: AsyncClient) -> None:
        self._client = client
        self._payload = PayloadClickHouse
        self._header = TsvHeader()
        self._lines = Lines(self.FORMAT, self.HEADER_LINES)

    async def stream(
        self, text: str, chunk_bytes: int, out: TransferOutbound
    ) -> ChCommandReport:
        tuning = ReadTuning(socket_read_size=chunk_bytes, read_buffer_size=chunk_bytes)
        async with self._payload.byte_stream_out(
            self._client, text, self.FORMAT, tuning=tuning
        ) as stream:
            chunks = Lines.views(stream.blocks)
            head = await self._lines.take(chunks)
            contract = self._contract(head.lines, stream.trace.server)
            await out.schema(
                SchemaHead(
                    kind="schema",
                    source_engine=Engine.CLICKHOUSE,
                    wire=StreamWire.TSV,
                    contract=contract.model_dump(mode="json"),
                )
            )
            async for block in Lines.all(head.rest, chunks):
                await out.rows(block)

            return stream.trace.report(contract.render(StreamWire.TSV), text)

    def _contract(self, lines: Sequence[bytes], server: str) -> ChContract:
        names = self._header.parse(lines[0])
        types = self._header.parse(lines[1])
        columns: list[ChSourceColumn] = []
        for position, (name, type_text) in enumerate(zip(names, types, strict=True)):
            columns.append(
                ChSourceColumn(position=position, name=name, type_text=type_text)
            )

        return ChContract(columns=tuple(columns), server=server)


class ChStatements:
    """Запросы приёмника на клиенте драйвера: команда и выборка строк; отказ
    сервера упаковывается в ClickHouseQueryError с текстом запроса."""

    def __init__(self, client: AsyncClient) -> None:
        self._client = client

    async def command(self, query: ChQuery) -> None:
        try:
            await self._client.command(query.text, parameters=query.params)
        except DriverError as exc:
            raise ClickHouseQueryError(
                f"clickhouse statement failed: {exc}; statement: {query.text[:300]!r}"
            ) from exc

    async def rows(self, query: ChQuery) -> list[Sequence[Any]]:
        try:
            result = await self._client.query(query.text, parameters=query.params)
        except DriverError as exc:
            raise ClickHouseQueryError(
                f"clickhouse query failed: {exc}; query: {query.text[:300]!r}"
            ) from exc

        return list(result.result_rows)


@dataclass(frozen=True)
class ChParsedType:
    """Тип ClickHouse, разобранный из текста: нейтральный тип, nullable,
    длина FixedString (0 — без предела), текст типа без обёрток, его имя
    (head) и аргументы в скобках как напечатал сервер."""

    kind: ColumnType
    nullable: bool
    char_length: int
    text: str
    head: str
    args: str


class ChWrapper(StrEnum):
    """Обёртки типа ClickHouse, которые не меняют семейство значения."""

    NULLABLE = "Nullable"
    LOW_CARDINALITY = "LowCardinality"


class ChTypes:
    """Текст типа ClickHouse (как его печатают system.columns и toTypeName)
    в нейтральный тип: Nullable и LowCardinality снимаются, дальше имя
    типа и его аргументы в скобках. Незнакомое семейство — other."""

    INTEGERS: ClassVar[Mapping[str, int]] = {
        "Int8": 8,
        "Int16": 16,
        "Int32": 32,
        "Int64": 64,
        "Int128": 128,
        "Int256": 256,
    }
    UNSIGNED: ClassVar[Mapping[str, int]] = {
        "UInt8": 8,
        "UInt16": 16,
        "UInt32": 32,
        "UInt64": 64,
        "UInt128": 128,
        "UInt256": 256,
    }
    FLOATS: ClassVar[Mapping[str, int]] = {"Float32": 32, "Float64": 64}
    DECIMALS: ClassVar[Mapping[str, int]] = {
        "Decimal32": 9,
        "Decimal64": 18,
        "Decimal128": 38,
        "Decimal256": 76,
    }
    PLAIN: ClassVar[Mapping[str, TypeFamily]] = {
        "String": TypeFamily.STRING,
        "Bool": TypeFamily.BOOLEAN,
        "Date": TypeFamily.DATE,
        "Date32": TypeFamily.DATE,
        "UUID": TypeFamily.UUID,
        "IPv4": TypeFamily.NETWORK,
        "IPv6": TypeFamily.NETWORK,
        "Enum8": TypeFamily.STRING,
        "Enum16": TypeFamily.STRING,
        "JSON": TypeFamily.JSON,
        "Object": TypeFamily.JSON,
        "Array": TypeFamily.ARRAY,
    }
    SPACES: ClassVar[re.Pattern[str]] = re.compile(r"\s+")
    UNITS: ClassVar[Sequence[tuple[int, TimeUnit]]] = (
        (0, TimeUnit.SECOND),
        (3, TimeUnit.MILLISECOND),
        (6, TimeUnit.MICROSECOND),
        (9, TimeUnit.NANOSECOND),
    )

    def normalized(self, text: str) -> str:
        """Текст типа с одиночными пробелами: новые серверы печатают именованный
        Tuple в несколько строк с отступами, старые — в одну; сверка текстов
        не должна зависеть от версии."""
        collapsed = self.SPACES.sub(" ", text.strip())

        return collapsed.replace("( ", "(").replace(" )", ")")

    def as_nullable(self, text: str) -> str:
        """Тот же тип, но принимающий NULL: Nullable(T); у LowCardinality(T)
        Nullable ставится внутрь, снаружи ClickHouse его не принимает."""
        head, args = self._split(text.strip())
        if head == ChWrapper.NULLABLE.value:
            return text.strip()

        if head == ChWrapper.LOW_CARDINALITY.value:
            return f"{head}({self.as_nullable(args)})"

        return f"{ChWrapper.NULLABLE.value}({text.strip()})"

    def parse(self, text: str) -> ChParsedType:
        inner = self.normalized(text)
        nullable = False
        while True:
            head, args = self._split(inner)
            if head == ChWrapper.NULLABLE.value:
                nullable = True
                inner = args
                continue

            if head == ChWrapper.LOW_CARDINALITY.value:
                inner = args
                continue

            break

        head, args = self._split(inner)
        char_length = 0
        if head == "FixedString":
            char_length = int(args)

        return ChParsedType(
            kind=self._kind(head, args, inner),
            nullable=nullable,
            char_length=char_length,
            text=inner,
            head=head,
            args=args,
        )

    def _kind(self, head: str, args: str, text: str) -> ColumnType:
        parsers: Mapping[str, Callable[[str, str, str], ColumnType]] = {
            "Decimal": self._decimal,
            "FixedString": self._fixed,
            "DateTime": self._datetime,
            "DateTime64": self._moment,
        }
        parser = parsers.get(head)
        if parser is not None:
            return parser(head, args, text)

        if head in self.INTEGERS:
            return ColumnType(TypeFamily.INTEGER, text, bits=self.INTEGERS[head])

        if head in self.UNSIGNED:
            bits = self.UNSIGNED[head]

            return ColumnType(TypeFamily.INTEGER, text, bits=bits, unsigned=True)

        if head in self.FLOATS:
            return ColumnType(TypeFamily.FLOAT, text, bits=self.FLOATS[head])

        if head in self.DECIMALS:
            precision = self.DECIMALS[head]

            return ColumnType(
                TypeFamily.DECIMAL, text, precision=precision, scale=int(args)
            )

        family = self.PLAIN.get(head, TypeFamily.OTHER)

        return ColumnType(family, text)

    def _decimal(self, head: str, args: str, text: str) -> ColumnType:
        precision_text, _, scale_text = args.partition(",")
        scale = 0
        if scale_text.strip():
            scale = int(scale_text)

        return ColumnType(
            TypeFamily.DECIMAL, text, precision=int(precision_text), scale=scale
        )

    @staticmethod
    def _fixed(head: str, args: str, text: str) -> ColumnType:
        return ColumnType(TypeFamily.STRING, text)

    @staticmethod
    def _datetime(head: str, args: str, text: str) -> ColumnType:
        return ColumnType(
            TypeFamily.TIMESTAMP, text, unit=TimeUnit.SECOND, zoned=bool(args)
        )

    def _moment(self, head: str, args: str, text: str) -> ColumnType:
        precision_text, _, zone = args.partition(",")
        precision = int(precision_text)
        unit = TimeUnit.NANOSECOND
        for limit, candidate in self.UNITS:
            if precision <= limit:
                unit = candidate
                break

        return ColumnType(
            TypeFamily.TIMESTAMP, text, unit=unit, zoned=bool(zone.strip())
        )

    @staticmethod
    def _split(text: str) -> tuple[str, str]:
        """Имя типа и текст внутри внешних скобок; без скобок — пусто."""
        head, bracket, rest = text.partition("(")
        if not bracket:
            return head.strip(), ""

        return head.strip(), rest.rstrip()[:-1]


@dataclass(frozen=True)
class ChCatalogColumn:
    """Колонка таблицы приёмника из system.columns: позиция (с нуля), имя и
    текст типа как его печатает сервер."""

    position: int
    name: str
    type_text: str


class ChDatabaseEngine(StrEnum):
    """Движок базы, в которой работает exchange tables."""

    ATOMIC = "Atomic"


class ChTableFacts:
    """Таблица по каталогу ClickHouse: движок базы (обязан быть Atomic), есть
    ли таблица и её колонки из system.columns."""

    def __init__(self, client: AsyncClient, table: ChTableRef) -> None:
        self._table = table
        self._statements = ChStatements(client)

    async def require_atomic(self) -> None:
        query = (
            ChQueryBuilder()
            .add(
                "select engine from system.databases where name = %(db)s",
                db=self._table.database,
            )
            .build()
        )
        rows = await self._statements.rows(query)
        if not rows:
            raise TransferError(
                f"clickhouse: database {self._table.database} does not exist; "
                f"create it before loading {self._table.text()}"
            )

        engine = str(rows[0][0])
        if engine != ChDatabaseEngine.ATOMIC.value:
            raise TransferError(
                f"clickhouse: database {self._table.database} has engine {engine}; "
                f"loading goes through exchange tables, which needs an "
                f"{ChDatabaseEngine.ATOMIC.value} database"
            )

    async def require_cluster(self, cluster: ChCluster) -> None:
        """Кластер DDL приёмника есть в system.clusters сервера соединения."""
        if not cluster.given():
            return

        query = (
            ChQueryBuilder()
            .add(
                "select count() from system.clusters where cluster = %(name)s",
                name=cluster.name,
            )
            .build()
        )
        rows = await self._statements.rows(query)
        if int(rows[0][0]) > 0:
            return

        known = await self._statements.rows(
            ChQueryBuilder().add("select distinct cluster from system.clusters").build()
        )
        names: list[str] = []
        for row in known:
            names.append(str(row[0]))

        raise TransferError(
            f"clickhouse: cluster {cluster.name!r} is not in system.clusters of the "
            f"server; known clusters: {', '.join(sorted(names)) or 'none'}"
        )

    async def exists(self) -> bool:
        return await self.exists_table(self._table)

    async def exists_table(self, table: ChTableRef) -> bool:
        query = (
            ChQueryBuilder()
            .add(
                "select count() from system.tables "
                "where database = %(db)s and name = %(name)s",
                db=table.database,
                name=table.name,
            )
            .build()
        )
        rows = await self._statements.rows(query)

        return int(rows[0][0]) > 0

    async def columns(self) -> Sequence[ChCatalogColumn]:
        query = (
            ChQueryBuilder()
            .add(
                "select name, type from system.columns "
                "where database = %(db)s and table = %(name)s order by position",
                db=self._table.database,
                name=self._table.name,
            )
            .build()
        )
        rows = await self._statements.rows(query)
        columns: list[ChCatalogColumn] = []
        for position, row in enumerate(rows):
            columns.append(
                ChCatalogColumn(
                    position=position, name=str(row[0]), type_text=str(row[1])
                )
            )

        return tuple(columns)


class ChTypeResolver:
    """Тексты типов из rules.column_types, разобранные сервером приёмника:
    toTypeName(defaultValueOfTypeName(<тип>)) печатает тип так, как его
    печатает каталог (decimal(20,6) -> Decimal(20, 6)); незнакомый тип —
    ошибка до любого DDL."""

    def __init__(self, client: AsyncClient) -> None:
        self._statements = ChStatements(client)

    async def resolve(self, column_types: Mapping[str, str]) -> dict[str, str]:
        resolved: dict[str, str] = {}
        for name, text in column_types.items():
            query = (
                ChQueryBuilder()
                .add("select toTypeName(defaultValueOfTypeName(%(type)s))", type=text)
                .build()
            )
            try:
                rows = await self._statements.rows(query)
            except ClickHouseQueryError as exc:
                raise TransferError(
                    f"rules.column_types[{name!r}] = {text!r}: the target clickhouse "
                    f"does not know this type: {exc}"
                ) from exc

            resolved[name] = str(rows[0][0])

        return resolved


class ChTwinFill(StrEnum):
    """Какие прежние строки таблицы попадают в двойник до потока."""

    ALL = "all"
    NONE = "none"
    KEEP_WHERE_NOT = "keep_where_not"


class ChTwin:
    """Двойник таблицы <table>__ex. Стратегия удаления лишь отмечает, какие
    прежние строки перенести (fill), prepare пересоздаёт двойник той же
    структуры и переносит их, после вставки потока exchange меняет таблицу и
    двойник местами. Прежняя версия остаётся в двойнике до следующей
    загрузки. С кластером DDL двойника и exchange идут ON CLUSTER."""

    def __init__(
        self, client: AsyncClient, table: ChTableRef, cluster: ChCluster
    ) -> None:
        self._table = table
        self._twin = table.twin()
        self._cluster = cluster
        self._statements = ChStatements(client)
        self._fill = ChTwinFill.ALL
        self._where = ""

    def target(self) -> ChTableRef:
        return self._twin

    def keeps_all(self) -> bool:
        """Стратегия удаления ничего не отметила: таблицу менять незачем."""
        return self._fill is ChTwinFill.ALL

    def empty(self) -> None:
        self._fill = ChTwinFill.NONE

    def keep_where_not(self, where: str) -> None:
        self._fill = ChTwinFill.KEEP_WHERE_NOT
        self._where = where

    async def count_where(self, where: str) -> int:
        query = (
            ChQueryBuilder()
            .add(
                "select count() from %(table_db)s.%(table_name)s where",
                **self._table.bind(ChTableRole.TABLE),
            )
            .raw_query(where)
            .build()
        )
        rows = await self._statements.rows(query)

        return int(rows[0][0])

    async def prepare(self) -> None:
        names = self._names()
        await self._statements.command(
            ChQueryBuilder()
            .add("drop table if exists %(twin_db)s.%(twin_name)s", **names)
            .when(self._cluster.given(), "on cluster %(cluster)s", **names)
            .build()
        )
        await self._statements.command(
            ChQueryBuilder()
            .add("create table %(twin_db)s.%(twin_name)s", **names)
            .when(self._cluster.given(), "on cluster %(cluster)s", **names)
            .add("as %(table_db)s.%(table_name)s", **names)
            .build()
        )
        if self._fill is ChTwinFill.NONE:
            return

        builder = ChQueryBuilder().add(
            "insert into %(twin_db)s.%(twin_name)s "
            "select * from %(table_db)s.%(table_name)s",
            **names,
        )
        if self._fill is ChTwinFill.KEEP_WHERE_NOT:
            builder.add("where not (").raw_query(self._where).add(")")

        await self._statements.command(builder.build())

    async def exchange(self) -> None:
        await self._statements.command(
            ChQueryBuilder()
            .add(
                "exchange tables %(table_db)s.%(table_name)s "
                "and %(twin_db)s.%(twin_name)s",
                **self._names(),
            )
            .when(self._cluster.given(), "on cluster %(cluster)s", **self._names())
            .build()
        )

    def _names(self) -> dict[str, ChIdentifier]:
        names = self._table.bind(ChTableRole.TABLE)
        names.update(self._twin.bind(ChTableRole.TWIN))
        if self._cluster.given():
            names.update(self._cluster.bind())

        return names


@dataclass(frozen=True)
class ChPlannedColumn:
    """Колонка плана таблицы: имя в таблице, поле потока, которое в неё
    ложится, текст типа ClickHouse для DDL с Nullable (пусто — у ClickHouse
    нет типа для этой колонки, решает стратегия неизвестных типов), nullable
    потока и то, что о типе известно, для текста отказа."""

    name: str
    source_name: str
    type_text: str
    nullable: bool
    known: str


@dataclass(frozen=True)
class ChTablePlan:
    """Колонки таблицы-приёмника в порядке полей потока."""

    columns: Sequence[ChPlannedColumn]

    def names(self) -> list[str]:
        return [column.name for column in self.columns]

    def source_names(self) -> list[str]:
        return [column.source_name for column in self.columns]


class ChTransferTable(TransferTable):
    """Реализация TransferTable для ClickHouse: create по шаблону вызывающего
    из готовых текстов типов плана, backup и drop действуют на саму таблицу,
    с кластером — on cluster. Стратегии удаления ничего не удаляют, а
    отмечают в двойнике, какие прежние строки перенести до потока: таблица
    меняется только exchange tables после вставки."""

    BACKUP_STAMP: ClassVar[str] = "%Y%m%d_%H%M%S_%f"
    STRING: ClassVar[str] = "String"

    def __init__(  # noqa: PLR0913
        self,
        client: AsyncClient,
        table: ChTableRef,
        plan: ChTablePlan,
        template: CreateTemplate,
        twin: ChTwin,
        placement: ChPlacement,
    ) -> None:
        self._table = table
        self._plan = plan
        self._template = template
        self._twin = twin
        self._placement = placement
        self._facts = ChTableFacts(client, table)
        self._statements = ChStatements(client)
        self._types = ChTypes()

    async def exists(self) -> bool:
        return await self._facts.exists()

    async def create(self, unknown_as_varchar: bool) -> str:
        statement = self._template.render(
            {
                TemplateVar.DATABASE: str(ChIdentifier(self._table.database)),
                TemplateVar.TABLE_NAME: str(ChIdentifier(self._table.name)),
                TemplateVar.COLUMNS: self._columns_fragment(unknown_as_varchar),
                TemplateVar.CLUSTER: self._placement.cluster.template_value(),
                TemplateVar.ORDER_BY: self._placement.order_by,
            }
        )
        await self._statements.command(ChQueryBuilder().raw_query(statement).build())

        return statement

    def _columns_fragment(self, unknown_as_varchar: bool) -> str:
        parts: list[str] = []
        for column in self._plan.columns:
            type_text = self._type_text(column, unknown_as_varchar)
            parts.append(f"{ChIdentifier(column.name)} {type_text}")

        return ", ".join(parts)

    def _type_text(self, column: ChPlannedColumn, unknown_as_varchar: bool) -> str:
        if column.type_text:
            return column.type_text

        if not unknown_as_varchar:
            raise TransferError(
                f"column {column.name}: the target clickhouse cannot map the stream "
                f"type {column.known}; declare the target type in "
                f'rules.column_types["{column.name}"] or take unknown_types '
                f"fallback_as_varchar"
            )

        if column.nullable:
            return self._types.as_nullable(self.STRING)

        return self.STRING

    async def backup(self) -> str:
        stamp = datetime.now(UTC).strftime(self.BACKUP_STAMP)
        renamed = ChTableRef(
            database=self._table.database, name=f"{self._table.name}_bak_{stamp}"
        )
        names = self._table.bind(ChTableRole.TABLE)
        names.update(renamed.bind(ChTableRole.RENAMED))
        cluster = self._placement.cluster
        names.update(cluster.bind())
        await self._statements.command(
            ChQueryBuilder()
            .add(
                "rename table %(table_db)s.%(table_name)s "
                "to %(renamed_db)s.%(renamed_name)s",
                **names,
            )
            .when(cluster.given(), "on cluster %(cluster)s", **names)
            .build()
        )

        return renamed.name

    async def drop(self, cascade: bool) -> str:
        names = self._table.bind(ChTableRole.TABLE)
        cluster = self._placement.cluster
        names.update(cluster.bind())
        query = (
            ChQueryBuilder()
            .add("drop table %(table_db)s.%(table_name)s", **names)
            .when(cluster.given(), "on cluster %(cluster)s", **names)
            .build()
        )
        await self._statements.command(query)

        return f"drop table {self._table.text()}"

    async def truncate(self) -> DeleteOutcome:
        self._twin.empty()

        return DeleteOutcome(
            rows=0,
            statement=f"truncate: {self._twin.target().text()} starts empty",
        )

    async def delete_all(self) -> DeleteOutcome:
        rows = await self._twin.count_where("1")
        self._twin.empty()

        return DeleteOutcome(
            rows=rows,
            statement=f"delete all: {self._twin.target().text()} starts empty",
        )

    async def delete_where(self, where: str) -> DeleteOutcome:
        rows = await self._twin.count_where(where)
        self._twin.keep_where_not(where)

        return DeleteOutcome(
            rows=rows,
            statement=(
                f"delete where {where}: {self._twin.target().text()} keeps the "
                f"other rows"
            ),
        )


class ChInputTypes(Protocol):
    """Типы полей потока для структуры input(): по колонкам двойника, как их
    печатает каталог, отдаёт текст типа поля по имени колонки таблицы."""

    def of(self, twin: Sequence[ChCatalogColumn]) -> Mapping[str, str]: ...


class ChNullableTwinTypes(ChInputTypes):
    """Поле потока читается типом колонки двойника; nullable поле — тем же
    типом с Nullable, иначе читатель формата подставил бы значение по
    умолчанию вместо NULL."""

    def __init__(self, nullable: Iterable[str]) -> None:
        self._nullable = frozenset(nullable)
        self._types = ChTypes()

    def of(self, twin: Sequence[ChCatalogColumn]) -> Mapping[str, str]:
        types: dict[str, str] = {}
        for column in twin:
            text = column.type_text
            if column.name in self._nullable:
                text = self._types.as_nullable(text)

            types[column.name] = text

        return types


class ChContractTypes(ChInputTypes):
    """Поле потока читается типом, который назвал источник в контракте."""

    def __init__(self, types: Mapping[str, str]) -> None:
        self._types = dict(types)

    def of(self, twin: Sequence[ChCatalogColumn]) -> Mapping[str, str]:
        return self._types


class ChInputSink(TransferSink):
    """Реализация TransferSink для ClickHouse: двойник готовится стратегией
    удаления, тела кадров вставляются в него как есть через input() в
    формате потока, select переименовывает поля потока в колонки таблицы,
    затем exchange tables. NULL потока в колонку без Nullable — ошибка
    сервера, а не значение по умолчанию: тихих подмен нет, как у not null
    postgres. discard без удаления таблицу не трогает, с удалением — готовит
    двойник и меняет местами."""

    SETTINGS: ClassVar[Mapping[str, int]] = {"insert_null_as_default": 0}

    def __init__(  # noqa: PLR0913
        self,
        client: AsyncClient,
        twin: ChTwin,
        plan: ChTablePlan,
        fmt: str,
        types: ChInputTypes,
        inbound: TransferInbound,
    ) -> None:
        self._client = client
        self._twin = twin
        self._plan = plan
        self._fmt = fmt
        self._types = types
        self._inbound = inbound
        self._facts = ChTableFacts(client, twin.target())
        self._payload = PayloadClickHouse

    async def load(self) -> int:
        await self._twin.prepare()
        query = await self._insert()
        trace = await self._payload.byte_stream_in(
            self._client,
            query.text,
            query.params,
            settings=self.SETTINGS,
            blocks=self._inbound.bodies(),
        )
        await self._twin.exchange()

        return trace.written_rows

    async def discard(self) -> int:
        async for _ in self._inbound.bodies():
            pass

        if self._twin.keeps_all():
            return 0

        await self._twin.prepare()
        await self._twin.exchange()

        return 0

    async def _insert(self) -> ChQuery:
        types = self._types.of(await self._facts.columns())
        structure: list[str] = []
        for column in self._plan.columns:
            type_text = types.get(column.name)
            if type_text is None:
                raise TransferError(
                    f"table {self._twin.target().text()} has no column "
                    f"{column.name!r} for stream field {column.source_name!r}; "
                    f"table columns: {', '.join(sorted(types))}"
                )

            structure.append(f"{ChIdentifier(column.source_name)} {type_text}")

        return (
            ChQueryBuilder()
            .add(
                "insert into %(twin_db)s.%(twin_name)s (%(columns)s) "
                "select %(fields)s from input(%(structure)s)",
                columns=ChIdentifiers(self._plan.names()),
                fields=ChIdentifiers(self._plan.source_names()),
                structure=", ".join(structure),
                **self._twin.target().bind(ChTableRole.TWIN),
            )
            .add(f"format {self._fmt}")
            .build()
        )


class ClickHouseTransfer(Protocol):
    """Пара «движок источника → ClickHouse»: разбирает контракт своего
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
    ) -> TransferReport: ...


@runtime_checkable
class ClickHouseTransferFactory(Protocol):
    """Конструктор пары: клиент приёмника, таблица, её кластер и ключ
    сортировки, кадр схемы, поток тел."""

    def __call__(
        self,
        client: AsyncClient,
        table: ChTableRef,
        placement: ChPlacement,
        head: SchemaHead,
        feed: TransferInbound,
    ) -> ClickHouseTransfer: ...


class ClickHouseTransfers:
    """Реестр пар в ClickHouse по entry points группы
    boba.transfer.clickhouse: имя записи — движок источника, значение —
    класс пары."""

    GROUP: ClassVar[str] = "boba.transfer.clickhouse"

    def __init__(self, factories: Mapping[Engine, ClickHouseTransferFactory]) -> None:
        self._factories = dict(factories)

    @classmethod
    def discover(cls) -> ClickHouseTransfers:
        factories: dict[Engine, ClickHouseTransferFactory] = {}
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
            if not isinstance(loaded, ClickHouseTransferFactory):
                raise TransferError(
                    f"entry point {entry.name!r} of group {cls.GROUP!r} "
                    f"({entry.value}): expected a transfer class, got {loaded!r}"
                )

            factories[engine] = loaded

        return cls(factories)

    def pair(self, engine: Engine) -> ClickHouseTransferFactory:
        factory = self._factories.get(engine)
        if factory is None:
            installed = ", ".join(sorted(member.value for member in self._factories))
            raise TransferError(
                f"no transfer from {engine.value} into clickhouse is installed "
                f"(installed: {installed or 'none'}); take wire arrow at the source"
            )

        return factory
