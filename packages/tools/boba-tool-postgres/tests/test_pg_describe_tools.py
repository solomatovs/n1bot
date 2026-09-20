"""Describe-инструменты pg на живом postgres стенда: каждый отдаёт страницу
с объявленными колонками, фильтр по имени сужает выдачу, окно листается,
pg_query отдаёт итог каждой команды."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, ClassVar

import pytest
from pydantic import BaseModel, ConfigDict

from boba.config import bind
from boba.db.postgres.profile import PostgresConfig
from boba.tool.pg import tools as pg
from boba.toolkit.entry import ToolMain
from boba.toolkit.facade import PayloadTool
from boba.toolkit.result import SqlResult

pytestmark = [pytest.mark.integration, pytest.mark.anyio]


class DescribeCase(BaseModel):
    """Инструмент, аргументы вызова со звёздочками и колонки, которые он обещает."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    tool: PayloadTool
    args: dict[str, str]
    columns: Sequence[str]


class Cases:
    """Все describe-инструменты; pg_catalog есть в любой базе, поэтому им и сужаем."""

    SCHEMA: ClassVar[str] = "pg_catalog"
    LIMIT: ClassVar[int] = 5

    @classmethod
    def all(cls) -> list[DescribeCase]:
        return [
            DescribeCase(
                tool=cls._of(pg.pg_database_describe),
                args={"db_name": "*"},
                columns=["address", "name", "owner", "encoding", "collate", "comment"],
            ),
            DescribeCase(
                tool=cls._of(pg.pg_schema_describe),
                args={"schema_name": "*"},
                columns=["address", "database", "name", "owner", "comment"],
            ),
            DescribeCase(
                tool=cls._of(pg.pg_table_describe),
                args={"schema_name": cls.SCHEMA, "table_name": "*"},
                columns=["address", "schema", "name", "kind", "owner", "options"],
            ),
            DescribeCase(
                tool=cls._of(pg.pg_column_describe),
                args={"schema_name": cls.SCHEMA, "table_name": "pg_class"},
                columns=[
                    "address",
                    "relation",
                    "name",
                    "ordinal",
                    "type",
                    "nullable",
                    "default",
                ],
            ),
            DescribeCase(
                tool=cls._of(pg.pg_constraints_describe),
                args={"schema_name": cls.SCHEMA, "table_name": "*"},
                columns=[
                    "address",
                    "relation",
                    "name",
                    "kind",
                    "columns",
                    "definition",
                ],
            ),
            DescribeCase(
                tool=cls._of(pg.pg_indexes_describe),
                args={"schema_name": cls.SCHEMA, "table_name": "pg_class"},
                columns=[
                    "address",
                    "relation",
                    "name",
                    "method",
                    "unique",
                    "primary",
                    "columns",
                    "definition",
                ],
            ),
            DescribeCase(
                tool=cls._of(pg.pg_routines_describe),
                args={"schema_name": cls.SCHEMA, "routine_name": "*"},
                columns=[
                    "address",
                    "name",
                    "signature",
                    "kind",
                    "language",
                    "returns",
                    "volatility",
                ],
            ),
            DescribeCase(
                tool=cls._of(pg.pg_routine_arg_describe),
                args={"schema_name": cls.SCHEMA, "routine_name": "format_type"},
                columns=["address", "routine", "signature", "position", "type", "mode"],
            ),
            DescribeCase(
                tool=cls._of(pg.pg_sequences_describe),
                args={"schema_name": "*", "sequence_name": "*"},
                columns=["address", "name", "type", "start", "increment", "owned_by"],
            ),
            DescribeCase(
                tool=cls._of(pg.pg_types_describe),
                args={"schema_name": "*", "type_name": "*"},
                columns=["address", "name", "kind", "owner", "labels", "attributes"],
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

    def __init__(self, connection: PostgresConfig) -> None:
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
def connection(raw_config: Any) -> PostgresConfig:
    return bind(raw_config, path="postgres", model=PostgresConfig)


@pytest.fixture(scope="module")
def runner(connection: PostgresConfig) -> Runner:
    return Runner(connection)


class TestDescribeTools:
    @pytest.mark.parametrize("case", Cases.all(), ids=lambda case: case.tool.name)
    async def test_page_has_the_declared_columns(
        self, case: DescribeCase, runner: Runner
    ) -> None:
        result = await runner.page(case, offset=0, limit=Cases.LIMIT)
        rows = runner.rows(result)

        assert rows, f"{case.tool.name}: no rows for {case.args}"
        missing = [column for column in case.columns if column not in rows[0]]
        assert missing == [], (
            f"{case.tool.name}: columns missing from the row: {missing}"
        )

    @pytest.mark.parametrize("case", Cases.all(), ids=lambda case: case.tool.name)
    async def test_window_moves_by_offset(
        self, case: DescribeCase, runner: Runner
    ) -> None:
        first = runner.rows(await runner.page(case, offset=0, limit=2))
        second = runner.rows(await runner.page(case, offset=1, limit=2))

        if len(first) < 2:
            pytest.skip(f"{case.tool.name}: fewer than two rows for {case.args}")

        assert second[0] == first[1], (
            f"{case.tool.name}: offset=1 must start at the second row"
        )

    async def test_exact_filter_narrows_to_one_table(self, runner: Runner) -> None:
        case = DescribeCase(
            tool=Cases._of(pg.pg_table_describe),
            args={"schema_name": "pg_catalog", "table_name": "pg_class"},
            columns=[],
        )
        rows = runner.rows(await runner.page(case, offset=0, limit=10))

        assert [row["name"] for row in rows] == ["pg_class"]
        assert rows[0]["kind"] == "table"

    async def test_query_reports_every_statement(
        self, connection: PostgresConfig
    ) -> None:
        body = Cases._of(pg.pg_query).coroutine
        if body is None:
            raise AssertionError("pg_query body is None")

        result = await body(
            connection=connection,
            sql="select 1 as a; select 2 as b, 3 as c",
            offset=0,
            limit=10,
        )

        assert isinstance(result, SqlResult)
        assert [statement.rows for statement in result.statements] == [
            [{"a": 1}],
            [{"b": 2, "c": 3}],
        ]
