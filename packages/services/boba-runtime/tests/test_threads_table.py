"""Таблица threads сервиса: DDL, upsert с правкой meta, список владельца, автор."""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from psycopg import sql

from boba.chat.threads import DataRejectedError, ThreadUpsert
from boba.db.postgres import AsyncPostgresPool
from boba.identity.signin import SignedIn
from boba.runtime.config import RuntimeConfig
from boba.runtime.threads import ThreadsTable
from boba.runtime.users import UsersTable

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

SCHEMA = "automation_threads_test"


@pytest.fixture
async def tables(
    runtime_config: RuntimeConfig, test_database: str, pool: AsyncPostgresPool
) -> tuple[UsersTable, ThreadsTable]:
    async with pool.connection() as conn:
        await conn.execute(
            sql.SQL("drop schema if exists {} cascade").format(sql.Identifier(SCHEMA))
        )
    postgres = runtime_config.data_layer.postgres
    cfg = postgres.model_copy(update={"dbname": test_database})
    users = UsersTable(cfg, SCHEMA, pool)
    threads = ThreadsTable(cfg, SCHEMA, pool)
    await users.setup()
    await threads.setup()
    await threads.setup()

    return users, threads


async def test_upsert_get_list_and_author(
    tables: tuple[UsersTable, ThreadsTable],
) -> None:
    users, threads = tables
    owner = await users.ensure_user(
        SignedIn(identifier="owner", display_name="owner", metadata={})
    )
    thread_id = uuid4()

    created = await threads.upsert(
        ThreadUpsert(id=thread_id, user_id=UUID(owner.id), meta_set={"a": 1, "b": 2})
    )
    assert created.inserted
    assert created.user_id == UUID(owner.id)

    renamed = await threads.upsert(
        ThreadUpsert(id=thread_id, name="renamed", meta_set={"c": 3}, meta_del=["a"])
    )
    assert not renamed.inserted
    assert renamed.name == "renamed"

    stored = await threads.get(thread_id)
    assert stored is not None
    assert stored.name == "renamed"
    assert stored.user_id == UUID(owner.id)
    assert dict(stored.meta) == {"b": 2, "c": 3}

    listed = await threads.list_of(UUID(owner.id), 10)
    assert [t.id for t in listed] == [thread_id]

    assert await threads.get_thread_author(str(thread_id)) == "owner"

    assert await threads.delete(thread_id) == UUID(owner.id)
    assert await threads.get(thread_id) is None
    assert await threads.delete(thread_id) is None


async def test_author_of_missing_thread_is_rejected(
    tables: tuple[UsersTable, ThreadsTable],
) -> None:
    _, threads = tables

    with pytest.raises(DataRejectedError):
        await threads.get_thread_author(str(uuid4()))


async def test_user_rows_and_llm_settings(
    tables: tuple[UsersTable, ThreadsTable],
) -> None:
    users, _ = tables
    stored = await users.upsert("reader", {"roles": ["DEV"]})

    assert (await users.stored_by_id(stored.id)) == stored
    assert (await users.stored("reader")) == stored

    await users.set_llm_settings(stored.id, "general", {"temperature": 0.2})
    again = await users.stored("reader")
    assert again is not None
    assert again.meta["llm"] == {"general": {"temperature": 0.2}}

    await users.set_llm_settings(stored.id, "general", {})
    cleared = await users.stored("reader")
    assert cleared is not None
    assert cleared.meta["llm"] == {}
