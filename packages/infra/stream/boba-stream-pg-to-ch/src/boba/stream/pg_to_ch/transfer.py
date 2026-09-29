"""Пара postgres -> ClickHouse: контракт RowDescription источника (OID и
typmod) переводится в типы ClickHouse, сверка с каталогом и план — общими
ChMatcher и ChTypeRules стороны ClickHouse, тела COPY text идут в input()
двойника как TabSeparated без перекодирования: экранирование и \\N у
форматов совпадают, разницу текстов значений закрывают настройки чтения.

Ошибки:
TransferError — кадр схемы не от postgres; правило rename_columns или
    column_types на несуществующую колонку; стратегия схемы отказала; тип
    без пары у ClickHouse при fail_on_unknown; база не Atomic или кластер
    неизвестен.
ClickHouseQueryError — сервер приёмника отклонил стейтмент или значение
    (маска в inet, дата до нашей эры, NaN в Decimal, Decimal шире колонки).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any, ClassVar

import psycopg.postgres
from clickhouse_connect.driver.asyncclient import AsyncClient
from psycopg._typeinfo import TypeInfo, TypesRegistry

from boba.db.clickhouse.target import ChPlacement, ChTableRef
from boba.db.clickhouse.transfer import (
    ChCatalogColumn,
    ChContractTypes,
    ChInetProbe,
    ChInputSink,
    ChJsonProbe,
    ChJsonSupport,
    ChMatcher,
    ChStreamColumn,
    ChTableFacts,
    ChTransfer,
    ChTransferTable,
    ChTwin,
    ChTypeResolver,
    ChTypes,
)
from boba.db.postgres.describe import PgTypmod
from boba.db.postgres.transfer import PgContract, PgSourceColumn
from boba.toolkit.transfer import (
    ColumnRules,
    CommandJournal,
    CreateTemplate,
    DeleteStrategyApply,
    Engine,
    InsertStrategyApply,
    SchemaHead,
    SchemaStrategyPlan,
    TransferError,
    TransferInbound,
    TransferReport,
    TransferRun,
    UnknownTypeApply,
)

__all__ = ["PgChTypes", "PgToCh"]


class PgChTypes:
    """Тип ClickHouse для колонки контракта postgres по OID и typmod из
    встроенного реестра psycopg. Пусто — у ClickHouse нет типа, который
    прочитает текст COPY без потерь: массивы (литерал {a,b}), numeric без
    точности или шире 76 разрядов, пользовательские типы и расширения (у
    источника только OID). Nullable потока — Nullable(...) снаружи.

    Что и как читается:
        - bool печатается t и f — настройки чтения READ_SETTINGS;
        - timestamptz печатается с +00 (сессия COPY в UTC) — best_effort;
        - json и jsonb — JSON, если сервер его умеет (по пробе), у nullable
          колонки нужен Nullable(JSON), иначе String; в JSON читаются
          только объекты, массив или скаляр на верхнем уровне — ошибка
          сервера;
        - inet — IPv6 (IPv4 хранится как ::ffff:a.b.c.d), если сервер
          читает текст IPv4 в IPv6 (по пробе, старые не умеют), иначе
          String; значение с маской — ошибка сервера; cidr и macaddr —
          String;
        - date и timestamp вне 1900–2299 сервер молча прижимает к границе,
          даты до нашей эры — ошибка сервера."""

    PLAIN: ClassVar[Mapping[str, str]] = {
        "int2": "Int16",
        "int4": "Int32",
        "int8": "Int64",
        "oid": "UInt32",
        "float4": "Float32",
        "float8": "Float64",
        "bool": "Bool",
        "text": "String",
        "varchar": "String",
        "bpchar": "String",
        "name": "String",
        "char": "String",
        "bytea": "String",
        "date": "Date32",
        "uuid": "UUID",
        "cidr": "String",
        "macaddr": "String",
        "macaddr8": "String",
        "money": "String",
        "bit": "String",
        "varbit": "String",
        "xml": "String",
        "time": "String",
        "timetz": "String",
        "interval": "String",
        "tsvector": "String",
        "tsquery": "String",
        "jsonpath": "String",
        "point": "String",
        "line": "String",
        "lseg": "String",
        "box": "String",
        "path": "String",
        "polygon": "String",
        "circle": "String",
        "int4range": "String",
        "int8range": "String",
        "numrange": "String",
        "tsrange": "String",
        "tstzrange": "String",
        "daterange": "String",
    }
    INET: ClassVar[str] = "inet"
    IPV6: ClassVar[str] = "IPv6"
    NUMERIC: ClassVar[str] = "numeric"
    TIMESTAMP: ClassVar[str] = "timestamp"
    TIMESTAMPTZ: ClassVar[str] = "timestamptz"
    DECIMAL_DIGITS: ClassVar[int] = 76
    STRING: ClassVar[str] = "String"
    ZONE: ClassVar[str] = "UTC"
    READ_SETTINGS: ClassVar[Mapping[str, Any]] = {
        "bool_true_representation": "t",
        "bool_false_representation": "f",
        "date_time_input_format": "best_effort",
    }
    """Настройки чтения текста COPY при вставке."""

    def __init__(
        self, registry: TypesRegistry, json: ChJsonSupport, inet_as_ipv6: bool
    ) -> None:
        self._registry = registry
        self._json = json
        self._inet_as_ipv6 = inet_as_ipv6
        self._typmod = PgTypmod()
        self._types = ChTypes()

    def render(self, column: PgSourceColumn) -> str:
        """Текст типа ClickHouse с Nullable по потоку; пусто — пары нет."""
        inner = self._inner(column)
        if not inner:
            return ""

        if column.nullable:
            return self._types.as_nullable(inner)

        return inner

    def _inner(self, column: PgSourceColumn) -> str:
        info: TypeInfo | None = self._registry.get(column.oid)
        if info is None:
            return ""

        if info.array_oid == column.oid:
            return ""

        parametric: Mapping[str, Callable[[PgSourceColumn], str]] = {
            "json": self._json_type,
            "jsonb": self._json_type,
            self.INET: self._inet,
            self.NUMERIC: self._decimal,
            self.TIMESTAMP: self._timestamp,
            self.TIMESTAMPTZ: self._timestamptz,
        }
        render = parametric.get(info.name)
        if render is not None:
            return render(column)

        return self.PLAIN.get(info.name, "")

    def _timestamp(self, column: PgSourceColumn) -> str:
        return f"DateTime64({self._typmod.precision(column.typmod)})"

    def _timestamptz(self, column: PgSourceColumn) -> str:
        precision = self._typmod.precision(column.typmod)

        return f"DateTime64({precision}, '{self.ZONE}')"

    def _inet(self, column: PgSourceColumn) -> str:
        if self._inet_as_ipv6:
            return self.IPV6

        return self.STRING

    def _json_type(self, column: PgSourceColumn) -> str:
        if not self._json.available:
            return self.STRING

        if column.nullable and not self._json.nullable:
            return self.STRING

        return self._json.type_text

    def _decimal(self, column: PgSourceColumn) -> str:
        if self._typmod.unbounded(column.typmod):
            return ""

        digits = self._typmod.digits(column.typmod)
        if digits.precision > self.DECIMAL_DIGITS:
            return ""

        return f"Decimal({digits.precision}, {digits.scale})"


class PgToCh(ChTransfer):
    """Реализация ChTransfer для источника postgres: база обязана
    быть Atomic; тип JSON пробуется на сервере приёмника, контракт
    переводится PgChTypes, column_types нормализует сервер приёмника,
    сверка и план — ChMatcher, стратегии — TransferRun с загрузкой
    TabSeparated в двойник через input() и exchange tables."""

    FORMAT: ClassVar[str] = "TabSeparated"

    def __init__(
        self,
        client: AsyncClient,
        table: ChTableRef,
        placement: ChPlacement,
        head: SchemaHead,
        feed: TransferInbound,
        journal: CommandJournal,
    ) -> None:
        if head.source_engine is not Engine.POSTGRES:
            raise TransferError(
                f"transfer postgres -> clickhouse got a schema frame from "
                f"{head.source_engine.value}"
            )

        self._client = client
        self._table = table
        self._placement = placement
        self._contract = PgContract.model_validate(head.contract)
        self._feed = feed
        self._journal = journal
        self._facts = ChTableFacts(client, table, journal)
        self._resolver = ChTypeResolver(client, journal)
        self._json_probe = ChJsonProbe(client, journal)
        self._inet_probe = ChInetProbe(client, journal)
        self._registry = psycopg.postgres.types

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
        json = await self._json_probe.probe()
        inet_as_ipv6 = await self._inet_probe.accepts_ipv4()
        types = PgChTypes(self._registry, json, inet_as_ipv6)
        matcher = ChMatcher(rules)
        declared = await self._resolver.resolve(rules.column_types)
        stream = matcher.stream(self._fields(types), declared)
        exists = await self._facts.exists()
        catalog: Sequence[ChCatalogColumn] = ()
        if exists:
            catalog = await self._facts.columns()

        matched = matcher.match(stream, catalog)
        twin = ChTwin(
            self._client,
            self._table,
            self._placement.cluster,
            self._journal,
            json.settings,
        )
        table = ChTransferTable(
            self._client,
            self._table,
            matched.plan,
            create_table,
            twin,
            self._placement,
            self._journal,
            json.settings,
        )
        sink = ChInputSink(
            self._client,
            twin,
            matched.plan,
            self.FORMAT,
            ChContractTypes(self._stream_types(matched.stream)),
            self._feed,
            self._journal,
            {**json.settings, **PgChTypes.READ_SETTINGS},
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
            matched.check,
            matched.plan.planned(),
            table,
            sink,
            False,
        )

    def _fields(self, types: PgChTypes) -> list[ChStreamColumn]:
        fields: list[ChStreamColumn] = []
        for column in self._contract.columns:
            fields.append(
                ChStreamColumn(
                    name=column.name,
                    source_name=column.name,
                    type_text=types.render(column),
                    nullable=column.nullable,
                    known=column.known(),
                )
            )

        return fields

    @staticmethod
    def _stream_types(stream: Sequence[ChStreamColumn]) -> dict[str, str]:
        """Типы полей потока для структуры input(): без пары у ClickHouse
        поле читается строкой — тем же типом, что даёт fallback_as_varchar."""
        types: dict[str, str] = {}
        for column in stream:
            text = column.type_text
            if not text:
                text = PgChTypes.STRING
                if column.nullable:
                    text = ChTypes().as_nullable(text)

            types[column.name] = text

        return types
