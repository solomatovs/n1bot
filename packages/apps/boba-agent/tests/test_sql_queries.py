"""Сборка срезов: закрытая группа tools, LAST результата и разовые настройки."""

import psycopg
import pytest
from psycopg.rows import dict_row

from boba.agent.openai_validator import OpenAiRequestDraft, OpenAiRequestValidator
from boba.agent.queries import Query

pytestmark = [pytest.mark.anyio, pytest.mark.integration]


async def test_pending_calls_and_later_user_messages_form_valid_requests(
    sql_connection, demo_records
):
    validator = OpenAiRequestValidator()
    async with psycopg.AsyncRawCursor(sql_connection, row_factory=dict_row) as cursor:
        drafts = []
        for through_seq in (16, 19, 29, 30):
            await cursor.execute(
                Query.OPENAI.text(), ("openai-tasks", None, through_seq)
            )
            draft = OpenAiRequestDraft.model_validate(await cursor.fetchone())
            validator.require_valid(draft)
            drafts.append(draft)
        first, users, polled, finished = drafts
        assert len(users.request["messages"]) == len(first.request["messages"]) + 3
        assert polled.request != finished.request
        # Updating the original task must not rewrite the snapshot returned by task_status.
        assert polled.request["messages"][-1] == finished.request["messages"][-1]
        await cursor.execute(Query.OPENAI.text(), ("openai-tasks", None, 19))
        assert OpenAiRequestDraft.model_validate(await cursor.fetchone()) == users
