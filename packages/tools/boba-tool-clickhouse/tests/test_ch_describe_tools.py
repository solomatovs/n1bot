"""Describe-инструменты ch на живом ClickHouse стенда: тест сам создаёт базу
с двумя таблицами на источнике ix_stand, проверяет на ней каждый инструмент и
сносит её. Каждый инструмент отдаёт страницу с объявленными колонками,
серверные параметры сужают выдачу, окно листается."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from enum import StrEnum
from typing import Any, ClassVar

import pytest
from pydantic import BaseModel, ConfigDict

from boba.config import bind
from boba.db.clickhouse.connection import ClickHouseConfig
from boba.db.clickhouse.payload import PayloadClickHouse
from boba.db.clickhouse.query import ChQueryBuilder
from boba.stand.edm import EdmSource, EdmSources
from boba.stand.names import StandNames
from boba.tool.ch import tools as ch
from boba.toolkit.entry import ToolMain
from boba.toolkit.facade import PayloadTool
from boba.toolkit.result import SqlResult

pytestmark = [pytest.mark.integration, pytest.mark.anyio]


class StandTable(StrEnum):
    """Таблицы базы теста; значение — имя таблицы."""

    CLIENTS = "clients"
    ORDERS = "orders"

    def ddl(self) -> str:
        """Колонки, движок и комментарий таблицы — то, что обязаны показать
        инструменты описания."""
        known = {
            StandTable.CLIENTS: (
                "(id UInt64 comment 'client id', name String comment 'client name') "
                "engine = MergeTree order by id comment 'clients of the stand'"
            ),
            StandTable.ORDERS: (
                "(id UInt64 comment 'order id', client_id UInt64, "
                "amount Decimal(18, 2) comment 'amount in rub') "
                "engine = MergeTree order by (client_id, id) "
                "comment 'orders of the stand'"
            ),
        }

        return known[self]


class DescribeDataset:
    """База теста на источнике стенда: две таблицы с известными колонками.
    Имя несёт метку набора и процесса xdist, поэтому параллельные процессы
    не делят базу; recreate убирает остатки оборванного прогона, drop сносит
    созданное."""

    DATABASE: ClassVar[str] = StandNames().of("describe_stand")

    def __init__(self, source: EdmSource) -> None:
        self._source = source

    async def recreate(self) -> None:
        async with PayloadClickHouse.opened_config(self._source.admin) as client:
            await self._command(client, "drop database if exists {db:Identifier}")
            await self._command(client, "create database {db:Identifier}")

            for table in StandTable:
                ddl = "create table {db:Identifier}.{table:Identifier} " + table.ddl()
                await self._command(client, ddl, table=table.value)

    async def drop(self) -> None:
        async with PayloadClickHouse.opened_config(self._source.admin) as client:
            await self._command(client, "drop database if exists {db:Identifier}")

    async def _command(self, client: Any, text: str, **bind: Any) -> None:
        query = ChQueryBuilder().add(text, db=self.DATABASE, **bind).build()
        await client.command(query.text, parameters=query.params)


class DescribeCase(BaseModel):
    """Инструмент, аргументы вызова и колонки, которые он обещает."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    tool: PayloadTool
    args: dict[str, Any]
    columns: Sequence[str]


class Cases:
    """Describe-инструменты над базой теста и над справочниками сервера."""

    LIMIT: ClassVar[int] = 5

    @classmethod
    def all(cls) -> list[DescribeCase]:
        return [
            DescribeCase(
                tool=cls.of(ch.ch_database_describe),
                args={"database": DescribeDataset.DATABASE},
                columns=["address", "name", "engine", "comment"],
            ),
            *cls.listings(),
        ]

    @classmethod
    def listings(cls) -> list[DescribeCase]:
        """Инструменты, у которых на базе теста заведомо больше одной строки."""
        return [
            DescribeCase(
                tool=cls.of(ch.ch_table_describe),
                args={"database": DescribeDataset.DATABASE, "table": "*"},
                columns=["address", "database", "name", "engine", "sorting_key"],
            ),
            DescribeCase(
                tool=cls.of(ch.ch_column_describe),
                args={"database": DescribeDataset.DATABASE, "table": "*"},
                columns=["address", "database", "table", "name", "type", "comment"],
            ),
            DescribeCase(
                tool=cls.of(ch.ch_function_describe),
                args={"function": "array%"},
                columns=["address", "name", "is_aggregate", "origin"],
            ),
            DescribeCase(
                tool=cls.of(ch.ch_types_describe),
                args={"name": "UInt%"},
                columns=["address", "name", "case_insensitive", "alias_to"],
            ),
        ]

    @classmethod
    def tables(cls, table: str) -> DescribeCase:
        return DescribeCase(
            tool=cls.of(ch.ch_table_describe),
            args={"database": DescribeDataset.DATABASE, "table": table},
            columns=[],
        )

    @classmethod
    def table_columns(cls, table: str) -> DescribeCase:
        return DescribeCase(
            tool=cls.of(ch.ch_column_describe),
            args={"database": DescribeDataset.DATABASE, "table": table},
            columns=[],
        )

    @staticmethod
    def of(function: Any) -> PayloadTool:
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
def sources(raw_config: Any) -> list[EdmSource]:
    listed = bind(raw_config, path="ix_stand", model=EdmSources).demo()
    if not listed:
        raise AssertionError(
            "stand.toml: [ix_stand].ch_sources has no source with demo = true, "
            "the test needs a ClickHouse server where it may create a database"
        )

    return listed


@pytest.fixture(scope="module")
async def runner(sources: list[EdmSource]) -> AsyncIterator[Runner]:
    """Самый новый сервер стенда с базой теста: она создаётся перед модулем и
    сносится после него, чем бы ни кончились проверки. Старые серверы не
    берутся: ch_function_describe читает колонки system.functions, которых
    у 22.12 нет."""
    source = sources[-1]

    dataset = DescribeDataset(source)
    try:
        await dataset.recreate()
        yield Runner(source.clickhouse)
    finally:
        await dataset.drop()


class TestDescribeTools:
    @pytest.mark.parametrize("case", Cases.all(), ids=lambda case: case.tool.name)
    async def test_page_has_the_declared_columns(
        self, case: DescribeCase, runner: Runner
    ) -> None:
        rows = runner.rows(await runner.page(case, offset=0, limit=Cases.LIMIT))

        assert rows, f"{case.tool.name}: no rows for {case.args}"
        missing = [column for column in case.columns if column not in rows[0]]
        assert missing == [], f"{case.tool.name}: columns missing: {missing}"

    @pytest.mark.parametrize("case", Cases.listings(), ids=lambda case: case.tool.name)
    async def test_window_moves_by_offset(
        self, case: DescribeCase, runner: Runner
    ) -> None:
        first = runner.rows(await runner.page(case, offset=0, limit=2))
        second = runner.rows(await runner.page(case, offset=1, limit=2))

        assert len(first) == 2, f"{case.tool.name}: two rows for {case.args}"
        assert second[0] == first[1], f"{case.tool.name}: offset=1 must start at row 2"

    async def test_system_databases_stay_hidden(self, runner: Runner) -> None:
        case = DescribeCase(
            tool=Cases.of(ch.ch_database_describe), args={"database": "*"}, columns=[]
        )
        rows = runner.rows(await runner.page(case, offset=0, limit=100))

        names = {row["name"] for row in rows}
        assert not names & {"system", "INFORMATION_SCHEMA", "information_schema"}

    async def test_database_is_described_by_its_name(self, runner: Runner) -> None:
        case = Cases.all()[0]
        rows = runner.rows(await runner.page(case, offset=0, limit=10))

        assert [(row["address"], row["name"]) for row in rows] == [
            (DescribeDataset.DATABASE, DescribeDataset.DATABASE)
        ]

    async def test_tables_come_with_engine_keys_and_comment(
        self, runner: Runner
    ) -> None:
        rows = runner.rows(await runner.page(Cases.tables("*"), offset=0, limit=10))

        listed = [
            (row["address"], row["engine"], row["sorting_key"], row["comment"])
            for row in rows
        ]
        database = DescribeDataset.DATABASE
        assert listed == [
            (f"{database}.clients", "MergeTree", "id", "clients of the stand"),
            (f"{database}.orders", "MergeTree", "client_id, id", "orders of the stand"),
        ]

    async def test_exact_filter_narrows_to_one_table(self, runner: Runner) -> None:
        exact = Cases.tables(StandTable.ORDERS.value)
        rows = runner.rows(await runner.page(exact, offset=0, limit=10))

        assert [(row["database"], row["name"]) for row in rows] == [
            (DescribeDataset.DATABASE, StandTable.ORDERS.value)
        ]

    async def test_like_filter_matches_by_prefix(self, runner: Runner) -> None:
        rows = runner.rows(await runner.page(Cases.tables("cli%"), offset=0, limit=10))

        assert [row["name"] for row in rows] == [StandTable.CLIENTS.value]

    async def test_columns_come_in_order_with_types_and_comments(
        self, runner: Runner
    ) -> None:
        case = Cases.table_columns(StandTable.ORDERS.value)
        rows = runner.rows(await runner.page(case, offset=0, limit=10))

        listed = [(row["name"], row["type"], row["comment"]) for row in rows]
        assert listed == [
            ("id", "UInt64", "order id"),
            ("client_id", "UInt64", ""),
            ("amount", "Decimal(18, 2)", "amount in rub"),
        ]
        assert rows[0]["address"] == f"{DescribeDataset.DATABASE}.orders.id"
        assert [row["is_in_sorting_key"] for row in rows] == [1, 1, 0]

    async def test_quote_in_a_value_is_a_value_not_sql(self, runner: Runner) -> None:
        case = Cases.tables("x' or 1=1 --")

        assert runner.rows(await runner.page(case, offset=0, limit=10)) == []
