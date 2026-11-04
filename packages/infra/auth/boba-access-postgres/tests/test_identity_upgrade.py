"""Явный переход старых users: UUID сохраняется, неправильная личность откатывается."""

from pathlib import Path
from uuid import UUID, uuid4

import pytest
from psycopg import sql
from psycopg.errors import CheckViolation
from psycopg.types.json import Jsonb

from boba.db.postgres import AsyncPostgresPool, PgQueryBuilder
from boba.identity.principal import UserIdentity
from boba.identity.session import Login
from boba.identity_postgres import UsersTable

pytestmark = pytest.mark.anyio
UPGRADE = (
    Path(__file__).resolve().parents[5]
    / "docs/migrations/2026-11-03-database-roles/identity.sql"
)


@pytest.fixture
async def legacy(pool: AsyncPostgresPool) -> tuple[str, UUID]:
    schema = "identity_upgrade_test"
    user = uuid4()
    query = (
        PgQueryBuilder(schema=sql.Identifier(schema))
        .add("""
        drop schema if exists {schema} cascade;
        create schema {schema};
        create table {schema}.users (
            id uuid primary key, identifier text not null unique,
            created_at timestamptz not null default now(),
            meta jsonb not null, tags text[]
        );
        create table {schema}.roles (
            id uuid primary key default gen_random_uuid(), role text not null unique
        );
        create table {schema}.user_roles (
            user_id uuid references {schema}.users(id),
            role_id uuid references {schema}.roles(id),
            primary key(user_id, role_id)
        );
    """)
        .build()
    )
    async with pool.connection() as conn:
        await conn.execute(query.text)
        query = (
            PgQueryBuilder(schema=sql.Identifier(schema))
            .add(
                "insert into {schema}.users (id, identifier, meta) "
                "values (%(id)s, 'office:alice', %(meta)s)",
                id=user,
                meta=Jsonb(
                    {
                        "identity": {"realm": "office", "login": "alice"},
                        "roles": ["reader"],
                        "provider": "KerberosAuth",
                    }
                ),
            )
            .build()
        )
        await conn.execute(query.text, query.params)
        await conn.execute(
            sql.SQL("insert into {}.roles (role) values ('reader')").format(
                sql.Identifier(schema)
            )
        )
        await conn.execute(
            sql.SQL("insert into {}.user_roles select %s, id from {}.roles").format(
                sql.Identifier(schema), sql.Identifier(schema)
            ),
            (user,),
        )
    return schema, user


async def test_upgrade_preserves_user_and_membership_and_removes_metadata(
    pool: AsyncPostgresPool,
    legacy: tuple[str, UUID],
) -> None:
    schema, user = legacy
    query = (
        PgQueryBuilder(schema=sql.Identifier(schema)).add(UPGRADE.read_text()).build()
    )
    async with pool.connection() as conn, conn.transaction():
        await conn.execute(query.text)
    stored = await UsersTable(None, schema, pool).stored_by_id(user)
    assert stored is not None
    assert stored.id == user
    assert stored.identity == UserIdentity(realm="office", login=Login("alice"))
    assert stored.roles == {"reader"}
    async with pool.connection() as conn:
        assert await (
            await conn.execute(
                sql.SQL("select managed from {}.user_roles").format(
                    sql.Identifier(schema)
                )
            )
        ).fetchone() == (True,)
        columns = await (
            await conn.execute(
                "select column_name from information_schema.columns "
                "where table_schema = %s and table_name = 'users'",
                (schema,),
            )
        ).fetchall()
    assert not {"meta", "tags"} & {row[0] for row in columns}


async def test_upgrade_rejects_conflicting_identity_atomically(
    pool: AsyncPostgresPool,
    legacy: tuple[str, UUID],
) -> None:
    schema, _ = legacy
    async with pool.connection() as conn:
        await conn.execute(
            sql.SQL("update {}.users set identifier = 'office:bob'").format(
                sql.Identifier(schema)
            )
        )
    query = (
        PgQueryBuilder(schema=sql.Identifier(schema)).add(UPGRADE.read_text()).build()
    )
    with pytest.raises(CheckViolation):
        async with pool.connection() as conn, conn.transaction():
            await conn.execute(query.text)
    async with pool.connection() as conn:
        columns = await (
            await conn.execute(
                "select column_name from information_schema.columns "
                "where table_schema = %s and table_name = 'users'",
                (schema,),
            )
        ).fetchall()
    assert {"meta", "tags"} <= {row[0] for row in columns}
    assert "realm" not in {row[0] for row in columns}
