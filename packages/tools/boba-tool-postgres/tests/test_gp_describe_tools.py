"""Инструменты Greenplum на источниках gp/greenplum из ix_stand.sources."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

import pytest
from psycopg import sql
from pydantic import BaseModel

from boba.config import bind
from boba.db.postgres import PayloadPostgres
from boba.db.postgres.connection import PostgresConfig
from boba.tool.pg import tools as pg
from boba.toolkit.entry import ToolMain
from boba.toolkit.result import SqlResult, TableResult

pytestmark = [pytest.mark.integration, pytest.mark.anyio]


class Source(BaseModel):
    name: str
    postgres: PostgresConfig


class Sources(BaseModel):
    sources: Sequence[Source]


@dataclass(frozen=True)
class Catalog:
    connection: PostgresConfig
    schema: str

    async def rows(self, function: Any, **args: Any) -> Sequence[Mapping[str, Any]]:
        body = ToolMain.toolset(function)[0].coroutine
        assert body is not None

        result = await body(connection=self.connection, **args)
        if isinstance(result, TableResult):
            return result.rows

        assert isinstance(result, SqlResult)
        rows = result.statements[0].rows
        assert rows is not None

        return rows


@pytest.fixture
async def catalogs(raw_config: Any) -> AsyncIterator[Sequence[Catalog]]:
    sources = bind(raw_config, path="ix_stand", model=Sources)
    catalogs: list[Catalog] = []
    for source in sources.sources:
        if not source.name.lower().startswith(("gp", "greenplum")):
            continue

        catalogs.append(Catalog(source.postgres, f"boba_gp_{uuid4().hex}"))

    if not catalogs:
        pytest.skip("ix_stand.sources has no gp/greenplum source")

    try:
        for catalog in catalogs:
            conn = await PayloadPostgres.connect_config(catalog.connection)
            async with conn:
                await conn.execute(
                    sql.SQL(
                        "create schema {s}; "
                        "create table {s}.hashed(a int, b int, check(a > 0)) "
                        "distributed by (b, a); "
                        "create table {s}.randomized(a int) distributed randomly; "
                        "create table {s}.replicated(a int) distributed replicated; "
                        "create table {s}.columnar(a int) "
                        "with(appendonly=true, orientation=column) distributed by(a); "
                        "create index hashed_a on {s}.hashed(a); "
                        "create sequence {s}.seq; "
                        "create type {s}.state as enum('ready', 'done'); "
                        "create function {s}.echo(int) returns int "
                        "language sql as 'select $1'"
                    ).format(s=sql.Identifier(catalog.schema)),
                    prepare=False,
                )

        yield catalogs
    finally:
        for catalog in catalogs:
            conn = await PayloadPostgres.connect_config(catalog.connection)
            async with conn:
                await conn.execute(
                    sql.SQL("drop schema if exists {} cascade").format(
                        sql.Identifier(catalog.schema)
                    )
                )


async def test_greenplum_tools_describe_catalog(
    catalogs: Sequence[Catalog],
) -> None:
    for catalog in catalogs:
        for payload in pg.TOOLS:
            if not payload.name.startswith("gp_"):
                continue

            args: dict[str, Any] = {}
            for name in payload.args_schema.model_fields:
                if name == "connection":
                    continue

                args[name] = "*"

            for name, value in {
                "offset": 0,
                "limit": 10,
                "schema_name": catalog.schema,
                "sql": "select 42 as answer",
            }.items():
                if name in args:
                    args[name] = value

            rows = await catalog.rows(payload, **args)
            assert rows, f"{catalog.connection.host}: {payload.name} returned no rows"


async def test_greenplum_distribution_and_filtering(
    catalogs: Sequence[Catalog],
) -> None:
    for catalog in catalogs:
        args = {"schema_name": catalog.schema, "offset": 0, "limit": 20}
        rows = await catalog.rows(pg.gp_distribution_describe, table_name="*", **args)
        by_name = {row["relation"]: row for row in rows}
        assert by_name["hashed"]["policy_type"] == "hash"
        assert by_name["hashed"]["distribution_key"] == ["b", "a"]
        assert by_name["randomized"]["policy_type"] == "random"
        assert by_name["replicated"]["policy_type"] == "replicated"

        tables = await catalog.rows(pg.gp_table_describe, table_name="*", **args)
        by_table = {row["table_name"]: row for row in tables}
        assert by_table["columnar"]["storage"] == "ao_column"
        assert by_table["hashed"]["storage"] == "heap"
        assert by_table["hashed"]["distribution_key"] == ["b", "a"]
        assert by_table["randomized"]["distribution"] == "random"
        assert by_table["replicated"]["distribution"] == "replicated"

        matched = await catalog.rows(pg.gp_table_describe, table_name="hash%", **args)
        assert [row["table_name"] for row in matched] == ["hashed"]

        quoted = await catalog.rows(
            pg.gp_table_describe, table_name="x' or true --", **args
        )
        assert quoted == []

        first = await catalog.rows(pg.gp_column_describe, table_name="hashed", **args)
        args["offset"] = 1
        second = await catalog.rows(pg.gp_column_describe, table_name="hashed", **args)
        assert second == first[1:]
