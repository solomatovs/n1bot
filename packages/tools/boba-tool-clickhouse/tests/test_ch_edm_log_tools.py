"""Витрина ЕДМ и поиск query_log на ClickHouse тестового стенда."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from typing import Any
from uuid import uuid4

import pytest
from pydantic import SecretStr

from boba.config import bind
from boba.db.clickhouse.connection import ClickHouseConfig, PasswordAuth
from boba.db.clickhouse.payload import PayloadClickHouse
from boba.tool.ch import tools as ch
from boba.toolkit.entry import ToolMain
from boba.toolkit.result import SqlResult

pytestmark = [pytest.mark.integration, pytest.mark.anyio]


@pytest.fixture
def connection(raw_config: Any) -> ClickHouseConfig:
    return bind(raw_config, path="clickhouse", model=ClickHouseConfig)


class Runner:
    def __init__(self, connection: ClickHouseConfig) -> None:
        self._connection = connection

    async def rows(self, function: Any, **args: Any) -> Sequence[Mapping[str, Any]]:
        body = ToolMain.toolset(function)[0].coroutine
        assert body is not None

        result = await body(connection=self._connection, **args)
        assert isinstance(result, SqlResult)
        rows = result.statements[0].rows
        assert rows is not None

        return rows


@pytest.fixture
async def edm(connection: ClickHouseConfig) -> AsyncIterator[Runner]:
    async with PayloadClickHouse.opened_config(connection) as client:
        exists = await client.command("exists table cmn_cds_test.dm_edm_describe")
        if exists:
            pytest.skip("cmn_cds_test.dm_edm_describe already exists; preserving it")

        await client.command("create database if not exists cmn_cds_test")
        await client.command(
            "create table cmn_cds_test.dm_edm_describe ("
            "system_name String, db_name String, schema_name String, "
            "table_name String, "
            "column_name Nullable(String), "
            "table_short_description_edm Nullable(String), "
            "table_extended_description_edm Nullable(String), "
            "column_short_description_edm Nullable(String), "
            "column_extended_description_edm Nullable(String)) engine=Memory"
        )
        try:
            await client.command(
                "insert into cmn_cds_test.dm_edm_describe values "
                "('erp','dwh','dm','orders','id','Orders',NULL,'Identifier',NULL),"
                "('erp','dwh','dm','orders','amount','Orders',NULL,'Amount',NULL),"
                "('crm','dwh','dm','users','id','Users',NULL,'Identifier',NULL),"
                "('erp','dwh','dm','empty','id',NULL,NULL,NULL,NULL),"
                "('erp','dwh','dm','table_only',NULL,'Table',NULL,NULL,NULL)"
            )
            yield Runner(connection)
        finally:
            await client.command("drop table cmn_cds_test.dm_edm_describe")


async def test_edm_filters_and_pagination(edm: Runner) -> None:
    args = {
        "db_name": "dw%",
        "schema_name": "dm",
        "table_name": "*",
        "offset": 0,
        "limit": 10,
    }
    rows = await edm.rows(ch.ch_edm_general_describe, **args)
    assert len(rows) == 3

    filtered = await edm.rows(
        ch.ch_edm_general_describe,
        system_name="erp",
        sql_where_stmt="and column_name = 'amount'",
        **args,
    )
    assert [row["column_name"] for row in filtered] == ["amount"]

    args["table_name"] = "ord%"
    args["limit"] = 1
    first = await edm.rows(ch.ch_edm_general_describe, **args)
    args["offset"] = 1
    second = await edm.rows(ch.ch_edm_general_describe, **args)
    assert first[0]["column_name"] == "amount"
    assert second[0]["column_name"] == "id"

    args["table_name"] = "x' or 1=1 --"
    assert await edm.rows(ch.ch_edm_general_describe, **args) == []


@pytest.fixture
async def query_log(connection: ClickHouseConfig) -> AsyncIterator[str]:
    suffix = uuid4().hex
    database = f"boba_log_{suffix}"
    user = f"boba.log_{suffix}"
    password = uuid4().hex
    auth = PasswordAuth(method="password", user=user, password=SecretStr(password))
    author = connection.model_copy(update={"auth": auth})

    async with PayloadClickHouse.opened_config(connection) as client:
        await client.command(f"create database {database}")
        try:
            await client.command(
                "create user {user:Identifier} identified with sha256_password "
                "by {password:String}",
                parameters={"user": user, "password": password},
            )
            await client.command(f"grant select on {database}.* to `{user}`")
            await client.command(
                f"create table {database}.left_t(id Int32) engine=Memory"
            )
            await client.command(
                f"create table {database}.right_t(id Int32) engine=Memory"
            )
            await client.command(
                "insert into {db:Identifier}.left_t values (1)",
                parameters={"db": database},
            )
            await client.command(
                "insert into {db:Identifier}.right_t values (1)",
                parameters={"db": database},
            )
            async with PayloadClickHouse.opened_config(author) as writer:
                for index in range(3):
                    await writer.query(
                        "select l.id, {marker:Int32} as marker "
                        "from {db:Identifier}.left_t l "
                        "join {db:Identifier}.right_t r on l.id=r.id",
                        parameters={"db": database, "marker": index},
                        settings={"log_queries": 1},
                    )

            await client.command("system flush logs")
            yield database
        finally:
            await client.command(f"drop user if exists `{user}`")
            await client.command(f"drop database {database}")


async def test_query_log_filters_database_table_and_offset(
    connection: ClickHouseConfig, query_log: str
) -> None:
    runner = Runner(connection)
    args = {"db_name": query_log, "table_name": "*", "offset": 0, "limit": 20}
    rows = await runner.rows(ch.ch_query_log_describe, **args)
    assert len(rows) >= 3
    assert all(query_log in row["databases"] for row in rows)

    args["table_name"] = "left%"
    matched = await runner.rows(ch.ch_query_log_describe, **args)
    assert matched == rows

    args["offset"] = 1
    args["limit"] = 2
    page = await runner.rows(ch.ch_query_log_describe, **args)
    assert page == rows[1:3]

    args["table_name"] = "x' or 1=1 --"
    assert await runner.rows(ch.ch_query_log_describe, **args) == []
