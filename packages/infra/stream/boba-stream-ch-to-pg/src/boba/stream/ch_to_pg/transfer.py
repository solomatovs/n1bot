"""Пара ClickHouse -> postgres: контракт из шапки TabSeparatedWithNamesAndTypes
источника переводится в текст типа postgres, приёмник разбирает его у себя
(select null::<тип>), сверка и план — общими PgMatcher и PgTypeRules стороны
postgres, тела TabSeparated идут в COPY text одной транзакцией без
перекодирования: экранирование и \\N у форматов совпадают.

Ошибки:
TransferError — кадр схемы не от ClickHouse; правило rename_columns или
    column_types на несуществующую колонку; стратегия схемы отказала; тип
    без пары у postgres при fail_on_unknown; тип не известен серверу
    приёмника.
psycopg.Error — сервер приёмника отклонил стейтмент или значение
    (массив в формате ClickHouse, NUL байт FixedString в text).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any, ClassVar

import psycopg

from boba.db.clickhouse.contract import ChContract, ChParsedType, ChTypes
from boba.db.postgres.transfer import (
    PgCatalogColumn,
    PgCopyIn,
    PgCopyLayout,
    PgField,
    PgMatcher,
    PgTableFacts,
    PgTableRef,
    PgTransfer,
    PgTransferTable,
    PgTypeResolver,
)
from boba.toolkit.contract import TypeFamily
from boba.toolkit.transfer import (
    ColumnRules,
    CommandJournal,
    CreateTemplate,
    DeleteStrategyApply,
    Engine,
    InsertStrategyApply,
    SchemaHead,
    SchemaStrategyPlan,
    StreamWire,
    TransferError,
    TransferInbound,
    TransferReport,
    TransferRun,
    UnknownTypeApply,
)

__all__ = ["ChPgTypes", "ChToPg"]


class ChPgTypes:
    """Текст типа postgres для колонки контракта ClickHouse по разобранному
    тексту его типа. Пусто — у postgres нет типа, который прочитает текст
    TabSeparated без потерь: Array, Map, Tuple, Nested (литерал ClickHouse),
    DateTime64 с поясом не UTC (текст без смещения прочитался бы как UTC),
    Object('json') старых серверов (печатается кортежем). Nullable потока —
    nullable колонки.

    Что и как читается:
        - целые по ширине и знаку: UInt64 — numeric(20), 128 и 256 бит —
          numeric(39) и numeric(78);
        - Decimal(p, s) — numeric(p, s);
        - Float — real и double precision, nan и inf postgres читает;
        - String, LowCardinality, Enum — text; FixedString — text, хвостовые
          NUL-байты postgres не примет;
        - Date и Date32 — date; DateTime — timestamp(0); DateTime64(n) —
          timestamp(n), n больше 6 postgres округляет; DateTime64(n, 'UTC') —
          timestamptz(n), сессия COPY в UTC;
        - Bool — boolean; UUID — uuid; IPv4 и IPv6 — inet; JSON — jsonb."""

    SIGNED: ClassVar[Mapping[int, str]] = {
        8: "smallint",
        16: "smallint",
        32: "integer",
        64: "bigint",
        128: "numeric(39)",
        256: "numeric(78)",
    }
    UNSIGNED: ClassVar[Mapping[int, str]] = {
        8: "smallint",
        16: "integer",
        32: "bigint",
        64: "numeric(20)",
        128: "numeric(39)",
        256: "numeric(78)",
    }
    FLOATS: ClassVar[Mapping[int, str]] = {32: "real", 64: "double precision"}
    PLAIN: ClassVar[Mapping[str, str]] = {
        "String": "text",
        "FixedString": "text",
        "Enum8": "text",
        "Enum16": "text",
        "Bool": "boolean",
        "Date": "date",
        "Date32": "date",
        "UUID": "uuid",
        "IPv4": "inet",
        "IPv6": "inet",
        "JSON": "jsonb",
    }
    UTC: ClassVar[str] = "UTC"
    MAX_PRECISION: ClassVar[int] = 6
    TIMESTAMP: ClassVar[str] = "timestamp"
    TIMESTAMPTZ: ClassVar[str] = "timestamptz"

    def __init__(self) -> None:
        self._types = ChTypes()

    def render(self, type_text: str) -> str:
        """Текст типа postgres без nullable; пусто — пары нет."""
        parsed = self._types.parse(type_text)
        renderers: Mapping[TypeFamily, Callable[[ChParsedType], str]] = {
            TypeFamily.INTEGER: self._integer,
            TypeFamily.FLOAT: self._float,
            TypeFamily.DECIMAL: self._decimal,
            TypeFamily.TIMESTAMP: self._moment,
        }
        render = renderers.get(parsed.kind.family)
        if render is not None:
            return render(parsed)

        return self.PLAIN.get(parsed.head, "")

    def known(self, type_text: str) -> str:
        """Что сказать о типе в отказе: тип и причина, если у неё есть выход."""
        parsed = self._types.parse(type_text)
        if parsed.kind.family is TypeFamily.TIMESTAMP and not self.render(type_text):
            return (
                f"{parsed.text}: TabSeparated carries no offset, select "
                f"toDateTime64(x, {self._precision(parsed)}, 'UTC') or declare "
                f"timestamp({self._precision(parsed)}) in rules.column_types"
            )

        return parsed.text

    def _integer(self, parsed: ChParsedType) -> str:
        if parsed.kind.unsigned:
            return self.UNSIGNED.get(parsed.kind.bits, "")

        return self.SIGNED.get(parsed.kind.bits, "")

    def _float(self, parsed: ChParsedType) -> str:
        return self.FLOATS.get(parsed.kind.bits, "")

    @staticmethod
    def _decimal(parsed: ChParsedType) -> str:
        return f"numeric({parsed.kind.precision}, {parsed.kind.scale})"

    def _moment(self, parsed: ChParsedType) -> str:
        precision = self._precision(parsed)
        zone = self._zone(parsed)
        if not zone:
            return f"{self.TIMESTAMP}({precision})"

        if zone == self.UTC:
            return f"{self.TIMESTAMPTZ}({precision})"

        return ""

    def _precision(self, parsed: ChParsedType) -> int:
        if parsed.head == "DateTime":
            return 0

        precision_text, _, _ = parsed.args.partition(",")

        return min(int(precision_text), self.MAX_PRECISION)

    @staticmethod
    def _zone(parsed: ChParsedType) -> str:
        if parsed.head == "DateTime":
            return parsed.args.strip().strip("'")

        _, _, zone = parsed.args.partition(",")

        return zone.strip().strip("'")


class ChToPg(PgTransfer):
    """Реализация PgTransfer для источника ClickHouse: одной транзакцией
    приёмника разбирает переведённые типы и rules.column_types своим
    сервером, читает каталог таблицы, сверяет через PgMatcher, ведёт
    стратегии через TransferRun и кладёт тела TabSeparated в COPY text как
    есть."""

    def __init__(
        self,
        conn: psycopg.AsyncConnection[Any],
        table: PgTableRef,
        head: SchemaHead,
        feed: TransferInbound,
        journal: CommandJournal,
    ) -> None:
        if head.source_engine is not Engine.CLICKHOUSE:
            raise TransferError(
                f"transfer clickhouse -> postgres got a schema frame from "
                f"{head.source_engine.value}"
            )

        if head.wire is not StreamWire.TSV:
            raise TransferError(
                f"pair clickhouse -> postgres takes wire tsv, got {head.wire.value}"
            )

        self._conn = conn
        self._table = table
        self._contract = ChContract.model_validate(head.contract)
        self._feed = feed
        self._journal = journal
        self._facts = PgTableFacts(conn, table, journal)
        self._resolver = PgTypeResolver(conn, journal)
        self._types = ChPgTypes()
        self._ch_types = ChTypes()

    async def run(  # noqa: PLR0913
        self,
        schema_strategy: SchemaStrategyPlan,
        delete_strategy: DeleteStrategyApply,
        insert_strategy: InsertStrategyApply,
        unknown_types: UnknownTypeApply,
        rules: ColumnRules,
        create_table: CreateTemplate,
    ) -> TransferReport:
        matcher = PgMatcher(self._conn.adapters.types, rules)
        fields = self._fields()
        async with self._conn.transaction():
            resolved = await self._resolver.resolve(matcher.resolve_text_types(fields))
            exists = await self._facts.exists()
            catalog: Sequence[PgCatalogColumn] = ()
            if exists:
                catalog = await self._facts.columns()

            matched = matcher.match(fields, catalog, resolved)
            table = PgTransferTable(
                self._conn, self._table, matched.plan, create_table, self._journal
            )
            sink = PgCopyIn(
                self._conn,
                self._table,
                matched.plan.names(),
                PgCopyLayout.TSV,
                self._feed.bodies(),
                self._journal,
            )
            run = TransferRun(
                schema_strategy,
                delete_strategy,
                insert_strategy,
                unknown_types,
                self._journal,
            )

            return await run.run(
                self._table.text(),
                exists,
                matched.check,
                matched.plan.planned(),
                table,
                sink,
                True,
            )

    def _fields(self) -> list[PgField]:
        """Поля потока в терминах postgres: текст типа — перевод ChPgTypes,
        OID и typmod даст разбор на приёмнике, nullable — из текста типа
        ClickHouse."""
        fields: list[PgField] = []
        for column in self._contract.columns:
            fields.append(
                PgField(
                    name=column.name,
                    oid=0,
                    typmod=-1,
                    type_text=self._types.render(column.type_text),
                    nullable=self._ch_types.parse(column.type_text).nullable,
                    known=self._types.known(column.type_text),
                )
            )

        return fields
