"""Инструменты Oracle на живом стенде: describe-инструменты отдают страницу с
объявленными колонками по схеме TOOL_DEMO, ora_query показывает выборку окном и
счётчик у DML, служебные схемы Oracle скрыты за `*`."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import Any, ClassVar

import pytest
from ora_tool_stand import DemoUser, IxStand, ToolDemo
from pydantic import BaseModel, ConfigDict

from boba.db.oracle import OraQueryBuilder, OraSql
from boba.tool.ora import tools as ora
from boba.toolkit.entry import ToolMain
from boba.toolkit.facade import PayloadTool
from boba.toolkit.result import SqlResult

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

STAND = IxStand.required()
ROWS = 25
CUSTOMERS = OraSql(f"{DemoUser.NAME}.customers")


def _sql(text: str) -> str:
    """Текст команды для ora_query с именем таблицы набора."""
    return OraQueryBuilder(table=CUSTOMERS).add(text).build().text


class DescribeCase(BaseModel):
    """Инструмент, аргументы вызова и колонки, которые он обещает."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    tool: PayloadTool
    args: dict[str, Any]
    columns: Sequence[str]


class Cases:
    """Describe-инструменты, у которых в TOOL_DEMO точно есть строки."""

    LIMIT: ClassVar[int] = 5
    SCHEMA: ClassVar[str] = DemoUser.NAME.value

    @classmethod
    def all(cls) -> list[DescribeCase]:
        return [
            DescribeCase(
                tool=cls._of(ora.ora_list_tables),
                args={"schema_name": cls.SCHEMA},
                columns=["schema", "name", "kind", "status", "last_ddl_time"],
            ),
            DescribeCase(
                tool=cls._of(ora.ora_describe_table),
                args={"table": "CUSTOMERS", "schema_name": cls.SCHEMA},
                columns=["address", "column_name", "data_type", "nullable", "comments"],
            ),
            DescribeCase(
                tool=cls._of(ora.ora_schema_describe),
                args={"schema_name": cls.SCHEMA},
                columns=["address", "name", "created", "oracle_maintained"],
            ),
            DescribeCase(
                tool=cls._of(ora.ora_table_describe),
                args={"schema_name": cls.SCHEMA, "table": "*"},
                columns=["address", "schema", "name", "num_rows", "comments"],
            ),
            DescribeCase(
                tool=cls._of(ora.ora_column_describe),
                args={"schema_name": cls.SCHEMA, "table": "CUSTOMERS"},
                columns=["address", "column_name", "data_type", "data_default"],
            ),
            DescribeCase(
                tool=cls._of(ora.ora_constraints_describe),
                args={"schema_name": cls.SCHEMA, "table": "ORDERS"},
                columns=["address", "constraint_type", "columns", "r_constraint_name"],
            ),
            DescribeCase(
                tool=cls._of(ora.ora_indexes_describe),
                args={"schema_name": cls.SCHEMA, "table": "CUSTOMERS"},
                columns=["address", "index_name", "uniqueness", "columns"],
            ),
            DescribeCase(
                tool=cls._of(ora.ora_routines_describe),
                args={"schema_name": cls.SCHEMA, "routine": "ORDER%"},
                columns=["address", "name", "kind", "status"],
            ),
            DescribeCase(
                tool=cls._of(ora.ora_sequences_describe),
                args={"schema_name": cls.SCHEMA},
                columns=["address", "name", "increment_by", "cache_size"],
            ),
        ]

    @staticmethod
    def _of(function: Any) -> PayloadTool:
        payload = ToolMain.toolset(function)[0]
        if not isinstance(payload, PayloadTool):
            raise AssertionError(f"{function}: expected a PayloadTool")

        return payload


async def _call(case: DescribeCase, connection: Any, **window: int) -> SqlResult:
    body = case.tool.coroutine
    if body is None:
        raise AssertionError(f"{case.tool.name}: body is a coroutine")

    result = await body(connection=connection, **case.args, **window)
    if not isinstance(result, SqlResult):
        raise AssertionError(f"{case.tool.name}: expected SqlResult")

    return result


@pytest.fixture(scope="module", params=[s.name for s in STAND.ora_sources])
async def target(request: pytest.FixtureRequest) -> AsyncIterator[Any]:
    """Цель стенда с пересозданной схемой TOOL_DEMO; профиль с минимальными правами.
    После модуля схема сносится: стенд общий со скрапером словаря."""
    source = STAND.source(request.param)
    demo = ToolDemo(source)
    await demo.recreate(ROWS)
    yield source
    await demo.drop()


class TestDescribeTools:
    @pytest.mark.parametrize("case", Cases.all(), ids=lambda c: c.tool.name)
    async def test_returns_declared_columns(
        self, case: DescribeCase, target: Any
    ) -> None:
        result = await _call(case, target.oracle, offset=0, limit=Cases.LIMIT)

        statement = result.statements[0]
        assert statement.rows, f"{case.tool.name}: no rows on {target.name}"
        assert len(statement.rows) <= Cases.LIMIT
        for column in case.columns:
            assert column in statement.rows[0], f"{case.tool.name}: no {column}"

    async def test_star_hides_oracle_schemas(self, target: Any) -> None:
        case = DescribeCase(
            tool=Cases._of(ora.ora_schema_describe),
            args={"schema_name": "*"},
            columns=["name"],
        )
        result = await _call(case, target.oracle, offset=0, limit=100)

        names = {row["name"] for row in result.statements[0].rows or ()}
        assert DemoUser.NAME.value in names
        assert "SYS" not in names
        assert "SYSTEM" not in names

    async def test_database_describe_is_one_row(self, target: Any) -> None:
        body = ToolMain.toolset(ora.ora_database_describe)[0].coroutine
        if body is None:
            raise AssertionError("body is a coroutine")

        result = await body(connection=target.oracle)

        rows = result.statements[0].rows
        assert rows is not None
        assert len(rows) == 1
        assert str(rows[0]["version"]).startswith("Oracle")
        assert rows[0]["charset"]


class TestQuery:
    async def test_select_is_windowed(self, target: Any) -> None:
        body = ToolMain.toolset(ora.ora_query)[0].coroutine
        if body is None:
            raise AssertionError("body is a coroutine")

        sql = _sql("select id, email from {table} order by id;")
        first = await body(connection=target.oracle, sql=sql, offset=0, limit=10)
        second = await body(connection=target.oracle, sql=sql, offset=10, limit=10)

        ids = [row["id"] for row in first.statements[0].rows]
        assert [int(i) for i in ids] == list(range(1, 11))
        assert int(second.statements[0].rows[0]["id"]) == 11
        assert "next offset" in first.statements[0].note

    async def test_dml_reports_affected_rows(self, target: Any) -> None:
        body = ToolMain.toolset(ora.ora_query)[0].coroutine
        if body is None:
            raise AssertionError("body is a coroutine")

        update = _sql("update {table} set note = 'x' where id <= 3")
        result = await body(connection=target.demo_owner, sql=update, offset=0, limit=1)

        assert result.statements[0].rows is None
        assert result.statements[0].affected_rows == 3

        check = _sql("select count(*) as n from {table} where note = 'x'")
        seen = await body(connection=target.oracle, sql=check, offset=0, limit=1)
        assert int(seen.statements[0].rows[0]["n"]) == 3

    async def test_plsql_block_runs(self, target: Any) -> None:
        body = ToolMain.toolset(ora.ora_query)[0].coroutine
        if body is None:
            raise AssertionError("body is a coroutine")

        result = await body(
            connection=target.oracle, sql="begin null; end;", offset=0, limit=1
        )

        assert result.statements[0].rows is None
