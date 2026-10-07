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

from abc import abstractmethod
from collections.abc import (
    AsyncIterator,
    Callable,
    Iterable,
    Iterator,
    Mapping,
    Sequence,
)
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from importlib.metadata import entry_points
from typing import Any, ClassVar, Protocol, runtime_checkable

from clickhouse_connect.driver.asyncclient import AsyncClient
from clickhouse_connect.driver.binding import finalize_query
from clickhouse_connect.driver.exceptions import ClickHouseError as DriverError

from boba.db.clickhouse.contract import (
    ChContract,
    ChParsedType,
    ChSourceColumn,
    ChTypes,
)
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
from boba.toolkit.contract import TypeFamily
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
    "ChCatalogColumn",
    "ChContract",
    "ChContractTypes",
    "ChInetProbe",
    "ChInputSink",
    "ChInputTypes",
    "ChJsonProbe",
    "ChJsonSupport",
    "ChMatch",
    "ChMatched",
    "ChMatcher",
    "ChNullableTwinTypes",
    "ChParsedType",
    "ChPlannedColumn",
    "ChSourceColumn",
    "ChStatements",
    "ChStreamColumn",
    "ChTableFacts",
    "ChTablePlan",
    "ChTransfer",
    "ChTransferFactory",
    "ChTransferTable",
    "ChTransfers",
    "ChTsvOut",
    "ChTwin",
    "ChTypeResolver",
    "ChTypeRules",
    "ChTypes",
    "StreamBody",
    "WholeBody",
]


class ChTsvOut:
    """Источник: запрос выполняется один раз в формате
    TabSeparatedWithNamesAndTypes, из начала ответа снимаются две строки
    шапки — имена и типы — они уходят кадром schema, остальные байты уходят
    кадрами rows как чистый TabSeparated без разбора."""

    FORMAT: ClassVar[str] = "TabSeparatedWithNamesAndTypes"
    HEADER_LINES: ClassVar[int] = 2

    def __init__(self, client: AsyncClient, journal: CommandJournal) -> None:
        self._client = client
        self._journal = journal
        self._payload = PayloadClickHouse
        self._header = TsvHeader()
        self._lines = Lines(self.FORMAT, self.HEADER_LINES)

    async def stream(
        self, text: str, chunk_bytes: int, out: TransferOutbound
    ) -> ChCommandReport:
        """Выгрузка целиком — одна команда журнала: тела идут мимо него."""
        tuning = ReadTuning(socket_read_size=chunk_bytes, read_buffer_size=chunk_bytes)
        statement = f"{text}\nformat {self.FORMAT}"
        with self._journal.command(statement, CommandKind.ACTION) as running:
            async with self._payload.byte_stream_out(
                self._client, text, self.FORMAT, tuning=tuning
            ) as stream:
                chunks = Lines.views(stream.blocks)
                head = await self._lines.take(chunks)
                contract = self._contract(head.lines, stream.trace.server)
                self._journal.columns(contract.rows())
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

                report = stream.trace.report(
                    contract.caption(StreamWire.TSV), text, columns=contract.rows()
                )

            running.status = f"read {report.read_rows} rows"

        return report

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
    """Запросы приёмника на клиенте драйвера: команда (действие) и выборка
    строк (справка по каталогу) с настройками сервера на запрос. Каждый
    запрос — команда журнала вызова с итоговым текстом SQL, где параметры
    клиента уже подставлены; отказ сервера упаковывается в
    ClickHouseQueryError с этим текстом."""

    SETTINGS_COMMENT: ClassVar[str] = "-- settings: "

    def __init__(self, client: AsyncClient, journal: CommandJournal) -> None:
        self._client = client
        self._journal = journal

    async def command(
        self, query: ChQuery, settings: Mapping[str, Any] | None = None
    ) -> None:
        text = self.text(query, settings)
        with self._journal.command(text, CommandKind.ACTION) as running:
            try:
                await self._client.command(
                    query.text, parameters=query.params, settings=self._dict(settings)
                )
            except DriverError as exc:
                raise ClickHouseQueryError(
                    f"clickhouse statement failed: {exc}; statement: {text!r}"
                ) from exc

            running.status = "done"

    async def rows(
        self, query: ChQuery, settings: Mapping[str, Any] | None = None
    ) -> list[Sequence[Any]]:
        text = self.text(query, settings)
        with self._journal.command(text, CommandKind.LOOKUP) as running:
            try:
                result = await self._client.query(
                    query.text, parameters=query.params, settings=self._dict(settings)
                )
            except DriverError as exc:
                raise ClickHouseQueryError(
                    f"clickhouse query failed: {exc}; query: {text!r}"
                ) from exc

            rows = list(result.result_rows)
            running.status = f"{len(rows)} rows"

        return rows

    def text(self, query: ChQuery, settings: Mapping[str, Any] | None = None) -> str:
        """Итоговый SQL запроса: параметры клиента подставлены, настройки
        запроса — строкой комментария под ним."""
        text = query.text
        if query.params:
            text = finalize_query(query.text, query.params)

        if not settings:
            return text

        pairs: list[str] = []
        for key, value in settings.items():
            pairs.append(f"{key} = {value}")

        return f"{text}\n{self.SETTINGS_COMMENT}{', '.join(pairs)}"

    @staticmethod
    def _dict(settings: Mapping[str, Any] | None) -> dict[str, Any] | None:
        if settings is None:
            return None

        return dict(settings)


@dataclass(frozen=True)
class ChJsonSupport:
    """Что сервер приёмника умеет с типом JSON: настройки, при которых он
    создаётся (пусто — без настроек), как сервер печатает этот тип
    (Object('json') до 24, дальше JSON) и можно ли Nullable(JSON). Пары без
    JSON у приёмника переводят json в String."""

    settings: Mapping[str, Any]
    available: bool
    type_text: str
    nullable: bool


class ChJsonProbe:
    """Проба типа JSON на сервере приёмника: тип экспериментальный до 25.3 и
    включается разными настройками по версиям, поэтому кандидаты
    перебираются, пока CAST('{}', 'JSON') не пройдёт; Nullable(JSON) есть
    только с 25.x."""

    CANDIDATES: ClassVar[Sequence[Mapping[str, Any]]] = (
        {"allow_experimental_json_type": 1, "allow_experimental_object_type": 1},
        {"allow_experimental_object_type": 1},
        {},
    )
    JSON: ClassVar[str] = "select toTypeName(CAST('{}', 'JSON'))"
    NULLABLE: ClassVar[str] = "select toTypeName(CAST(NULL, 'Nullable(JSON)'))"

    def __init__(self, client: AsyncClient, journal: CommandJournal) -> None:
        self._statements = ChStatements(client, journal)

    async def probe(self) -> ChJsonSupport:
        for settings in self.CANDIDATES:
            printed = await self._printed(self.JSON, settings)
            if not printed:
                continue

            nullable = bool(await self._printed(self.NULLABLE, settings))

            return ChJsonSupport(
                settings=settings, available=True, type_text=printed, nullable=nullable
            )

        return ChJsonSupport(settings={}, available=False, type_text="", nullable=False)

    async def _printed(self, text: str, settings: Mapping[str, Any]) -> str:
        """Имя типа, как его печатает сервер; пусто — сервер отказал."""
        query = ChQueryBuilder().add(text).build()
        try:
            rows = await self._statements.rows(query, settings)
        except ClickHouseQueryError:
            # проба: отказ сервера и есть ответ «этот кандидат не подходит»
            return ""

        return str(rows[0][0])


class ChInetProbe:
    """Проба IPv6 на сервере приёмника тем же путём, что и загрузка: читает
    ли формат TabSeparated текст адреса IPv4 (10.1.2.3) в колонку IPv6 как
    ::ffff:10.1.2.3. Старые серверы отказывают (CAST при этом проходит),
    и inet postgres тогда ложится String."""

    STATEMENT: ClassVar[str] = (
        "insert into function null('x IPv6') select x from input('x IPv6') "
        "format TabSeparated"
    )
    IPV4_TEXT: ClassVar[bytes] = b"10.1.2.3\n"

    def __init__(self, client: AsyncClient, journal: CommandJournal) -> None:
        self._client = client
        self._journal = journal
        self._payload = PayloadClickHouse

    async def accepts_ipv4(self) -> bool:
        try:
            with self._journal.command(self.STATEMENT, CommandKind.LOOKUP) as running:
                await self._payload.byte_stream_in(
                    self._client, self.STATEMENT, blocks=self._body()
                )
                running.status = "accepted"
        except ClickHouseQueryError:
            # проба: отказ сервера и есть ответ «IPv4 в IPv6 не читается»
            return False

        return True

    async def _body(self) -> AsyncIterator[bytes]:
        yield self.IPV4_TEXT


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

    def __init__(
        self, client: AsyncClient, table: ChTableRef, journal: CommandJournal
    ) -> None:
        self._table = table
        self._statements = ChStatements(client, journal)

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

    def __init__(self, client: AsyncClient, journal: CommandJournal) -> None:
        self._statements = ChStatements(client, journal)

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
        self,
        client: AsyncClient,
        table: ChTableRef,
        cluster: ChCluster,
        journal: CommandJournal,
        settings: Mapping[str, Any] = {},
    ) -> None:
        self._table = table
        self._twin = table.twin()
        self._cluster = cluster
        self._settings = dict(settings)
        self._statements = ChStatements(client, journal)
        self._fill = ChTwinFill.ALL
        self._where = ""

    def target(self) -> ChTableRef:
        return self._twin

    def method(self) -> str:
        """Как строки попали в таблицу: через двойник и exchange tables."""
        return (
            f"into {self._twin.text()}, then exchange tables with "
            f"{self._table.text()}; the previous version stays in {self._twin.text()}"
        )

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
            .build(),
            self._settings,
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

    def planned(self) -> PlannedColumn:
        return PlannedColumn(
            name=self.name,
            field=self.source_name,
            type=self.type_text,
            nullable=self.nullable,
            source_type=self.known,
            notes=tuple(self._notes()),
        )

    def _notes(self) -> Iterator[str]:
        if self.source_name != self.name:
            yield f"renamed from {self.source_name}"

        if not self.type_text:
            yield "no target type for the stream type: unknown_types decides"


@dataclass(frozen=True)
class ChTablePlan:
    """Колонки таблицы-приёмника в порядке полей потока."""

    columns: Sequence[ChPlannedColumn]

    def planned(self) -> list[PlannedColumn]:
        planned: list[PlannedColumn] = []
        for column in self.columns:
            planned.append(column.planned())

        return planned

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
        journal: CommandJournal,
        settings: Mapping[str, Any] = {},
    ) -> None:
        self._table = table
        self._plan = plan
        self._template = template
        self._twin = twin
        self._placement = placement
        self._settings = dict(settings)
        self._facts = ChTableFacts(client, table, journal)
        self._statements = ChStatements(client, journal)
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
        await self._statements.command(
            ChQueryBuilder().raw_query(statement).build(), self._settings
        )

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

        return DeleteOutcome(effect="replaced: the new version starts empty")

    async def delete_all(self) -> DeleteOutcome:
        rows = await self._twin.count_where("1")
        self._twin.empty()

        return DeleteOutcome(
            effect=f"{rows} rows replaced: the new version starts empty"
        )

    async def delete_where(self, where: str) -> DeleteOutcome:
        rows = await self._twin.count_where(where)
        self._twin.keep_where_not(where)

        return DeleteOutcome(
            effect=(
                f"{rows} rows matching `{where}` left out, the others carried "
                f"into the new version"
            )
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


class StreamBody(Protocol):
    """Каким тела кадров потока уходят серверу телом запроса вставки.
    Реализуют WholeBody (текстовые форматы и Native) и ArrowBodyWithoutEos
    (boba.db.clickhouse.arrow_stream)."""

    @abstractmethod
    def shaped(self, blocks: AsyncIterator[Chunk]) -> AsyncIterator[Chunk]:
        """Тело запроса вставки по телам кадров потока."""


class WholeBody(StreamBody):
    """Реализация StreamBody форматами, которые сервер читает до конца тела
    запроса (TabSeparated, Native): тела кадров идут как есть."""

    def shaped(self, blocks: AsyncIterator[Chunk]) -> AsyncIterator[Chunk]:
        return blocks


class ChInputSink(TransferSink):
    """Реализация TransferSink для ClickHouse: двойник готовится стратегией
    удаления, тела кадров вставляются в него как есть через input() в
    формате потока (каким телом запроса — решает body), select
    переименовывает поля потока в колонки таблицы, затем барьер группы и
    exchange tables. NULL потока в колонку без Nullable — ошибка
    сервера, а не значение по умолчанию: тихих подмен нет, как у not null
    postgres. discard без удаления таблицу не трогает, с удалением — готовит
    двойник и меняет местами."""

    SETTINGS: ClassVar[Mapping[str, Any]] = {"insert_null_as_default": 0}
    """Настройки каждой вставки; пара добавляет свои (разбор текста)."""

    def __init__(  # noqa: PLR0913
        self,
        client: AsyncClient,
        twin: ChTwin,
        plan: ChTablePlan,
        fmt: str,
        types: ChInputTypes,
        inbound: TransferInbound,
        journal: CommandJournal,
        body: StreamBody,
        settings: Mapping[str, Any] = {},
    ) -> None:
        self._client = client
        self._twin = twin
        self._plan = plan
        self._fmt = fmt
        self._types = types
        self._inbound = inbound
        self._journal = journal
        self._body = body
        self._settings = {**self.SETTINGS, **settings}
        self._facts = ChTableFacts(client, twin.target(), journal)
        self._statements = ChStatements(client, journal)
        self._payload = PayloadClickHouse

    async def load(self) -> int:
        """Вставка потока целиком — одна команда журнала: тела идут мимо него."""
        await self._twin.prepare()
        query = await self._insert()
        text = self._statements.text(query, self._settings)
        with self._journal.command(text, CommandKind.ACTION) as running:
            trace = await self._payload.byte_stream_in(
                self._client,
                query.text,
                query.params,
                settings=self._settings,
                blocks=self._body.shaped(self._inbound.bodies()),
            )
            running.status = f"{trace.written_rows} rows written"

        await self._inbound.committing()
        await self._twin.exchange()

        return trace.written_rows

    def method(self) -> str:
        return self._twin.method()

    async def discard(self) -> int:
        async for _ in self._inbound.bodies():
            pass

        if self._twin.keeps_all():
            return 0

        await self._twin.prepare()
        await self._inbound.committing()
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


TypeRule = Callable[["ChParsedType", "ChParsedType"], "ColumnVerdict"]


@dataclass(frozen=True)
class ChStreamColumn:
    """Поле потока глазами приёмника ClickHouse: имя колонки таблицы (после
    rename), имя поля потока, текст типа ClickHouse с Nullable (свой из
    контракта ClickHouse, переведённый парой из типа другого движка или из
    column_types; пусто — у ClickHouse нет типа для этого поля, решает
    стратегия неизвестных типов), nullable потока и то, что о типе известно,
    для сообщений."""

    name: str
    source_name: str
    type_text: str
    nullable: bool
    known: str

    def parsed(self, types: ChTypes) -> ChParsedType:
        return types.parse(self.type_text)


class ChTypeRules:
    """Сверка типа поля потока с типом колонки таблицы. Оба текста печатает
    ClickHouse: контракт источника — из шапки ответа, каталог — из
    system.columns, column_types — сервер приёмника; обёртки Nullable и
    LowCardinality сняты, nullable — отдельно. Правила:
        - тексты совпадают — ok;
        - семейства разные — ошибка;
        - целые и float: приёмник уже или теряет знак — ошибка, шире —
          предупреждение;
        - Decimal: scale или целые разряды приёмника меньше — ошибка, больше
          — предупреждение;
        - DateTime и DateTime64: другой пояс — ошибка (текст TabSeparated
          читается в поясе колонки приёмника), точность грубее — ошибка,
          тоньше — предупреждение;
        - строки: FixedString короче — ошибка, длиннее или String —
          предупреждение; Enum в String — предупреждение, обратно — ошибка;
        - Date в Date32 — предупреждение, обратно — ошибка;
        - остальное (Array, Map, Tuple, UUID, IP, Enum с другими значениями)
          — только точное совпадение текста;
        - nullable поле в колонку без Nullable — ошибка, обратное —
          предупреждение."""

    FIXED_STRING: ClassVar[str] = "FixedString"
    STRING: ClassVar[str] = "String"
    ENUMS: ClassVar[frozenset[str]] = frozenset({"Enum8", "Enum16"})
    DATE: ClassVar[str] = "Date"
    DATE32: ClassVar[str] = "Date32"

    def __init__(self) -> None:
        self._types = ChTypes()

    def compare(self, stream: ChStreamColumn, table: ChCatalogColumn) -> ColumnVerdict:
        if not stream.type_text:
            return ColumnVerdict(
                Verdict.WARNING,
                f"type cannot be verified, clickhouse has no type for the stream "
                f"{stream.known}; table {table.type_text}",
            )

        source = stream.parsed(self._types)
        target = self._types.parse(table.type_text)
        kinds = self._kinds(source, target)
        if kinds.level is Verdict.ERROR:
            return kinds

        nullable = self._nullable(source, target)
        if nullable.level is Verdict.ERROR:
            return nullable

        if kinds.level is Verdict.WARNING:
            return kinds

        return nullable

    def _kinds(self, source: ChParsedType, target: ChParsedType) -> ColumnVerdict:
        if source.text == target.text:
            return ColumnVerdict(Verdict.OK, "ok")

        if source.kind.family is not target.kind.family:
            return self._differs(source, target)

        rules: Mapping[TypeFamily, TypeRule] = {
            TypeFamily.INTEGER: self._width,
            TypeFamily.FLOAT: self._width,
            TypeFamily.DECIMAL: self._decimal,
            TypeFamily.TIMESTAMP: self._moment,
            TypeFamily.STRING: self._string,
            TypeFamily.DATE: self._date,
        }
        rule = rules.get(source.kind.family)
        if rule is None:
            return self._differs(source, target)

        return rule(source, target)

    @staticmethod
    def _differs(source: ChParsedType, target: ChParsedType) -> ColumnVerdict:
        return ColumnVerdict(
            Verdict.ERROR, f"type differs: stream {source.text}, table {target.text}"
        )

    def _width(self, source: ChParsedType, target: ChParsedType) -> ColumnVerdict:
        src = source.kind
        tgt = target.kind
        if src.unsigned and not tgt.unsigned and src.bits >= tgt.bits:
            return self._narrower(source, target)

        if not src.unsigned and tgt.unsigned:
            return ColumnVerdict(
                Verdict.ERROR,
                f"table {target.text} has no sign, stream {source.text} is signed",
            )

        if src.bits > tgt.bits:
            return self._narrower(source, target)

        return self._wider(source, target)

    def _decimal(self, source: ChParsedType, target: ChParsedType) -> ColumnVerdict:
        src = source.kind
        tgt = target.kind
        if src.scale > tgt.scale:
            return ColumnVerdict(
                Verdict.ERROR,
                f"table {target.text} truncates the scale of stream {source.text}",
            )

        if src.precision - src.scale > tgt.precision - tgt.scale:
            return ColumnVerdict(
                Verdict.ERROR,
                f"table {target.text} holds fewer integer digits than stream "
                f"{source.text}",
            )

        return self._wider(source, target)

    def _moment(self, source: ChParsedType, target: ChParsedType) -> ColumnVerdict:
        if self._zone(source) != self._zone(target):
            return ColumnVerdict(
                Verdict.ERROR,
                f"time zone differs: stream {source.text}, table {target.text}; "
                f"TabSeparated text is read in the zone of the table column",
            )

        if source.kind.unit.rank() > target.kind.unit.rank():
            return ColumnVerdict(
                Verdict.ERROR,
                f"table {target.text} is coarser than stream {source.text}",
            )

        return self._wider(source, target)

    @staticmethod
    def _zone(parsed: ChParsedType) -> str:
        """Пояс из аргументов DateTime('UTC') и DateTime64(3, 'UTC'); пусто —
        пояс сервера."""
        if parsed.head == "DateTime":
            return parsed.args.strip()

        _, _, zone = parsed.args.partition(",")

        return zone.strip()

    def _string(self, source: ChParsedType, target: ChParsedType) -> ColumnVerdict:
        if target.head == self.STRING:
            return self._wider(source, target)

        if source.head == self.FIXED_STRING and target.head == self.FIXED_STRING:
            if source.char_length > target.char_length:
                return self._narrower(source, target)

            return self._wider(source, target)

        return self._differs(source, target)

    def _date(self, source: ChParsedType, target: ChParsedType) -> ColumnVerdict:
        if source.head == self.DATE and target.head == self.DATE32:
            return self._wider(source, target)

        return self._narrower(source, target)

    @staticmethod
    def _narrower(source: ChParsedType, target: ChParsedType) -> ColumnVerdict:
        return ColumnVerdict(
            Verdict.ERROR, f"table {target.text} is narrower than stream {source.text}"
        )

    @staticmethod
    def _wider(source: ChParsedType, target: ChParsedType) -> ColumnVerdict:
        return ColumnVerdict(
            Verdict.WARNING, f"table {target.text} is wider than stream {source.text}"
        )

    @staticmethod
    def _nullable(source: ChParsedType, target: ChParsedType) -> ColumnVerdict:
        if source.nullable and not target.nullable:
            return ColumnVerdict(
                Verdict.ERROR,
                "stream is nullable, table column is not",
                ColumnIssue.NULLABLE_INTO_NOT_NULL,
            )

        if not source.nullable and target.nullable:
            return ColumnVerdict(
                Verdict.WARNING,
                "stream is not null, table column is nullable",
                ColumnIssue.NOT_NULL_INTO_NULLABLE,
            )

        return ColumnVerdict(Verdict.OK, "ok")


@dataclass(frozen=True)
class ChMatch:
    """Колонка приёмника с тем, что о ней известно с двух сторон: поток
    (после rename и column_types) и таблица; любая сторона может
    отсутствовать — в этом и состоит сверка."""

    name: str
    stream: ChStreamColumn | None
    table: ChCatalogColumn | None

    def check(self, rules: ChTypeRules) -> ColumnCheck:
        """Сверка колонки с типами обеих сторон для отчёта."""
        stream = ""
        if self.stream is not None:
            stream = self.stream.known

        table = ""
        if self.table is not None:
            table = self.table.type_text

        return ColumnCheck(
            name=self.name, stream=stream, table=table, verdict=self.verdict(rules)
        )

    def verdict(self, rules: ChTypeRules) -> ColumnVerdict:
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
class ChMatched:
    """Итог сверки: план таблицы, поток после правил и сверка для стратегии."""

    plan: ChTablePlan
    stream: Sequence[ChStreamColumn]
    check: SchemaCheck


class ChMatcher:
    """Правила приёмника поверх полей потока, которые пара уже выразила
    типами ClickHouse: rename_columns даёт колонке таблицы поле потока,
    column_types — тип текстом (Nullable по полю потока); затем сверка с
    каталогом по ChTypeRules и план таблицы в порядке полей потока."""

    def __init__(self, rules: ColumnRules) -> None:
        self._rules = rules
        self._types = ChTypes()
        self._type_rules = ChTypeRules()

    def stream(
        self, fields: Sequence[ChStreamColumn], declared: Mapping[str, str]
    ) -> list[ChStreamColumn]:
        by_source = self._by_source(fields)
        columns: list[ChStreamColumn] = []
        for field in fields:
            name = by_source.get(field.source_name, field.source_name)
            type_text = field.type_text
            if name in declared:
                type_text = self._declared(declared[name], field.nullable)

            columns.append(replace(field, name=name, type_text=type_text))

        self._check_types(declared, columns)

        return columns

    def match(
        self, stream: Sequence[ChStreamColumn], catalog: Sequence[ChCatalogColumn]
    ) -> ChMatched:
        targets = {column.name: column for column in catalog}
        matches: list[ChMatch] = []
        seen: set[str] = set()
        for column in stream:
            seen.add(column.name)
            matches.append(
                ChMatch(name=column.name, stream=column, table=targets.get(column.name))
            )

        for column in catalog:
            if column.name in seen:
                continue

            matches.append(ChMatch(name=column.name, stream=None, table=column))

        return ChMatched(
            plan=self._plan(stream), stream=tuple(stream), check=self._check(matches)
        )

    def _declared(self, declared: str, nullable: bool) -> str:
        """Объявленный тип с Nullable, если поле потока nullable."""
        if nullable:
            return self._types.as_nullable(declared)

        return declared

    def _by_source(self, fields: Sequence[ChStreamColumn]) -> dict[str, str]:
        names = {field.source_name for field in fields}
        by_source: dict[str, str] = {}
        for name, source_name in self._rules.rename_columns.items():
            if source_name not in names:
                raise TransferError(
                    f"rename_columns: the stream has no field {source_name!r}; "
                    f"stream fields: {', '.join(sorted(names))}"
                )

            by_source[source_name] = name

        return by_source

    @staticmethod
    def _check_types(
        declared: Mapping[str, str], columns: Sequence[ChStreamColumn]
    ) -> None:
        names = {column.name for column in columns}
        for name in declared:
            if name in names:
                continue

            raise TransferError(
                f"column_types: the stream has no column {name!r}; columns: "
                f"{', '.join(sorted(names))}"
            )

    @staticmethod
    def _plan(stream: Sequence[ChStreamColumn]) -> ChTablePlan:
        columns: list[ChPlannedColumn] = []
        for column in stream:
            columns.append(
                ChPlannedColumn(
                    name=column.name,
                    source_name=column.source_name,
                    type_text=column.type_text,
                    nullable=column.nullable,
                    known=column.known,
                )
            )

        return ChTablePlan(columns=tuple(columns))

    def _check(self, matches: Sequence[ChMatch]) -> SchemaCheck:
        columns: list[ColumnCheck] = []
        for match in matches:
            columns.append(match.check(self._type_rules))

        return SchemaCheck(columns=tuple(columns))


class ChTransfer(Protocol):
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
class ChTransferFactory(Protocol):
    """Конструктор пары: клиент приёмника, таблица, её кластер и ключ
    сортировки, кадр схемы, поток тел, журнал команд вызова."""

    def __call__(  # noqa: PLR0913 — ширина подписи задана контрактом
        self,
        client: AsyncClient,
        table: ChTableRef,
        placement: ChPlacement,
        head: SchemaHead,
        feed: TransferInbound,
        journal: CommandJournal,
    ) -> ChTransfer: ...


class ChTransfers:
    """Реестр пар в ClickHouse по entry points группы
    boba.transfer.clickhouse: имя записи — движок источника, значение —
    класс пары."""

    GROUP: ClassVar[str] = "boba.transfer.clickhouse"

    def __init__(self, factories: Mapping[Engine, ChTransferFactory]) -> None:
        self._factories = dict(factories)

    @classmethod
    def discover(cls) -> ChTransfers:
        factories: dict[Engine, ChTransferFactory] = {}
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
            if not isinstance(loaded, ChTransferFactory):
                raise TransferError(
                    f"entry point {entry.name!r} of group {cls.GROUP!r} "
                    f"({entry.value}): expected a transfer class, got {loaded!r}"
                )

            factories[engine] = loaded

        return cls(factories)

    def pair(self, engine: Engine) -> ChTransferFactory:
        factory = self._factories.get(engine)
        if factory is None:
            installed = ", ".join(sorted(member.value for member in self._factories))
            raise TransferError(
                f"no transfer from {engine.value} into clickhouse is installed "
                f"(installed: {installed or 'none'}); take wire arrow at the source"
            )

        return factory
