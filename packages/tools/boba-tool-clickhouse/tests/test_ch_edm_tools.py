"""Инструменты ЕДМ на живом ClickHouse стенда: выгрузка ЕДМ пересоздаётся
малым набором на каждом источнике ix_stand, оба инструмента отдают объявленные
колонки, фильтры сужают выдачу, окно листается."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest

from boba.config import bind
from boba.db.clickhouse.connection import ClickHouseConfig
from boba.stand.edm import Asset, EdmDataset, EdmSource, EdmSources
from boba.tool.ch import tools as ch
from boba.toolkit.entry import ToolMain
from boba.toolkit.facade import PayloadTool
from boba.toolkit.result import SqlResult

pytestmark = [pytest.mark.integration, pytest.mark.anyio]


EDM_DATABASE = "edm_stand"
"""База выгрузки тестов инструментов; у UI-стенда своя."""


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
            database=EDM_DATABASE,
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


@pytest.fixture(scope="module")
def sources(raw_config: Any) -> list[EdmSource]:
    listed = bind(raw_config, path="ix_stand", model=EdmSources).demo()
    if not listed:
        pytest.fail("ix_stand.ch_sources has no source with demo = true")

    return listed


@pytest.fixture(scope="module", params=["first", "last"])
async def runner(request: Any, sources: list[EdmSource]) -> AsyncIterator[Runner]:
    """Самый старый и самый новый сервер стенда: старый и новый анализатор."""
    source = sources[0]
    if request.param == "last":
        source = sources[-1]

    dataset = EdmDataset(source, EDM_DATABASE)
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
