"""Таблица feedbacks сервиса: DDL, upsert, выборка и очистка треда."""

from __future__ import annotations

from importlib.resources import files
from uuid import uuid4

import pytest
from psycopg import sql

from boba.chat.threads import StoredFeedback
from boba.db.postgres import AsyncPostgresPool, PostgresSchema
from boba.runtime.config import RuntimeConfig
from boba.runtime.elements import FeedbacksTable

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

SCHEMA = "automation_elements_test"


@pytest.fixture
async def tables(
    runtime_config: RuntimeConfig, test_database: str, pool: AsyncPostgresPool
) -> FeedbacksTable:
    async with pool.connection() as conn:
        await conn.execute(
            sql.SQL("drop schema if exists {} cascade").format(sql.Identifier(SCHEMA))
        )
    postgres = runtime_config.postgres
    cfg = postgres.model_copy(update={"dbname": test_database})
    schema_dir = files("boba.runtime") / "schema"
    async with pool.connection() as conn:
        await PostgresSchema(SCHEMA).apply_files(conn, schema_dir, {})

    return FeedbacksTable(cfg, SCHEMA, pool)


async def test_feedbacks_round_trip(tables: FeedbacksTable) -> None:
    thread_id = uuid4()
    step_id = uuid4()
    feedback = StoredFeedback(id=uuid4(), for_id=step_id, value=1, thread_id=thread_id)

    await tables.upsert(feedback)
    await tables.upsert(feedback.model_copy(update={"comment": "good"}))

    listed = await tables.list_of_thread(thread_id)
    assert [f.comment for f in listed] == ["good"]

    deleted = await tables.delete(feedback.id)
    assert deleted is not None
    assert deleted.for_id == step_id
    assert await tables.delete(feedback.id) is None

    await tables.upsert(feedback)
    await tables.delete_of_thread(thread_id)
    assert await tables.list_of_thread(thread_id) == []
