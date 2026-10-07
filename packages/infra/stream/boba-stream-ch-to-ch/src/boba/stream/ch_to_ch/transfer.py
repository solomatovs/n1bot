"""Пара ClickHouse -> ClickHouse: контракт из шапки TabSeparatedWithNamesAndTypes
источника как есть, поле потока — тип ClickHouse источника с Nullable по его
тексту, сверка и план — общими ChMatcher и ChTypeRules стороны ClickHouse,
тела TabSeparated в input() приёмника без перекодирования.

Ошибки:
TransferError — кадр схемы не от ClickHouse; правило rename_columns или
    column_types на несуществующую колонку; стратегия схемы отказала; база не
    Atomic или кластер неизвестен.
ClickHouseQueryError — сервер приёмника отклонил стейтмент или значение.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import ClassVar

from clickhouse_connect.driver.asyncclient import AsyncClient

from boba.db.clickhouse.target import ChPlacement, ChTableRef
from boba.db.clickhouse.transfer import (
    ChCatalogColumn,
    ChContract,
    ChContractTypes,
    ChInputSink,
    ChMatcher,
    ChStreamColumn,
    ChTableFacts,
    ChTransfer,
    ChTransferTable,
    ChTwin,
    ChTypeResolver,
    ChTypes,
    WholeBody,
)
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

__all__ = ["ChToCh"]


class ChToCh(ChTransfer):
    """Реализация ChTransfer для источника ClickHouse: база обязана
    быть Atomic; column_types нормализует сервер приёмника, каталог таблицы
    сверяется по текстам типов, стратегии идут через TransferRun с загрузкой
    TabSeparated в двойник через input() и exchange tables."""

    FORMAT: ClassVar[str] = "TabSeparated"

    def __init__(  # noqa: PLR0913 — ширина подписи задана контрактом
        self,
        client: AsyncClient,
        table: ChTableRef,
        placement: ChPlacement,
        head: SchemaHead,
        feed: TransferInbound,
        journal: CommandJournal,
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
        self._journal = journal
        self._facts = ChTableFacts(client, table, journal)
        self._resolver = ChTypeResolver(client, journal)
        self._types = ChTypes()

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
        stream = matcher.stream(self._fields(), declared)
        exists = await self._facts.exists()
        catalog: Sequence[ChCatalogColumn] = ()
        if exists:
            catalog = await self._facts.columns()

        matched = matcher.match(stream, catalog)
        twin = ChTwin(self._client, self._table, self._placement.cluster, self._journal)
        table = ChTransferTable(
            self._client,
            self._table,
            matched.plan,
            create_table,
            twin,
            self._placement,
            self._journal,
        )
        sink = ChInputSink(
            self._client,
            twin,
            matched.plan,
            self.FORMAT,
            ChContractTypes(self._stream_types(matched.stream)),
            self._feed,
            self._journal,
            WholeBody(),
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
            False,
        )

    def _fields(self) -> list[ChStreamColumn]:
        """Поля потока как их описал источник: текст типа как есть, nullable
        и то, что известно, — из него же."""
        fields: list[ChStreamColumn] = []
        for column in self._contract.columns:
            fields.append(
                ChStreamColumn(
                    name=column.name,
                    source_name=column.name,
                    type_text=column.type_text,
                    nullable=self._types.parse(column.type_text).nullable,
                    known=column.type_text,
                )
            )

        return fields

    @staticmethod
    def _stream_types(stream: Sequence[ChStreamColumn]) -> dict[str, str]:
        types: dict[str, str] = {}
        for column in stream:
            types[column.name] = column.type_text

        return types
