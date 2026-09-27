"""Пара postgres -> postgres: контракт RowDescription источника как есть,
сверка с таблицей приёмника по OID и typmod, DDL текстом типа источника,
тела COPY csv или text в COPY приёмника без перекодирования.

Ошибки:
TransferError — кадр схемы не от postgres; правило rename_columns или
    column_types на несуществующую колонку; стратегия схемы отказала;
    колонка без типа при fail_on_unknown.
psycopg.Error — сервер приёмника отклонил стейтмент или значение.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, ClassVar

import psycopg
from psycopg._typeinfo import TypesRegistry

from boba.db.postgres.transfer import (
    PgCatalogColumn,
    PgContract,
    PgCopyIn,
    PgCopyLayout,
    PgField,
    PgMatcher,
    PgServerVersion,
    PgStreamColumn,
    PgTableFacts,
    PgTableRef,
    PgTransferTable,
    PgTypedName,
    PgTypeResolver,
    PostgresTransfer,
)
from boba.toolkit.transfer import (
    ColumnRules,
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

__all__ = ["PgBinaryCompatibility", "PgToPg"]


class PgBinaryCompatibility:
    """Проверка приёмником потока COPY binary по фактам источника из кадра
    schema: мажорная версия приёмника равна версии источника,
    integer_datetimes включён на обоих, и у каждой колонки потока тип из
    встроенного реестра (свой или объявленный в rules.column_types).
    Массивы и составные типы в binary несут OID элемента, у пользовательских
    типов он на другом инстансе иной, поэтому такие колонки едут только csv."""

    INTEGER_DATETIMES: ClassVar[str] = "integer_datetimes"
    ON: ClassVar[str] = "on"

    def __init__(self, registry: TypesRegistry) -> None:
        self._registry = registry

    def check(
        self,
        contract: PgContract,
        target_version: PgServerVersion,
        target_integer_datetimes: bool,
        columns: Sequence[PgTypedName],
    ) -> None:
        source_version = contract.version()
        if source_version.major() != target_version.major():
            raise TransferError(
                f"binary copy from postgres {source_version.text()} into postgres "
                f"{target_version.text()}: major versions differ, restart "
                f"pg_stream_out with wire csv"
            )

        if not contract.integer_datetimes or not target_integer_datetimes:
            raise TransferError(
                "binary copy needs integer_datetimes = on on both servers, restart "
                "pg_stream_out with wire csv"
            )

        for column in columns:
            if self._registry.get(column.oid) is not None:
                continue

            raise TransferError(
                f"binary copy: column {column.name} has a type outside the "
                f"built-in registry ({column.known}); user types carry "
                f"instance-specific OIDs in binary, restart pg_stream_out with wire "
                f'csv or declare a built-in type in rules.column_types["{column.name}"]'
            )


class PgToPg(PostgresTransfer):
    """Реализация PostgresTransfer для источника postgres: одной транзакцией
    приёмника разбирает rules.column_types своим сервером, читает каталог
    таблицы, сверяет, ведёт стратегии через TransferRun и кладёт тела COPY в
    таблицу как есть."""

    def __init__(
        self,
        conn: psycopg.AsyncConnection[Any],
        table: PgTableRef,
        head: SchemaHead,
        feed: TransferInbound,
    ) -> None:
        if head.source_engine is not Engine.POSTGRES:
            raise TransferError(
                f"transfer postgres -> postgres got a schema frame from "
                f"{head.source_engine.value}"
            )

        self._conn = conn
        self._table = table
        self._layout = self._layout_of(head.wire)
        self._contract = PgContract.model_validate(head.contract)
        self._feed = feed
        self._facts = PgTableFacts(conn, table)
        self._resolver = PgTypeResolver(conn)
        self._binary = PgBinaryCompatibility(conn.adapters.types)

    @staticmethod
    def _layout_of(wire: StreamWire) -> PgCopyLayout:
        """Формат COPY по формату из кадра schema; arrow и raw пара не принимает."""
        try:
            return PgCopyLayout(wire.value)
        except ValueError as exc:
            raise TransferError(
                f"pair postgres -> postgres takes copy layouts "
                f"{[member.value for member in PgCopyLayout]}, got {wire.value}"
            ) from exc

    def _fields(self) -> list[PgField]:
        """Поля потока как их описал источник: OID и typmod, текст типа,
        nullable по декларации."""
        fields: list[PgField] = []
        for column in self._contract.columns:
            fields.append(
                PgField(
                    name=column.name,
                    oid=column.oid,
                    typmod=column.typmod,
                    type_text=column.type_text,
                    nullable=column.nullable,
                    known=column.known(),
                )
            )

        return fields

    @staticmethod
    def _typed_names(stream: Sequence[PgStreamColumn]) -> list[PgTypedName]:
        names: list[PgTypedName] = []
        for column in stream:
            names.append(
                PgTypedName(name=column.name, oid=column.oid, known=column.known)
            )

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
        matcher = PgMatcher(self._conn.adapters.types, rules)
        fields = self._fields()
        async with self._conn.transaction():
            resolved = await self._resolver.resolve(matcher.resolve_text_types(fields))
            exists = await self._facts.exists()
            catalog: Sequence[PgCatalogColumn] = ()
            if exists:
                catalog = await self._facts.columns()

            matched = matcher.match(fields, catalog, resolved)
            if self._layout is PgCopyLayout.BINARY:
                info = self._conn.info
                self._binary.check(
                    self._contract,
                    PgServerVersion(info.server_version),
                    info.parameter_status(self._binary.INTEGER_DATETIMES)
                    == self._binary.ON,
                    self._typed_names(matched.stream),
                )

            table = PgTransferTable(self._conn, self._table, matched.plan, create_table)
            sink = PgCopyIn(
                self._conn,
                self._table,
                matched.plan.names(),
                self._layout,
                self._feed.bodies(),
            )
            run = TransferRun(
                schema_strategy, delete_strategy, insert_strategy, unknown_types
            )

            return await run.run(
                self._table.text(), exists, matched.check, table, sink, True
            )
