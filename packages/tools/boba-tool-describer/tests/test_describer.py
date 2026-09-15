"""Инструменты describer на живой базе: узлы и рёбра в тестовой базе стенда.

Тела вызываются напрямую с областью и конфигом, как их подаст хост; строки
проверяются своим запросом к таблицам.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import uuid4

import pytest
from psycopg import sql

from boba.connections.address import AddressError
from boba.db.clickhouse.address import ChNodeKind
from boba.db.postgres import AsyncPostgresPool
from boba.db.postgres.address import PgNodeKind
from boba.db.postgres.profile import PostgresConfig
from boba.identity.context import Scope, ScopeKind
from boba.tool.describer.address import EntityKind
from boba.tool.describer.store import EdgeKind, NodeMissingError, WriteAction
from boba.tool.describer.tools import (
    DescriberToolConfig,
    ListColumn,
    ListItem,
    describe_edge,
    describe_list,
    describe_node,
    describe_pg_address,
)
from boba.toolkit.entry import ToolMain

pytestmark = [pytest.mark.anyio, pytest.mark.integration]

SCHEMA = "describer_test"

PG_TABLE = "postgresql://dwh.local:5432/dwh?schema=dm&table=users"
PG_COLUMN = "postgresql://dwh.local:5432/dwh?schema=dm&table=users&column=id"
CH_COLUMN = "clickhouse://ch1:9000/logs?table=events&column=user_id"
ENTITY = "entity://customer"

Body = Callable[..., Awaitable[Any]]


def _body(tool: object) -> Body:
    body = ToolMain.toolset(tool)[0].coroutine
    if body is None:
        raise AssertionError("tool body is a coroutine")

    return body


@pytest.fixture
async def cfg(
    pool: AsyncPostgresPool, test_postgres: PostgresConfig
) -> DescriberToolConfig:
    async with pool.connection() as conn:
        await conn.execute(
            sql.SQL("drop schema if exists {} cascade").format(sql.Identifier(SCHEMA))
        )

    return DescriberToolConfig(connection=test_postgres, db_schema=SCHEMA)


@pytest.fixture
def scope() -> Scope:
    return Scope(kind=ScopeKind.CHAT, id=str(uuid4()))


async def _node(
    cfg: DescriberToolConfig, scope: Scope, kind: object, address: str, text: str
) -> dict[str, Any]:
    result = await _body(describe_node)(
        kind=kind, address=address, description=text, scope=scope, cfg=cfg
    )

    return dict(result.rows[0])


async def _edge(  # noqa: PLR0913 — аргументы вызова инструмента
    cfg: DescriberToolConfig,
    scope: Scope,
    source: str,
    target: str,
    kind: EdgeKind,
    text: str,
) -> dict[str, Any]:
    result = await _body(describe_edge)(
        source=source, target=target, kind=kind, description=text, scope=scope, cfg=cfg
    )

    return dict(result.rows[0])


async def _listing(cfg: DescriberToolConfig, scope: Scope) -> list[dict[str, Any]]:
    result = await _body(describe_list)(scope=scope, cfg=cfg)

    rows: list[dict[str, Any]] = []
    for row in result.rows:
        rows.append(dict(row))

    return rows


async def _count(pool: AsyncPostgresPool, table: str) -> int:
    async with pool.connection() as conn:
        cur = await conn.execute(
            sql.SQL("select count(*) from {}").format(sql.Identifier(SCHEMA, table))
        )
        row = await cur.fetchone()

    if row is None:
        raise AssertionError("count returns a row")

    return int(row[0])


async def test_node_is_upserted_by_address(
    cfg: DescriberToolConfig, scope: Scope, pool: AsyncPostgresPool
) -> None:
    first = await _node(cfg, scope, PgNodeKind.TABLE, PG_TABLE, "users of the shop")
    assert first["action"] == WriteAction.INSERTED
    assert first["url"] == PG_TABLE
    assert first["kind"] == PgNodeKind.TABLE

    shuffled = "postgresql://dwh.local:5432/dwh?table=users&schema=dm"
    second = await _node(cfg, scope, PgNodeKind.TABLE, shuffled, "registered users")
    assert second["action"] == WriteAction.UPDATED
    assert second["url"] == PG_TABLE

    assert await _count(pool, "node") == 1

    rows = await _listing(cfg, scope)
    assert rows[0][ListColumn.DESCRIPTION] == "registered users"


async def test_edge_links_nodes_across_systems(
    cfg: DescriberToolConfig, scope: Scope, pool: AsyncPostgresPool
) -> None:
    await _node(cfg, scope, PgNodeKind.COLUMN, PG_COLUMN, "user id")
    await _node(cfg, scope, ChNodeKind.COLUMN, CH_COLUMN, "user id in events")

    written = await _edge(
        cfg, scope, CH_COLUMN, PG_COLUMN, EdgeKind.IMPLICIT_KEY, "values match"
    )
    assert written["action"] == WriteAction.INSERTED
    assert written["source"] == CH_COLUMN
    assert written["target"] == PG_COLUMN

    again = await _edge(
        cfg, scope, CH_COLUMN, PG_COLUMN, EdgeKind.IMPLICIT_KEY, "checked by join"
    )
    assert again["action"] == WriteAction.UPDATED
    assert await _count(pool, "edge") == 1

    rows = await _listing(cfg, scope)
    edges: list[dict[str, Any]] = []
    for row in rows:
        if row[ListColumn.ITEM] == ListItem.EDGE:
            edges.append(row)

    assert len(edges) == 1
    assert edges[0][ListColumn.SOURCE] == CH_COLUMN
    assert edges[0][ListColumn.TARGET] == PG_COLUMN
    assert edges[0][ListColumn.KIND] == EdgeKind.IMPLICIT_KEY
    assert edges[0][ListColumn.DESCRIPTION] == "checked by join"


async def test_edge_to_undescribed_node_is_refused(
    cfg: DescriberToolConfig, scope: Scope, pool: AsyncPostgresPool
) -> None:
    await _node(cfg, scope, PgNodeKind.COLUMN, PG_COLUMN, "user id")

    with pytest.raises(NodeMissingError) as caught:
        await _edge(cfg, scope, PG_COLUMN, CH_COLUMN, EdgeKind.FOREIGN_KEY, "x")

    assert caught.value.url == CH_COLUMN
    assert caught.value.known == (PG_COLUMN,)
    assert await _count(pool, "edge") == 0


async def test_scopes_do_not_see_each_other(
    cfg: DescriberToolConfig, scope: Scope
) -> None:
    other = Scope(kind=ScopeKind.WORKFLOW, id=str(uuid4()))
    await _node(cfg, scope, PgNodeKind.COLUMN, PG_COLUMN, "user id")
    await _node(cfg, other, ChNodeKind.COLUMN, CH_COLUMN, "user id in events")

    assert len(await _listing(cfg, scope)) == 1
    assert len(await _listing(cfg, other)) == 1

    with pytest.raises(NodeMissingError):
        await _edge(cfg, scope, PG_COLUMN, CH_COLUMN, EdgeKind.IMPLICIT_KEY, "x")


async def test_entity_and_confluence_nodes(
    cfg: DescriberToolConfig, scope: Scope
) -> None:
    page = "https://cwiki.apache.org/confluence/rest/api/content/307136992"

    entity = await _node(cfg, scope, EntityKind.ENTITY, ENTITY, "a paying customer")
    assert entity["url"] == ENTITY

    await _node(cfg, scope, "confluence_page", page, "customer model page")
    await _node(cfg, scope, PgNodeKind.TABLE, PG_TABLE, "users")

    await _edge(cfg, scope, PG_TABLE, ENTITY, EdgeKind.SIMILAR, "table holds customers")
    await _edge(cfg, scope, page, ENTITY, EdgeKind.SIMILAR, "page describes customer")

    rows = await _listing(cfg, scope)
    assert len(rows) == 5


async def test_empty_scope_lists_nothing(
    cfg: DescriberToolConfig, scope: Scope
) -> None:
    result = await _body(describe_list)(scope=scope, cfg=cfg)

    assert result.rows == []
    assert result.note is not None


async def test_address_is_checked_against_kind(
    cfg: DescriberToolConfig, scope: Scope
) -> None:
    with pytest.raises(AddressError, match="matches none of its shapes"):
        await _node(cfg, scope, PgNodeKind.TABLE, PG_COLUMN, "x")

    with pytest.raises(AddressError, match="expected scheme postgresql"):
        await _node(cfg, scope, PgNodeKind.DATABASE, "clickhouse://ch1:9000/logs", "x")

    with pytest.raises(AddressError, match="unknown scheme"):
        await _edge(
            cfg, scope, "mysql://db1:3306/shop", PG_TABLE, EdgeKind.SIMILAR, "x"
        )


async def test_tables_survive_truncate_by_the_consumer(
    cfg: DescriberToolConfig, scope: Scope, pool: AsyncPostgresPool
) -> None:
    await _node(cfg, scope, PgNodeKind.TABLE, PG_TABLE, "users")

    async with pool.connection() as conn:
        await conn.execute(
            sql.SQL("truncate {}, {}").format(
                sql.Identifier(SCHEMA, "edge"), sql.Identifier(SCHEMA, "node")
            )
        )

    written = await _node(cfg, scope, PgNodeKind.TABLE, PG_TABLE, "users again")
    assert written["action"] == WriteAction.INSERTED
    assert await _count(pool, "node") == 1


async def test_pg_address_of_connection(test_postgres: PostgresConfig) -> None:
    result = await _body(describe_pg_address)(connection=test_postgres)

    row = dict(result.rows[0])
    assert row["url"].startswith("postgresql://")
    assert row["url"].endswith(f"/{test_postgres.dbname}")


async def test_parallel_calls_on_a_fresh_schema(
    cfg: DescriberToolConfig, scope: Scope, pool: AsyncPostgresPool
) -> None:
    """Модель зовёт несколько describe_node одним ответом, и хост исполняет их
    параллельно: схема и таблицы создаются под общим замком без гонки."""
    calls: list[Awaitable[dict[str, Any]]] = []
    for index in range(8):
        url = f"postgresql://dwh.local:5432/dwh?schema=dm&table=t{index}"
        calls.append(_node(cfg, scope, PgNodeKind.TABLE, url, f"table {index}"))

    written = await asyncio.gather(*calls)

    actions: list[str] = []
    for row in written:
        actions.append(row["action"])

    assert actions == [WriteAction.INSERTED] * 8
    assert await _count(pool, "node") == 8
