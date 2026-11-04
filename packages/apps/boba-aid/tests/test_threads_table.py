"""Таблица threads сервиса: DDL и миграция на месте, upsert с правкой meta,
сдвиг активности, список владельца страницами по активности, автор."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from psycopg import sql

from boba.aid.data.threads import ThreadsTable
from boba.aid.data.users import UsersTable
from boba.aid.infra.config import AppConfig
from boba.chat.threads import (
    DataRejectedError,
    ThreadCursor,
    ThreadCursors,
    ThreadUpsert,
)
from boba.db.postgres import AsyncPostgresPool, PostgresSchema
from boba.identity.principal import UserIdentity
from boba.identity.session import Login

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

SCHEMA = "automation_threads_test"


def _identity(login: str) -> UserIdentity:
    return UserIdentity(realm="test", login=Login(login))


@pytest.fixture
async def tables(
    pool: AsyncPostgresPool,
) -> tuple[UsersTable, ThreadsTable]:
    async with pool.connection() as conn:
        await conn.execute(
            sql.SQL("drop schema if exists {} cascade").format(sql.Identifier(SCHEMA))
        )
    async with pool.connection() as conn:
        await PostgresSchema(SCHEMA).apply_packages(
            conn, ("boba.identity_postgres", "boba.aid")
        )

    return UsersTable(pool, SCHEMA), ThreadsTable(pool, SCHEMA)


async def test_upsert_get_list_and_author(
    tables: tuple[UsersTable, ThreadsTable],
) -> None:
    users, threads = tables
    owner = await users.upsert(_identity("owner"))
    thread_id = uuid4()

    created = await threads.upsert(
        ThreadUpsert(id=thread_id, user_id=owner.id, meta_set={"a": 1, "b": 2})
    )
    assert created.inserted
    assert created.user_id == owner.id

    renamed = await threads.upsert(
        ThreadUpsert(id=thread_id, name="renamed", meta_set={"c": 3}, meta_del=["a"])
    )
    assert not renamed.inserted
    assert renamed.name == "renamed"

    stored = await threads.get(thread_id)
    assert stored is not None
    assert stored.name == "renamed"
    assert stored.user_id == owner.id
    assert dict(stored.meta) == {"b": 2, "c": 3}

    listed = await threads.list_of(owner.id, 10, None)
    assert [t.id for t in listed] == [thread_id]
    assert listed[0].updated_at == listed[0].created_at

    assert await threads.get_thread_author(str(thread_id)) == "test:owner"

    assert await threads.delete(thread_id) == owner.id
    assert await threads.get(thread_id) is None
    assert await threads.delete(thread_id) is None


async def test_author_of_missing_thread_is_rejected(
    tables: tuple[UsersTable, ThreadsTable],
) -> None:
    _, threads = tables

    with pytest.raises(DataRejectedError):
        await threads.get_thread_author(str(uuid4()))


async def test_user_rows(
    tables: tuple[UsersTable, ThreadsTable],
) -> None:
    users, _ = tables
    identity = UserIdentity(realm="test", login=Login("reader"))
    stored = await users.upsert(identity)

    assert (await users.stored_by_id(stored.id)) == stored
    assert (await users.stored(identity.identifier)) == stored


async def test_activity_moves_forward_only(
    tables: tuple[UsersTable, ThreadsTable],
) -> None:
    users, threads = tables
    owner = await users.upsert(_identity("active"))
    older = uuid4()
    newer = uuid4()
    await threads.upsert(ThreadUpsert(id=older, user_id=owner.id))
    await threads.upsert(ThreadUpsert(id=newer, user_id=owner.id))
    created = await threads.get(older)
    assert created is not None

    later = created.updated_at + timedelta(hours=1)
    touched = await threads.touch(older, later)
    assert touched is not None
    assert touched.user_id == owner.id
    assert touched.updated_at == later

    listed = await threads.list_of(owner.id, 10, None)
    assert [t.id for t in listed] == [older, newer]

    assert await threads.touch(older, later) is None
    assert await threads.touch(older, created.updated_at) is None
    assert await threads.touch(uuid4(), later) is None

    stored = await threads.get(older)
    assert stored is not None
    assert stored.updated_at == later
    assert stored.created_at == created.created_at


async def test_pages_follow_activity_without_repeats_or_gaps(
    tables: tuple[UsersTable, ThreadsTable],
) -> None:
    """Страницы по (updated_at, id) курсора: тред, сдвинутый между страницами,
    и удалённый тред-курсор не дают ни повторов, ни пропусков."""
    users, threads = tables
    owner = await users.upsert(_identity("pager"))
    base = datetime.now(UTC) + timedelta(days=1)
    ids: list[UUID] = []
    for index in range(7):
        thread_id = uuid4()
        await threads.upsert(ThreadUpsert(id=thread_id, user_id=owner.id))
        await threads.touch(thread_id, base + timedelta(minutes=index))
        ids.append(thread_id)

    tied = uuid4()
    await threads.upsert(ThreadUpsert(id=tied, user_id=owner.id))
    await threads.touch(tied, base + timedelta(minutes=3))

    newest_first = await threads.list_of(owner.id, 100, None)
    cursors = ThreadCursors()

    first = await threads.list_of(owner.id, 3, None)
    assert first == newest_first[:3]

    cursor = ThreadCursor(updated_at=first[-1].updated_at, id=first[-1].id)
    assert cursors.parse(cursor.render()) == cursor

    await threads.touch(first[-1].id, base + timedelta(days=2))
    await threads.delete(first[-1].id)
    second = await threads.list_of(owner.id, 3, cursors.parse(cursor.render()))
    assert second == newest_first[3:6]

    last = ThreadCursor(updated_at=second[-1].updated_at, id=second[-1].id)
    third = await threads.list_of(owner.id, 3, last)
    assert third == newest_first[6:]

    listed: list[UUID] = []
    for stored in [*first, *second, *third]:
        listed.append(stored.id)

    assert sorted(listed) == sorted([*ids, tied])


def test_malformed_cursor_is_rejected() -> None:
    cursors = ThreadCursors()

    with pytest.raises(DataRejectedError, match="thread list cursor"):
        cursors.parse("not-a-cursor")

    with pytest.raises(DataRejectedError, match="no iso time"):
        cursors.parse(f"yesterday/{uuid4()}")

    with pytest.raises(DataRejectedError, match="no thread uuid"):
        cursors.parse("2026-01-01T00:00:00+00:00/not-a-uuid")


async def test_threads_without_updated_at_are_migrated_in_place(
    runtime_config: AppConfig, test_database: str, pool: AsyncPostgresPool
) -> None:
    """Таблица до updated_at с данными: накат файлов схемы добавляет колонку,
    заполняет её created_at и делает not null; повтор безвреден."""
    schema = "automation_threads_migrate_test"
    ident = sql.Identifier(schema)
    created = datetime(2025, 5, 1, 12, 30, tzinfo=UTC)
    thread_id = uuid4()
    async with pool.connection() as conn:
        await conn.execute(sql.SQL("drop schema if exists {} cascade").format(ident))
        await conn.execute(sql.SQL("create schema {}").format(ident))
        await conn.execute(
            sql.SQL(
                "create table {}.threads (id uuid primary key, created_at timestamptz "
                "not null, name text, user_id uuid, tags text[], meta jsonb)"
            ).format(ident)
        )
        await conn.execute(
            sql.SQL(
                "insert into {}.threads (id, created_at, name) values (%s, %s, %s)"
            ).format(ident),
            (thread_id, created, "old"),
        )

    async with pool.connection() as conn:
        await PostgresSchema(schema).apply_packages(
            conn, ("boba.identity_postgres", "boba.aid")
        )
        await PostgresSchema(schema).apply_packages(
            conn, ("boba.identity_postgres", "boba.aid")
        )
        cur = await conn.execute(
            sql.SQL(
                "select is_nullable from information_schema.columns "
                "where table_schema = %s and table_name = 'threads' "
                "and column_name = 'updated_at'"
            ),
            (schema,),
        )
        nullable = await cur.fetchone()

    assert nullable == ("NO",)

    stored = await ThreadsTable(pool, schema).get(thread_id)
    assert stored is not None
    assert stored.name == "old"
    assert stored.created_at == created
    assert stored.updated_at == created
