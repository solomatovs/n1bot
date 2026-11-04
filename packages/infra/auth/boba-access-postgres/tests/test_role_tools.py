"""Администратор меняет права в БД, работающий процесс видит их сразу."""

import pytest
from psycopg import sql

from boba.access import ProfileGrant, ToolAccess
from boba.access_postgres.roles import RoleToolsTable
from boba.db.postgres import AsyncPostgresPool, PostgresSchema
from boba.stand.roles import RoleSeed

pytestmark = pytest.mark.anyio
SCHEMA = "role_tools_test"


@pytest.fixture
async def permissions(pool: AsyncPostgresPool) -> RoleToolsTable:
    async with pool.connection() as conn:
        await conn.execute(
            sql.SQL("drop schema if exists {} cascade").format(sql.Identifier(SCHEMA))
        )
        await PostgresSchema(SCHEMA).apply_packages(
            conn, ("boba.identity_postgres", "boba.access_postgres")
        )
    return RoleToolsTable(None, SCHEMA, pool)


async def test_empty_catalog_denies_all_tools(permissions: RoleToolsTable) -> None:
    access = ToolAccess(
        ["query"],
        {},
        {"general": ProfileGrant(roles=["*"], tools=["*"])},
        role_permissions=permissions,
    )
    assert not await permissions.tools_for_roles(frozenset())
    assert not (await access.decide_for("query", [], "general")).allowed
    assert not (await access.decide_for("query", ["unknown"], "general")).allowed


async def test_permissions_change_without_restarting_and_deleted_roles_lose_grants(
    permissions: RoleToolsTable,
) -> None:
    access = ToolAccess(
        ["query", "write"],
        {},
        {"general": ProfileGrant(roles=["*"], tools=["*"])},
        role_permissions=permissions,
    )
    await RoleSeed(permissions).tools({"reader": ["query"]})
    assert (await access.decide_for("query", ["reader"], "general")).allowed
    assert not (await access.decide_for("write", ["reader"], "general")).allowed
    await RoleSeed(permissions).tools({"reader": ["*"]})
    assert (await access.decide_for("write", ["reader"], "general")).allowed
    query = (
        permissions._query()
        .add("delete from {schema}.roles where role = 'reader'")
        .build()
    )
    await permissions._execute(query, "administrator deletes role")
    assert not (await access.decide_for("query", ["reader"], "general")).allowed
    assert not await permissions.tools_for_roles(frozenset({"reader"}))


async def test_global_policy_covers_only_existing_roles(
    permissions: RoleToolsTable,
) -> None:
    await RoleSeed(permissions).tools({"*": ["*"]})
    assert not await permissions.tools_for_roles(frozenset())
    assert not await permissions.tools_for_roles(frozenset({"unknown"}))
    await RoleSeed(permissions).add(["reader"])
    assert await permissions.tools_for_roles(frozenset({"reader"})) == {"*"}
