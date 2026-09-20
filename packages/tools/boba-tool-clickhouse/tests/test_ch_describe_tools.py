"""Describe-инструменты ch на живом clickhouse стенда: каждый отдаёт страницу
с объявленными колонками, серверные параметры сужают выдачу, окно листается."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, ClassVar

import pytest
from pydantic import BaseModel, ConfigDict

from boba.config import bind
from boba.db.clickhouse.profile import ClickHouseConfig
from boba.tool.ch import tools as ch
from boba.toolkit.entry import ToolMain
from boba.toolkit.facade import PayloadTool
from boba.toolkit.result import SqlResult

pytestmark = [pytest.mark.integration, pytest.mark.anyio]


class DescribeCase(BaseModel):
    """Инструмент, аргументы вызова и колонки, которые он обещает."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    tool: PayloadTool
    args: dict[str, Any]
    columns: Sequence[str]


class Cases:
    """Describe-инструменты, у которых на стенде точно есть строки."""

    LIMIT: ClassVar[int] = 5

    @classmethod
    def all(cls) -> list[DescribeCase]:
        return [
            DescribeCase(
                tool=cls._of(ch.ch_list_tables),
                args={"database": None},
                columns=["database", "table", "engine", "total_rows"],
            ),
            DescribeCase(
                tool=cls._of(ch.ch_list_columns),
                args={"database": None, "table": None},
                columns=["database", "table", "name", "position", "type"],
            ),
            DescribeCase(
                tool=cls._of(ch.ch_database_describe),
                args={"database": "*"},
                columns=["address", "name", "engine", "comment"],
            ),
            DescribeCase(
                tool=cls._of(ch.ch_table_describe),
                args={"database": "*", "table": "*"},
                columns=["address", "database", "name", "engine", "sorting_key"],
            ),
            DescribeCase(
                tool=cls._of(ch.ch_column_describe),
                args={"database": "*", "table": "*"},
                columns=["address", "database", "table", "name", "type", "comment"],
            ),
            DescribeCase(
                tool=cls._of(ch.ch_function_describe),
                args={"function": "array%"},
                columns=["address", "name", "is_aggregate", "origin"],
            ),
            DescribeCase(
                tool=cls._of(ch.ch_types_describe),
                args={"name": "UInt%"},
                columns=["address", "name", "case_insensitive", "alias_to"],
            ),
        ]

    @staticmethod
    def _of(function: Any) -> PayloadTool:
        payload = ToolMain.toolset(function)[0]
        if not isinstance(payload, PayloadTool):
            raise AssertionError(
                f"{function}: expected PayloadTool, got {type(payload).__name__}"
            )
        return payload


class Runner:
    """Вызов тела инструмента напрямую с профилем соединения стенда."""

    def __init__(self, connection: ClickHouseConfig) -> None:
        self._connection = connection

    async def page(self, case: DescribeCase, offset: int, limit: int) -> SqlResult:
        body = case.tool.coroutine
        if body is None:
            raise AssertionError(f"{case.tool.name}: body is None")

        result = await body(
            connection=self._connection, offset=offset, limit=limit, **case.args
        )
        if not isinstance(result, SqlResult):
            raise AssertionError(
                f"{case.tool.name}: expected SqlResult, got {type(result).__name__}"
            )

        return result

    @staticmethod
    def rows(result: SqlResult) -> Sequence[dict[str, Any]]:
        statement = result.statements[0]
        if statement.rows is None:
            raise AssertionError("statement without rows")

        return [dict(row) for row in statement.rows]


@pytest.fixture(scope="module")
def connection(raw_config: Any) -> ClickHouseConfig:
    return bind(raw_config, path="clickhouse", model=ClickHouseConfig)


@pytest.fixture(scope="module")
def runner(connection: ClickHouseConfig) -> Runner:
    return Runner(connection)


class TestDescribeTools:
    @pytest.mark.parametrize("case", Cases.all(), ids=lambda case: case.tool.name)
    async def test_page_has_the_declared_columns(
        self, case: DescribeCase, runner: Runner
    ) -> None:
        rows = runner.rows(await runner.page(case, offset=0, limit=Cases.LIMIT))

        assert rows, f"{case.tool.name}: no rows for {case.args}"
        missing = [column for column in case.columns if column not in rows[0]]
        assert missing == [], f"{case.tool.name}: columns missing: {missing}"

    @pytest.mark.parametrize("case", Cases.all(), ids=lambda case: case.tool.name)
    async def test_window_moves_by_offset(
        self, case: DescribeCase, runner: Runner
    ) -> None:
        first = runner.rows(await runner.page(case, offset=0, limit=2))
        second = runner.rows(await runner.page(case, offset=1, limit=2))

        if len(first) < 2:
            pytest.skip(f"{case.tool.name}: fewer than two rows for {case.args}")

        assert second[0] == first[1], f"{case.tool.name}: offset=1 must start at row 2"

    async def test_system_databases_stay_hidden(self, runner: Runner) -> None:
        case = DescribeCase(
            tool=Cases._of(ch.ch_database_describe), args={"database": "*"}, columns=[]
        )
        rows = runner.rows(await runner.page(case, offset=0, limit=100))

        names = {row["name"] for row in rows}
        assert not names & {"system", "INFORMATION_SCHEMA", "information_schema"}

    async def test_exact_filter_narrows_to_one_table(self, runner: Runner) -> None:
        listing = DescribeCase(
            tool=Cases._of(ch.ch_table_describe),
            args={"database": "*", "table": "*"},
            columns=[],
        )
        first = runner.rows(await runner.page(listing, offset=0, limit=1))[0]

        exact = DescribeCase(
            tool=Cases._of(ch.ch_table_describe),
            args={"database": first["database"], "table": first["name"]},
            columns=[],
        )
        rows = runner.rows(await runner.page(exact, offset=0, limit=10))

        assert [(row["database"], row["name"]) for row in rows] == [
            (first["database"], first["name"])
        ]

    async def test_quote_in_a_value_is_a_value_not_sql(self, runner: Runner) -> None:
        case = DescribeCase(
            tool=Cases._of(ch.ch_table_describe),
            args={"database": "*", "table": "x' or 1=1 --"},
            columns=[],
        )

        assert runner.rows(await runner.page(case, offset=0, limit=10)) == []
