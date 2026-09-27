"""Пара ClickHouse -> ClickHouse: контракт из шапки TabSeparatedWithNamesAndTypes
источника как есть, сверка текстов типов с каталогом приёмника, DDL текстом
типа источника, тела TabSeparated в input() приёмника без перекодирования.

Ошибки:
TransferError — кадр схемы не от ClickHouse; правило rename_columns или
    column_types на несуществующую колонку; стратегия схемы отказала; база не
    Atomic или кластер неизвестен.
ClickHouseQueryError — сервер приёмника отклонил стейтмент или значение.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import ClassVar

from clickhouse_connect.driver.asyncclient import AsyncClient

from boba.db.clickhouse.target import ChPlacement, ChTableRef
from boba.db.clickhouse.transfer import (
    ChCatalogColumn,
    ChContract,
    ChContractTypes,
    ChInputSink,
    ChParsedType,
    ChPlannedColumn,
    ChTableFacts,
    ChTablePlan,
    ChTransferTable,
    ChTwin,
    ChTypeResolver,
    ChTypes,
    ClickHouseTransfer,
)
from boba.toolkit.sync import TypeFamily
from boba.toolkit.transfer import (
    ColumnRules,
    CreateTemplate,
    DeleteStrategyApply,
    Engine,
    InsertStrategyApply,
    SchemaCheck,
    SchemaHead,
    SchemaStrategyPlan,
    TransferError,
    TransferInbound,
    TransferReport,
    TransferRun,
    UnknownTypeApply,
)

__all__ = ["ChMatched", "ChMatcher", "ChStreamColumn", "ChToCh", "ChTypeRules"]

TypeRule = Callable[["ChParsedType", "ChParsedType"], "ColumnVerdict"]


class Verdict(StrEnum):
    """Итог сверки одной колонки."""

    OK = "ok"
    WARNING = "warning"
    ERROR = "error"


@dataclass(frozen=True)
class ColumnVerdict:
    level: Verdict
    message: str


@dataclass(frozen=True)
class ChStreamColumn:
    """Поле потока после правил приёмника: имя колонки таблицы, имя поля,
    текст типа ClickHouse с Nullable (свой из контракта или из
    column_types, нормализованный сервером приёмника)."""

    name: str
    source_name: str
    type_text: str

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
                Verdict.ERROR, "stream is nullable, table column is not"
            )

        if not source.nullable and target.nullable:
            return ColumnVerdict(
                Verdict.WARNING, "stream is not null, table column is nullable"
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

    def verdict(self, rules: ChTypeRules) -> ColumnVerdict:
        if self.stream is None:
            return ColumnVerdict(
                Verdict.ERROR, f"column {self.name}: in the table but not in the stream"
            )

        if self.table is None:
            return ColumnVerdict(
                Verdict.ERROR, f"column {self.name}: in the stream but not in the table"
            )

        verdict = rules.compare(self.stream, self.table)

        return ColumnVerdict(verdict.level, f"column {self.name}: {verdict.message}")


@dataclass(frozen=True)
class ChMatched:
    """Итог сверки: план таблицы, поток после правил и сверка для стратегии."""

    plan: ChTablePlan
    stream: Sequence[ChStreamColumn]
    check: SchemaCheck


class ChMatcher:
    """Правила приёмника поверх контракта источника: rename_columns даёт
    колонке таблицы поле потока, column_types — тип текстом (Nullable по
    полю потока); затем сверка с каталогом по ChTypeRules и план таблицы в
    порядке полей потока."""

    def __init__(self, rules: ColumnRules) -> None:
        self._rules = rules
        self._types = ChTypes()
        self._type_rules = ChTypeRules()

    def stream(
        self, contract: ChContract, declared: Mapping[str, str]
    ) -> list[ChStreamColumn]:
        by_source = self._by_source(contract)
        columns: list[ChStreamColumn] = []
        for column in contract.columns:
            name = by_source.get(column.name, column.name)
            type_text = column.type_text
            if name in declared:
                type_text = self._declared(declared[name], column.type_text)

            columns.append(
                ChStreamColumn(name=name, source_name=column.name, type_text=type_text)
            )

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

    def _declared(self, declared: str, stream_type: str) -> str:
        """Объявленный тип с Nullable, если поле потока nullable."""
        if self._types.parse(stream_type).nullable:
            return self._types.as_nullable(declared)

        return declared

    def _by_source(self, contract: ChContract) -> dict[str, str]:
        names = set(contract.names())
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
                    nullable=False,
                    known=column.type_text,
                )
            )

        return ChTablePlan(columns=tuple(columns))

    def _check(self, matches: Sequence[ChMatch]) -> SchemaCheck:
        errors: list[str] = []
        warnings: list[str] = []
        lines: list[str] = []
        for match in matches:
            verdict = match.verdict(self._type_rules)
            lines.append(f"- {verdict.level.value} {verdict.message}")
            if verdict.level is Verdict.ERROR:
                errors.append(verdict.message)

            if verdict.level is Verdict.WARNING:
                warnings.append(verdict.message)

        return SchemaCheck(
            errors=tuple(errors), warnings=tuple(warnings), lines=tuple(lines)
        )


class ChToCh(ClickHouseTransfer):
    """Реализация ClickHouseTransfer для источника ClickHouse: база обязана
    быть Atomic; column_types нормализует сервер приёмника, каталог таблицы
    сверяется по текстам типов, стратегии идут через TransferRun с загрузкой
    TabSeparated в двойник через input() и exchange tables."""

    FORMAT: ClassVar[str] = "TabSeparated"

    def __init__(
        self,
        client: AsyncClient,
        table: ChTableRef,
        placement: ChPlacement,
        head: SchemaHead,
        feed: TransferInbound,
    ) -> None:
        if head.source_engine is not Engine.CLICKHOUSE:
            raise TransferError(
                f"transfer clickhouse -> clickhouse got a schema frame from "
                f"{head.source_engine.value}"
            )

        self._client = client
        self._table = table
        self._placement = placement
        self._contract = ChContract.model_validate(head.contract)
        self._feed = feed
        self._facts = ChTableFacts(client, table)
        self._resolver = ChTypeResolver(client)

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
        matcher = ChMatcher(rules)
        declared = await self._resolver.resolve(rules.column_types)
        stream = matcher.stream(self._contract, declared)
        exists = await self._facts.exists()
        catalog: Sequence[ChCatalogColumn] = ()
        if exists:
            catalog = await self._facts.columns()

        matched = matcher.match(stream, catalog)
        twin = ChTwin(self._client, self._table, self._placement.cluster)
        table = ChTransferTable(
            self._client, self._table, matched.plan, create_table, twin, self._placement
        )
        sink = ChInputSink(
            self._client,
            twin,
            matched.plan,
            self.FORMAT,
            ChContractTypes(self._stream_types(matched.stream)),
            self._feed,
        )
        run = TransferRun(
            schema_strategy, delete_strategy, insert_strategy, unknown_types
        )

        return await run.run(
            self._table.text(), exists, matched.check, table, sink, False
        )

    @staticmethod
    def _stream_types(stream: Sequence[ChStreamColumn]) -> dict[str, str]:
        types: dict[str, str] = {}
        for column in stream:
            types[column.name] = column.type_text

        return types
