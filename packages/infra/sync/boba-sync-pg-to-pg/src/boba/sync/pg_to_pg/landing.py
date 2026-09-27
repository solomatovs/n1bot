"""Пара postgres -> postgres: контракт RowDescription источника как есть,
сверка с таблицей приёмника по OID и typmod, DDL текстом типа источника,
тела COPY csv или text в COPY приёмника без перекодирования.

Ошибки:
LandingError — кадр схемы не от postgres; правило rename_columns или
    column_types на несуществующую колонку; стратегия схемы отказала;
    колонка без типа при fail_on_unknown.
psycopg.Error — сервер приёмника отклонил стейтмент или значение.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

import psycopg
from psycopg._typeinfo import TypeInfo, TypesRegistry

from boba.db.postgres.describe import PgDescribedColumn, PgTypeText, PgTypmod
from boba.db.postgres.landing import (
    PgCatalogColumn,
    PgContract,
    PgCopyIn,
    PgLandingTable,
    PgPlannedColumn,
    PgSourceColumn,
    PgTableFacts,
    PgTablePlan,
    PgTableRef,
    PgTypeResolver,
    PostgresLanding,
)
from boba.toolkit.landing import (
    ColumnRules,
    DeleteStrategyApply,
    InsertStrategyApply,
    LandingError,
    LandingInbound,
    LandingReport,
    LandingRun,
    SchemaCheck,
    SchemaHead,
    SchemaStrategyPlan,
    SourceEngine,
    UnknownTypeApply,
)

__all__ = ["PgMatcher", "PgToPg", "PgTypeRules"]


class Verdict(StrEnum):
    """Итог сверки колонки: ошибка не даёт загрузить без пересоздания."""

    OK = "ok"
    WARNING = "warning"
    ERROR = "error"


@dataclass(frozen=True)
class ColumnVerdict:
    level: Verdict
    message: str


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
        table_text = self._table_text(table)
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
                Verdict.ERROR, "stream is nullable, table column is not null"
            )

        if not stream.nullable and not table.not_null:
            return ColumnVerdict(
                Verdict.WARNING, "stream is not null, table column is nullable"
            )

        return ColumnVerdict(Verdict.OK, "ok")

    def _table_text(self, table: PgCatalogColumn) -> str:
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

    def verdict(self, rules: PgTypeRules) -> ColumnVerdict:
        if self.stream is None:
            return ColumnVerdict(
                Verdict.ERROR, f"column {self.name}: in the table but not in the stream"
            )

        if self.table is None:
            return ColumnVerdict(
                Verdict.ERROR, f"column {self.name}: in the stream but not in the table"
            )

        return rules.compare(self.stream, self.table)


@dataclass(frozen=True)
class PgMatched:
    """Итог сверки: план таблицы по потоку и вердикты для стратегии."""

    plan: PgTablePlan
    check: SchemaCheck


class PgMatcher:
    """Сведение контракта, правил приёмника и колонок таблицы по имени
    колонки приёмника: rename переименовывает поле потока, column_types
    подменяет его тип разобранным сервером, дальше каждая колонка получает
    вердикт, а поток — план таблицы в своём порядке."""

    def __init__(self, registry: TypesRegistry, rules: ColumnRules) -> None:
        self._registry = registry
        self._rules = rules
        self._type_rules = PgTypeRules(registry)

    def resolve_text_types(self, contract: PgContract) -> dict[str, str]:
        """Merge информации о колонках,
        которая досталась нам от источника и была передана сюда, на приемник

        Вовзращает словарь: {"колонка приёмника": "текст типа"}
        Например:
        ```python
        {
           "amount": "numeric(20,6)",
            "v": "pump_landing.mood",
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
        for name, column in self._renamed(contract):
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
        #   "v": "pump_landing.mood",
        # }.
        return texts

    def match(
        self,
        contract: PgContract,
        catalog: Sequence[PgCatalogColumn],
        resolved: Mapping[str, PgDescribedColumn],
    ) -> PgMatched:
        renamed = self._renamed(contract)
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
                    type_text=stream.type_text,
                    nullable=stream.nullable,
                    known=stream.known,
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

            raise LandingError(
                f"column_types: neither the stream nor the table has a column "
                f"{name!r}; columns: {', '.join(sorted(seen))}"
            )

        return PgMatched(
            plan=PgTablePlan(columns=tuple(planned)), check=self._check(matches)
        )

    def _stream_column(
        self,
        name: str,
        column: PgSourceColumn,
        declared: PgDescribedColumn | None,
    ) -> PgStreamColumn:
        if declared is None:
            return PgStreamColumn(
                name=name,
                oid=column.oid,
                typmod=column.typmod,
                type_text=column.type_text,
                known=column.known(),
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

    def _renamed(self, contract: PgContract) -> list[tuple[str, PgSourceColumn]]:
        by_source: dict[str, str] = {}
        for name, rename_from in self._rules.rename_columns.items():
            by_source[rename_from] = name

        names = {column.name for column in contract.columns}
        for source_name in by_source:
            if source_name not in names:
                raise LandingError(
                    f"rename_columns: the stream has no field {source_name!r}; "
                    f"stream fields: {', '.join(sorted(names))}"
                )

        renamed: list[tuple[str, PgSourceColumn]] = []
        for column in contract.columns:
            renamed.append((by_source.get(column.name, column.name), column))

        return renamed

    def _check(self, matches: Sequence[PgMatch]) -> SchemaCheck:
        errors: list[str] = []
        warnings: list[str] = []
        lines: list[str] = []
        for match in matches:
            verdict = match.verdict(self._type_rules)
            lines.append(f"- {verdict.level.value} {match.name}: {verdict.message}")
            if verdict.level is Verdict.ERROR:
                errors.append(f"{match.name}: {verdict.message}")

            if verdict.level is Verdict.WARNING:
                warnings.append(f"{match.name}: {verdict.message}")

        return SchemaCheck(
            errors=tuple(errors), warnings=tuple(warnings), lines=tuple(lines)
        )


class PgToPg(PostgresLanding):
    """Реализация PostgresLanding для источника postgres: одной транзакцией
    приёмника разбирает rules.column_types своим сервером, читает каталог
    таблицы, сверяет, ведёт стратегии через LandingRun и кладёт тела COPY в
    таблицу как есть."""

    def __init__(
        self,
        conn: psycopg.AsyncConnection[Any],
        table: PgTableRef,
        head: SchemaHead,
        feed: LandingInbound,
    ) -> None:
        if head.source_engine is not SourceEngine.POSTGRES:
            raise LandingError(
                f"landing postgres -> postgres got a schema frame from "
                f"{head.source_engine.value}"
            )

        self._conn = conn
        self._table = table
        self._wire = head.wire
        self._contract = PgContract.model_validate(head.contract)
        self._feed = feed
        self._facts = PgTableFacts(conn, table)
        self._resolver = PgTypeResolver(conn)

    async def run(
        self,
        schema_strategy: SchemaStrategyPlan,
        delete_strategy: DeleteStrategyApply,
        insert_strategy: InsertStrategyApply,
        unknown_types: UnknownTypeApply,
        rules: ColumnRules,
    ) -> LandingReport:
        matcher = PgMatcher(self._conn.adapters.types, rules)
        async with self._conn.transaction():
            resolved = await self._resolver.resolve(
                matcher.resolve_text_types(self._contract)
            )
            exists = await self._facts.exists()
            catalog: Sequence[PgCatalogColumn] = ()
            if exists:
                catalog = await self._facts.columns()

            matched = matcher.match(self._contract, catalog, resolved)
            table = PgLandingTable(self._conn, self._table, matched.plan)
            sink = PgCopyIn(
                self._conn,
                self._table,
                matched.plan.names(),
                self._wire,
                self._feed.bodies(),
            )
            run = LandingRun(
                schema_strategy, delete_strategy, insert_strategy, unknown_types
            )

            return await run.run(
                self._table.text(), exists, matched.check, table, sink, True
            )
