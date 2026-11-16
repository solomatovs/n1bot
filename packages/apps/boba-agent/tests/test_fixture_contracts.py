"""Прямые проверки целевой истории на SQL-адаптере пакета агента."""

import psycopg
import pytest
from psycopg.rows import dict_row

from boba.agent.openai_validator import OpenAiRequestDraft, OpenAiRequestValidator
from boba.agent.queries import Query

pytestmark = [pytest.mark.anyio, pytest.mark.integration]


async def test_history_records_and_openai_projection(sql_connection, demo_records):
    sessions = sorted({row["session_id"] for row in demo_records})
    validator = OpenAiRequestValidator()
    async with psycopg.AsyncRawCursor(sql_connection, row_factory=dict_row) as cursor:
        for sid in sessions:
            await cursor.execute(Query.OPENAI.text(), (sid, None, None))
            draft = OpenAiRequestDraft.model_validate(await cursor.fetchone())
            errors = validator.validate(draft)
            if sid.startswith("openai-error-"):
                assert errors, sid
                continue
            assert not errors, (sid, errors)
