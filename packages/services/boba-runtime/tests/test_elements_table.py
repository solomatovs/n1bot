"""Таблицы elements и feedbacks сервиса: DDL, upsert, выборка треда, очистка шага."""

from __future__ import annotations

from uuid import uuid4

import pytest
from psycopg import sql

from boba.chat.threads import StoredElement, StoredFeedback
from boba.db.postgres import AsyncPostgresPool
from boba.runtime.config import RuntimeConfig
from boba.runtime.elements import ChatTables

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

SCHEMA = "automation_elements_test"


@pytest.fixture
async def tables(
    runtime_config: RuntimeConfig, test_database: str, pool: AsyncPostgresPool
) -> ChatTables:
    async with pool.connection() as conn:
        await conn.execute(
            sql.SQL("drop schema if exists {} cascade").format(sql.Identifier(SCHEMA))
        )
    postgres = runtime_config.data_layer.postgres
    cfg = postgres.model_copy(update={"dbname": test_database})
    built = ChatTables.of(cfg, SCHEMA, pool)
    await built.setup()
    await built.setup()

    return built


async def test_elements_round_trip(tables: ChatTables) -> None:
    thread_id = uuid4()
    step_id = uuid4()
    element = StoredElement(
        id=uuid4(),
        name="report.pdf",
        type="file",
        display="inline",
        thread_id=thread_id,
        for_id=step_id,
        props={"dir": "docs"},
        mime="application/pdf",
    )

    await tables.elements.upsert(element)
    await tables.elements.upsert(element.model_copy(update={"language": "ru"}))

    found = await tables.elements.get(thread_id, element.id)
    assert found is not None
    assert found.language == "ru"
    assert found.chainlit_key == ""
    assert dict(found.props) == {"dir": "docs"}
    assert (await tables.elements.find(element.id)) == found
    listed = await tables.elements.list_of_thread(thread_id)
    assert [e.id for e in listed] == [element.id]

    await tables.elements.delete_of_step(step_id)
    assert await tables.elements.find(element.id) is None
    assert await tables.elements.delete(element.id) is None


async def test_feedbacks_round_trip(tables: ChatTables) -> None:
    thread_id = uuid4()
    step_id = uuid4()
    feedback = StoredFeedback(id=uuid4(), for_id=step_id, value=1, thread_id=thread_id)

    await tables.feedbacks.upsert(feedback)
    await tables.feedbacks.upsert(feedback.model_copy(update={"comment": "good"}))

    listed = await tables.feedbacks.list_of_thread(thread_id)
    assert [f.comment for f in listed] == ["good"]

    deleted = await tables.feedbacks.delete(feedback.id)
    assert deleted is not None
    assert deleted.for_id == step_id
    assert await tables.feedbacks.delete(feedback.id) is None

    await tables.feedbacks.upsert(feedback)
    await tables.feedbacks.delete_of_thread(thread_id)
    assert await tables.feedbacks.list_of_thread(thread_id) == []
