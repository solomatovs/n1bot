"""Таблица feedbacks сервиса: DDL, upsert, выборка и очистка треда."""

from __future__ import annotations

from uuid import uuid4

import pytest
from psycopg import sql

from boba.aid.data.feedbacks import FeedbacksTable
from boba.chat.threads import StoredFeedback
from boba.db.postgres import AsyncPostgresPool
from boba.db.postgres.connection import PostgresConfig
from boba.stand.migrate import StandMigrations

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

SCHEMA = "automation_elements_test"


@pytest.fixture
async def tables(
    pool: AsyncPostgresPool, test_postgres: PostgresConfig
) -> FeedbacksTable:
    async with pool.connection() as conn:
        await conn.execute(
            sql.SQL("drop schema if exists {} cascade").format(sql.Identifier(SCHEMA))
        )
    await StandMigrations().application(test_postgres, SCHEMA, "boba.aid")

    return FeedbacksTable(pool, SCHEMA)


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
