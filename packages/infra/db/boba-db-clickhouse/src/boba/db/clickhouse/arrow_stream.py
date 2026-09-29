"""Поток arrow для ClickHouse в обе стороны.

Источник ChArrowSource: ответ запроса в FORMAT ArrowStream читается pyarrow
только ради схемы и контракта, пачки перекладываются в кадры потока тем же
IPC без разбора значений; декларации вызова ложатся поверх типов Arrow.

Приёмник ChArrowLoader для нейтрального контракта любого движка:
    - семейства, которых у ClickHouse нет (json, inet, interval, money, xml,
      bit, bytea), приёмник видит строками — так же, как создаёт колонку;
    - колонки таблицы из system.columns, сверка по семействам;
    - DDL по шаблону вызывающего с переменной {database};
    - загрузка в двойник <table>__ex через input() — сервер сопоставляет
      поля потока по именам, select переименовывает их в колонки таблицы;
    - exchange tables меняет таблицу и двойник местами.

Ошибки:
ContractError — декларация на колонку, которой нет в ответе; правило вызова не
    сходится со схемами.
TransferError — база не Atomic; стратегия схемы отказала; тип из
    rules.column_types сервер не знает; колонка без типа при fail_on_unknown.
ClickHouseQueryError — сервер отклонил запрос или ответ оборвался.
ArrowStreamError — ответ сервера не читается как поток Arrow IPC.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import ClassVar

from clickhouse_connect.driver.asyncclient import AsyncClient

from boba.db.clickhouse.payload import PayloadClickHouse, ReadTuning
from boba.db.clickhouse.target import ChCluster, ChPlacement, ChTableRef
from boba.db.clickhouse.trace import ChCommandReport
from boba.db.clickhouse.transfer import (
    ChInputSink,
    ChNullableTwinTypes,
    ChPlannedColumn,
    ChTableFacts,
    ChTablePlan,
    ChTransferTable,
    ChTwin,
    ChTypeResolver,
    ChTypes,
)
from boba.toolkit.arrow import ArrowColumns, ArrowIpc, BytePipe
from boba.toolkit.contract import (
    ArrowContract,
    ColumnDeclaration,
    ColumnSpec,
    ColumnType,
    ContractTable,
    Declarations,
    DeclaredType,
    SchemaMatcher,
    StreamContract,
    TableColumn,
    TableSpec,
    TimeUnit,
    TypeFamily,
)
from boba.toolkit.contract import Engine as NeutralEngine
from boba.toolkit.transfer import (
    ColumnRules,
    CreateTemplate,
    DeleteStrategyApply,
    Engine,
    InsertStrategyApply,
    SchemaHead,
    SchemaStrategyPlan,
    StreamWire,
    TransferInbound,
    TransferOutbound,
    TransferReport,
    TransferRun,
    UnknownTypeApply,
)

__all__ = [
    "ChArrowLoader",
    "ChArrowSource",
    "ChDdlTypes",
    "ChNeutralFacts",
    "ChStreamProjection",
]


class ChArrowSource:
    """Источник sync-потока ClickHouse: всегда Arrow IPC — контракт из схемы
    ответа сервера, тела кадров — те же пачки, переписанные писателем IPC."""

    ENGINE: ClassVar[Engine] = Engine.CLICKHOUSE
    FORMAT: ClassVar[str] = "ArrowStream"

    def __init__(self, client: AsyncClient) -> None:
        self._client = client
        self._payload = PayloadClickHouse
        self._ipc = ArrowIpc()
        self._columns = ArrowColumns()
        self._declarations = Declarations()
        self._contract = StreamContract()
        self._contract_table = ContractTable()

    async def stream(
        self,
        text: str,
        declared: Sequence[ColumnDeclaration],
        chunk_bytes: int,
        out: TransferOutbound,
    ) -> ChCommandReport:
        pipe = BytePipe()
        tuning = ReadTuning(socket_read_size=chunk_bytes, read_buffer_size=chunk_bytes)
        async with self._payload.byte_stream_out(
            self._client, text, self.FORMAT, tuning=tuning
        ) as stream:

            async def produce() -> None:
                try:
                    async for block in stream.blocks:
                        await pipe.write(block)
                finally:
                    pipe.close_write()

            async def consume() -> tuple[ColumnSpec, ...]:
                try:
                    reader = await self._ipc.open_in(pipe.source, chunk_bytes)
                    specs = self._declarations.merge(
                        self._columns.specs(reader.schema), declared
                    )
                    contract = ArrowContract(columns=self._contract.columns(specs))
                    await out.schema(
                        SchemaHead(
                            kind="schema",
                            source_engine=self.ENGINE,
                            wire=StreamWire.ARROW,
                            contract=contract.model_dump(mode="json"),
                        )
                    )
                    writer = await self._ipc.open_out(out.writer(), reader.schema)
                    async for batch in reader.batches:
                        await writer.write(batch)

                    await writer.close()

                    return specs
                finally:
                    pipe.close_read()

            _, specs = await asyncio.gather(produce(), consume())

            return stream.trace.report(
                self._contract_table.caption(StreamWire.ARROW.value, specs),
                text,
                columns=self._contract_table.rows(specs),
            )


class ChStreamProjection:
    """Поле нейтрального потока так, как его хранит ClickHouse: семейства,
    для которых у ClickHouse нет своего типа, становятся строками (json,
    inet, interval, money, xml, bit, bytea). Так же приёмник их и создаёт,
    поэтому повторная загрузка сверяет строку со строкой."""

    AS_STRING: ClassVar[frozenset[TypeFamily]] = frozenset(
        {
            TypeFamily.JSON,
            TypeFamily.NETWORK,
            TypeFamily.INTERVAL,
            TypeFamily.MONEY,
            TypeFamily.XML,
            TypeFamily.BIT,
            TypeFamily.BINARY,
        }
    )

    def project(self, columns: Sequence[ColumnSpec]) -> tuple[ColumnSpec, ...]:
        projected: list[ColumnSpec] = []
        for column in columns:
            projected.append(self._column(column))

        return tuple(projected)

    def _column(self, column: ColumnSpec) -> ColumnSpec:
        if column.kind.family not in self.AS_STRING:
            return column

        kind = ColumnType(TypeFamily.STRING, column.kind.text)

        return replace(column, kind=kind, char_length=0)


class ChDdlTypes:
    """Текст типа ClickHouse для колонки нейтрального плана: ddl_type из
    правил приёмника; текст типа источника, если источник — ClickHouse и тип
    назван; иначе по нейтральному типу (Int64, Decimal(p, s), String,
    Date32, DateTime64(6, 'UTC'), UUID ...). Nullable решает поток: nullable
    колонка получает Nullable(...) и у объявленного типа тоже. Тип без пары
    у ClickHouse — пустой текст, дальше решает стратегия неизвестных типов
    в ChTransferTable."""

    SIGNED: ClassVar[Mapping[int, str]] = {
        8: "Int8",
        16: "Int16",
        32: "Int32",
        64: "Int64",
        128: "Int128",
        256: "Int256",
    }
    UNSIGNED: ClassVar[Mapping[int, str]] = {
        8: "UInt8",
        16: "UInt16",
        32: "UInt32",
        64: "UInt64",
        128: "UInt128",
        256: "UInt256",
    }
    FLOATS: ClassVar[Mapping[int, str]] = {32: "Float32", 64: "Float64"}
    PRECISION: ClassVar[Mapping[TimeUnit, int]] = {
        TimeUnit.SECOND: 0,
        TimeUnit.MILLISECOND: 3,
        TimeUnit.MICROSECOND: 6,
        TimeUnit.NANOSECOND: 9,
    }
    PLAIN: ClassVar[Mapping[TypeFamily, str]] = {
        TypeFamily.BOOLEAN: "Bool",
        TypeFamily.STRING: "String",
        TypeFamily.DATE: "Date32",
        TypeFamily.UUID: "UUID",
    }
    DECIMAL_DIGITS: ClassVar[int] = 76
    ZONE: ClassVar[str] = "UTC"

    def __init__(self) -> None:
        self._types = ChTypes()

    def render(self, column: TableColumn, source_engine: NeutralEngine) -> str:
        """Текст типа с Nullable по потоку; пусто — пары у ClickHouse нет."""
        inner = self._named(column, source_engine)
        if not inner:
            inner = self._inner(column.kind)

        if not inner:
            return ""

        if column.nullable:
            return self._types.as_nullable(inner)

        return inner

    def known(self, column: TableColumn) -> str:
        """Что известно о типе колонки, для текста отказа."""
        if column.source_type:
            return f"{column.kind.text} [{column.source_type}]"

        return column.kind.text

    @staticmethod
    def _named(column: TableColumn, source_engine: NeutralEngine) -> str:
        """Тип, названный текстом: правилом приёмника или источником того же
        движка; пусто — выводить из нейтрального типа."""
        if column.ddl_type:
            return column.ddl_type

        if source_engine is NeutralEngine.CLICKHOUSE and column.source_type:
            return column.source_type

        return ""

    def _inner(self, kind: ColumnType) -> str:
        """Тип ClickHouse по нейтральному; пусто — пары нет."""
        if kind.family is TypeFamily.INTEGER:
            return self._integer(kind)

        if kind.family is TypeFamily.FLOAT:
            return self.FLOATS.get(kind.bits, "Float64")

        if kind.family is TypeFamily.DECIMAL:
            return self._decimal(kind)

        if kind.family is TypeFamily.TIMESTAMP:
            return self._timestamp(kind)

        return self.PLAIN.get(kind.family, "")

    def _integer(self, kind: ColumnType) -> str:
        if kind.unsigned:
            return self.UNSIGNED.get(kind.bits, "")

        return self.SIGNED.get(kind.bits, "")

    def _decimal(self, kind: ColumnType) -> str:
        if kind.precision == 0:
            return ""

        if kind.precision > self.DECIMAL_DIGITS:
            return ""

        return f"Decimal({kind.precision}, {kind.scale})"

    def _timestamp(self, kind: ColumnType) -> str:
        precision = self.PRECISION[kind.unit]
        if kind.zoned:
            return f"DateTime64({precision}, '{self.ZONE}')"

        return f"DateTime64({precision})"


class ChDeclaredTypes:
    """Типы из rules.column_types в нейтральном виде: текст нормализует сервер
    приёмника через ChTypeResolver, семейство и параметры — ChTypes."""

    def __init__(self, client: AsyncClient) -> None:
        self._resolver = ChTypeResolver(client)
        self._types = ChTypes()

    async def resolve(self, column_types: Mapping[str, str]) -> dict[str, DeclaredType]:
        resolved = await self._resolver.resolve(column_types)
        declared: dict[str, DeclaredType] = {}
        for name, text in resolved.items():
            parsed = self._types.parse(text)
            declared[name] = DeclaredType(
                kind=parsed.kind,
                source_type=parsed.text,
                char_length=parsed.char_length,
            )

        return declared


class ChNeutralFacts:
    """Колонки таблицы приёмника из system.columns в нейтральном виде."""

    def __init__(self, client: AsyncClient, table: ChTableRef) -> None:
        self._facts = ChTableFacts(client, table)
        self._types = ChTypes()

    async def require_atomic(self) -> None:
        await self._facts.require_atomic()

    async def require_cluster(self, cluster: ChCluster) -> None:
        await self._facts.require_cluster(cluster)

    async def exists(self) -> bool:
        return await self._facts.exists()

    async def columns(self) -> tuple[ColumnSpec, ...]:
        specs: list[ColumnSpec] = []
        for column in await self._facts.columns():
            parsed = self._types.parse(column.type_text)
            specs.append(
                ColumnSpec(
                    name=column.name,
                    kind=parsed.kind,
                    nullable=parsed.nullable,
                    position=column.position,
                    source_type=parsed.text,
                    char_length=parsed.char_length,
                )
            )

        return tuple(specs)


class ChArrowLoader:
    """Приёмник потока arrow в ClickHouse: база обязана быть Atomic;
    нейтральный контракт потока в представлении ClickHouse -> колонки
    каталога -> сверка по семействам -> план таблицы текстами типов ClickHouse
    -> ход стратегий TransferRun с загрузкой через двойник. Транзакций нет,
    атомарность даёт exchange tables."""

    FORMAT: ClassVar[str] = "ArrowStream"

    def __init__(  # noqa: PLR0913
        self,
        client: AsyncClient,
        table: ChTableRef,
        placement: ChPlacement,
        contract: Sequence[ColumnSpec],
        source_engine: NeutralEngine,
        inbound: TransferInbound,
    ) -> None:
        self._client = client
        self._table = table
        self._placement = placement
        self._stream = ChStreamProjection().project(contract)
        self._source_engine = source_engine
        self._exact = source_engine is NeutralEngine.CLICKHOUSE
        self._inbound = inbound
        self._facts = ChNeutralFacts(client, table)
        self._declared = ChDeclaredTypes(client)
        self._ddl_types = ChDdlTypes()

    def _plan(self, spec: TableSpec) -> ChTablePlan:
        """План таблицы текстами типов ClickHouse; у колонки без пары текст
        пуст, её судьбу решает стратегия неизвестных типов при создании."""
        columns: list[ChPlannedColumn] = []
        for column in spec.columns:
            columns.append(
                ChPlannedColumn(
                    name=column.name,
                    source_name=column.source_name,
                    type_text=self._ddl_types.render(column, self._source_engine),
                    nullable=column.nullable,
                    known=self._ddl_types.known(column),
                )
            )

        return ChTablePlan(columns=tuple(columns))

    @staticmethod
    def _nullable(spec: TableSpec) -> list[str]:
        names: list[str] = []
        for column in spec.columns:
            if column.nullable:
                names.append(column.name)

        return names

    async def run(  # noqa: PLR0913
        self,
        schema_strategy: SchemaStrategyPlan,
        delete_strategy: DeleteStrategyApply,
        insert_strategy: InsertStrategyApply,
        unknown_types: UnknownTypeApply,
        rules: ColumnRules,
        create_table: CreateTemplate,
    ) -> TransferReport:
        await self._facts.require_atomic()
        await self._facts.require_cluster(self._placement.cluster)
        declared = await self._declared.resolve(rules.column_types)
        exists = await self._facts.exists()
        facts: Sequence[ColumnSpec] = ()
        if exists:
            facts = await self._facts.columns()

        diff = SchemaMatcher(rules, self._exact).diff(self._stream, facts, declared)
        spec = diff.table_spec()
        twin = ChTwin(self._client, self._table, self._placement.cluster)
        plan = self._plan(spec)
        table = ChTransferTable(
            self._client, self._table, plan, create_table, twin, self._placement
        )
        sink = ChInputSink(
            self._client,
            twin,
            plan,
            self.FORMAT,
            ChNullableTwinTypes(self._nullable(spec)),
            self._inbound,
        )
        transfer = TransferRun(
            schema_strategy, delete_strategy, insert_strategy, unknown_types
        )

        return await transfer.run(
            self._table.text(), exists, diff.check(), table, sink, False
        )
