"""Малая выгрузка ЕДМ на ClickHouse стенда: таблицы и строки, по которым
инструменты ch_edm_* дают проверяемую выдачу. Базу пересоздаёт admin-профиль
demo-источника [ix_stand].ch_sources, сносит тот, кто её создал.

Ошибки наружу — ошибки драйвера ClickHouse как есть: стенд падает на месте.
"""

from __future__ import annotations

from collections.abc import Sequence
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict

from boba.db.clickhouse.connection import ClickHouseConfig, ClickHouseSettingsConfig
from boba.db.clickhouse.payload import PayloadClickHouse
from boba.db.clickhouse.query import ChQueryBuilder
from boba.tool.ch.tools import Edm

__all__ = ["Asset", "EdmDataset", "EdmSource", "EdmSources", "RelationType"]


class EdmSource(BaseModel):
    """Источник ClickHouse из [ix_stand].ch_sources: имя и профиль; demo —
    можно ли на нём создавать базы."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    name: str
    clickhouse: ClickHouseConfig
    demo: bool = True

    @property
    def admin(self) -> ClickHouseConfig:
        return self.clickhouse.model_copy(
            update={"settings": ClickHouseSettingsConfig.model_validate({})}
        )


class EdmSources(BaseModel):
    """Секция [ix_stand]: здесь нужен только список ch_sources."""

    model_config = ConfigDict(extra="ignore")

    ch_sources: Sequence[EdmSource]

    def demo(self) -> list[EdmSource]:
        """Источники, на которых стенду можно создавать базы."""
        chosen: list[EdmSource] = []
        for source in self.ch_sources:
            if source.demo:
                chosen.append(source)

        return chosen


class Asset(StrEnum):
    """Идентификаторы объектов набора; значение — id в assets."""

    ORDERS = "tbl-orders"
    V_PAID = "view-v_paid"
    ORDERS_ID = "col-orders-id"
    ORDERS_AMOUNT = "col-orders-amount"
    V_PAID_ID = "col-v_paid-id"
    ED_ORDER = "ed-order"


class RelationType(StrEnum):
    TABLE_COLUMN = "rt-table-column"
    VIEW_COLUMN = "rt-view-column"
    OUTER = "rt-outer"
    LOGICAL = "rt-logical"


class TableRows(BaseModel):
    """Таблица набора: имя из Edm, колонки с типами и строки для вставки."""

    model_config = ConfigDict(frozen=True)

    table: Edm
    columns: Sequence[tuple[str, str]]
    rows: Sequence[Sequence[Any]]

    def columns_ddl(self) -> str:
        parts: list[str] = []
        for name, kind in self.columns:
            parts.append(f"{name} {kind}")

        return ", ".join(parts)

    def column_names(self) -> list[str]:
        return [name for name, _ in self.columns]


class EdmDataset:
    """Пересоздаёт базу выгрузки ЕДМ на источнике: две таблицы с колонками,
    одна логическая сущность, одна внешняя (is_inner = 0) связь-помеха.
    Базу называет вызывающий: тесты инструментов и UI-стенд не делят её."""

    def __init__(self, source: EdmSource, database: str) -> None:
        self._source = source
        self._database = database

    @property
    def database(self) -> str:
        return self._database

    async def recreate(self) -> None:
        async with PayloadClickHouse.opened_config(self._source.admin) as client:
            await self._command(client, "drop database if exists {db:Identifier}")
            await self._command(client, "create database {db:Identifier}")

            for table in self._tables():
                ddl = (
                    "create table {db:Identifier}.{table:Identifier} ("
                    + table.columns_ddl()
                    + ") engine = MergeTree order by tuple()"
                )
                await self._command(client, ddl, table=table.table.value)
                await client.insert(
                    table.table.value,
                    [list(row) for row in table.rows],
                    column_names=table.column_names(),
                    database=self._database,
                )

    async def drop(self) -> None:
        async with PayloadClickHouse.opened_config(self._source.admin) as client:
            await self._command(client, "drop database if exists {db:Identifier}")

    async def _command(self, client: Any, text: str, **bind: Any) -> None:
        query = ChQueryBuilder().add(text, db=self._database, **bind).build()
        await client.command(query.text, parameters=query.params)

    def _tables(self) -> list[TableRows]:
        return [
            TableRows(
                table=Edm.ASSETS,
                columns=[("id", "String"), ("path", "String")],
                rows=[
                    (Asset.ORDERS, "/dwh/public"),
                    (Asset.V_PAID, "/dwh/public"),
                    (Asset.ORDERS_ID, "/dwh/public/orders"),
                    (Asset.ORDERS_AMOUNT, "/dwh/public/orders"),
                    (Asset.V_PAID_ID, "/dwh/public/v_paid"),
                    (Asset.ED_ORDER, "/ldm/sales"),
                ],
            ),
            TableRows(
                table=Edm.ATTRIBUTES_PHYSICAL,
                columns=[
                    ("etalon_id", "String"),
                    ("attribute_id", "String"),
                    ("value", "String"),
                ],
                rows=[
                    (Asset.ORDERS, Edm.NAME, "orders"),
                    (Asset.ORDERS, Edm.SHORT_DESCRIPTION, "Orders"),
                    (Asset.ORDERS, Edm.EXTENDED_DESCRIPTION, "All orders"),
                    (Asset.ORDERS, Edm.DESCRIPTION, "orders table"),
                    (Asset.V_PAID, Edm.NAME, "v_paid"),
                    (Asset.V_PAID, Edm.SHORT_DESCRIPTION, "Paid orders"),
                    (Asset.ORDERS_ID, Edm.NAME, "id"),
                    (Asset.ORDERS_ID, Edm.SHORT_DESCRIPTION, "Order id"),
                    (Asset.ORDERS_AMOUNT, Edm.NAME, "amount"),
                    (Asset.ORDERS_AMOUNT, Edm.DESCRIPTION, "amount in rub"),
                    (Asset.V_PAID_ID, Edm.NAME, "id"),
                ],
            ),
            TableRows(
                table=Edm.ATTRIBUTES,
                columns=[
                    ("etalon_id", "String"),
                    ("attribute_id", "String"),
                    ("value", "String"),
                ],
                rows=[(Asset.ED_ORDER, Edm.ED_ENTITY_NAME, "Order")],
            ),
            TableRows(
                table=Edm.RELATION_TYPES,
                columns=[
                    ("relation_type_id", "String"),
                    ("is_inner", "UInt8"),
                    ("type_from", "String"),
                    ("type_to", "String"),
                ],
                rows=[
                    (
                        RelationType.TABLE_COLUMN,
                        1,
                        Edm.TABLE,
                        Edm.TABLE_COLUMN,
                    ),
                    (
                        RelationType.VIEW_COLUMN,
                        1,
                        Edm.VIEW,
                        Edm.VIEW_COLUMN,
                    ),
                    (
                        RelationType.OUTER,
                        0,
                        Edm.TABLE,
                        Edm.TABLE_COLUMN,
                    ),
                    (RelationType.LOGICAL, 0, "ldm_entity", Edm.TABLE),
                ],
            ),
            TableRows(
                table=Edm.RELATIONS,
                columns=[
                    ("etalon_id_from", "String"),
                    ("etalon_id_to", "String"),
                    ("relation_type_id", "String"),
                    ("name", "String"),
                ],
                rows=[
                    (Asset.ORDERS, Asset.ORDERS_ID, RelationType.TABLE_COLUMN, "has"),
                    (
                        Asset.ORDERS,
                        Asset.ORDERS_AMOUNT,
                        RelationType.TABLE_COLUMN,
                        "has",
                    ),
                    (Asset.V_PAID, Asset.V_PAID_ID, RelationType.VIEW_COLUMN, "has"),
                    (Asset.ORDERS, Asset.V_PAID_ID, RelationType.OUTER, "derived"),
                    (
                        Asset.ED_ORDER,
                        Asset.ORDERS,
                        RelationType.LOGICAL,
                        Edm.LOGICAL_TO_PHYSICAL,
                    ),
                ],
            ),
        ]
