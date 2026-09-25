"""Инструменты ЕДМ на живом ClickHouse стенда: выгрузка ЕДМ пересоздаётся
малым набором на каждом источнике ix_stand, оба инструмента отдают объявленные
колонки, фильтры сужают выдачу, окно листается."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from enum import StrEnum
from typing import Any, ClassVar

import pytest
from pydantic import BaseModel, ConfigDict

from boba.config import bind
from boba.db.clickhouse.connection import ClickHouseConfig, ClickHouseSettingsConfig
from boba.db.clickhouse.payload import PayloadClickHouse
from boba.db.clickhouse.query import ChQueryBuilder, ChValue
from boba.tool.ch import tools as ch
from boba.tool.ch.tools import Edm
from boba.toolkit.entry import ToolMain
from boba.toolkit.facade import PayloadTool
from boba.toolkit.result import SqlResult

pytestmark = [pytest.mark.integration, pytest.mark.anyio]


class StandSource(BaseModel):
    """Источник ix_stand: имя и профиль; demo — можно ли на нём создавать базы."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    name: str
    clickhouse: ClickHouseConfig
    demo: bool = True

    @property
    def admin(self) -> ClickHouseConfig:
        return self.clickhouse.model_copy(
            update={"settings": ClickHouseSettingsConfig.model_validate({})}
        )


class StandSources(BaseModel):
    """Секция [ix_stand]: здесь нужен только список ch_sources."""

    model_config = ConfigDict(extra="ignore")

    ch_sources: Sequence[StandSource]


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
    одна логическая сущность, одна внешняя (is_inner = 0) связь-помеха."""

    DATABASE: ClassVar[str] = "edm_stand"

    def __init__(self, source: StandSource) -> None:
        self._source = source

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
                await self._command(client, ddl, table=ChValue(table.table.value))
                await client.insert(
                    table.table.value,
                    [list(row) for row in table.rows],
                    column_names=table.column_names(),
                    database=self.DATABASE,
                )

    async def drop(self) -> None:
        async with PayloadClickHouse.opened_config(self._source.admin) as client:
            await self._command(client, "drop database if exists {db:Identifier}")

    async def _command(self, client: Any, text: str, **bind: Any) -> None:
        query = ChQueryBuilder().add(text, db=ChValue(self.DATABASE), **bind).build()
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


class Runner:
    """Вызов тела инструмента напрямую с профилем соединения стенда."""

    def __init__(self, connection: ClickHouseConfig) -> None:
        self._connection = connection

    async def rows(
        self, function: Any, offset: int, limit: int, **args: Any
    ) -> list[dict[str, Any]]:
        payload = ToolMain.toolset(function)[0]
        if not isinstance(payload, PayloadTool):
            raise AssertionError(f"{function}: expected PayloadTool")

        body = payload.coroutine
        if body is None:
            raise AssertionError(f"{payload.name}: body is None")

        result = await body(
            connection=self._connection,
            database=EdmDataset.DATABASE,
            offset=offset,
            limit=limit,
            **args,
        )
        if not isinstance(result, SqlResult):
            raise AssertionError(f"{payload.name}: expected SqlResult")

        statement = result.statements[0]
        if statement.rows is None:
            raise AssertionError(f"{payload.name}: statement without rows")

        return [dict(row) for row in statement.rows]


def _demo_sources(raw_config: Any) -> list[StandSource]:
    sources = bind(raw_config, path="ix_stand", model=StandSources)

    return [source for source in sources.ch_sources if source.demo]


@pytest.fixture(scope="module")
def sources(raw_config: Any) -> list[StandSource]:
    listed = _demo_sources(raw_config)
    if not listed:
        pytest.skip("ix_stand.ch_sources has no source with demo = true")

    return listed


@pytest.fixture(scope="module", params=["first", "last"])
async def runner(request: Any, sources: list[StandSource]) -> AsyncIterator[Runner]:
    """Самый старый и самый новый сервер стенда: старый и новый анализатор."""
    source = sources[0]
    if request.param == "last":
        source = sources[-1]

    dataset = EdmDataset(source)
    await dataset.recreate()

    yield Runner(source.clickhouse)

    await dataset.drop()


class TestEdmStructure:
    async def test_every_column_of_every_table(self, runner: Runner) -> None:
        rows = await runner.rows(ch.ch_edm_structure, 0, 100)

        listed = [(row["path"], row["table_name"], row["column_name"]) for row in rows]

        assert listed == [
            ("/dwh/public/orders", "orders", "amount"),
            ("/dwh/public/orders", "orders", "id"),
            ("/dwh/public/v_paid", "v_paid", "id"),
        ]
        assert rows[0]["etalon_id"] == Asset.ORDERS_AMOUNT
        assert rows[0]["etalon_id_parent"] == Asset.ORDERS

    async def test_table_filter_narrows(self, runner: Runner) -> None:
        rows = await runner.rows(ch.ch_edm_structure, 0, 100, table="v_paid")

        assert [row["column_name"] for row in rows] == ["id"]

    async def test_path_filter_is_like(self, runner: Runner) -> None:
        rows = await runner.rows(ch.ch_edm_structure, 0, 100, path="%/orders")

        assert [row["column_name"] for row in rows] == ["amount", "id"]

    async def test_window_moves_by_offset(self, runner: Runner) -> None:
        first = await runner.rows(ch.ch_edm_structure, 0, 2)
        second = await runner.rows(ch.ch_edm_structure, 1, 2)

        assert second[0] == first[1]


class TestEdmDescriptions:
    async def test_every_physical_object(self, runner: Runner) -> None:
        rows = await runner.rows(ch.ch_edm_descriptions, 0, 100)

        assert [row["path"] for row in rows] == [
            "/dwh/public/orders",
            "/dwh/public/orders/amount",
            "/dwh/public/orders/id",
            "/dwh/public/v_paid",
            "/dwh/public/v_paid/id",
        ]

        orders = rows[0]
        assert orders["name"] == "orders"
        assert orders["short_description_edm"] == "Orders"
        assert orders["extended_description_edm"] == "All orders"
        assert orders["description_from_source"] == "orders table"
        assert orders["ed_name"] == "Order"

        amount = rows[1]
        assert amount["description_from_source"] == "amount in rub"
        assert amount["short_description_edm"] == ""
        assert amount["ed_name"] == ""

    async def test_name_filter_is_exact(self, runner: Runner) -> None:
        rows = await runner.rows(ch.ch_edm_descriptions, 0, 100, name="id")

        assert [row["path"] for row in rows] == [
            "/dwh/public/orders/id",
            "/dwh/public/v_paid/id",
        ]

    async def test_path_filter_is_like(self, runner: Runner) -> None:
        rows = await runner.rows(ch.ch_edm_descriptions, 0, 100, path="%/v_paid%")

        assert [row["name"] for row in rows] == ["v_paid", "id"]

    async def test_window_moves_by_offset(self, runner: Runner) -> None:
        first = await runner.rows(ch.ch_edm_descriptions, 0, 2)
        second = await runner.rows(ch.ch_edm_descriptions, 1, 2)

        assert second[0] == first[1]
