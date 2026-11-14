import psycopg
import pytest
from psycopg.rows import dict_row

from boba.agent.openai_validator import OpenAiRequestDraft, OpenAiRequestValidator
from boba.agent.queries import Query

pytestmark = [pytest.mark.anyio, pytest.mark.integration]


async def test_history_records_and_openai_projection(sql_connection, demo_records):
    rows = demo_records
    async with psycopg.AsyncRawCursor(sql_connection, row_factory=dict_row) as cursor:
        good, invalid = 0, []
        for row in rows:
            if row["category"] != "request":
                continue
            await cursor.execute(
                Query.OPENAI.text(), (row["session_id"], row["record_id"], None)
            )
            draft = OpenAiRequestDraft.model_validate(await cursor.fetchone())
            errors = OpenAiRequestValidator().validate(draft)
            if errors:
                invalid.append(
                    (row["session_id"], row["record_id"], [e.code for e in errors])
                )
            else:
                good += 1
        assert good == 39
        assert len(invalid) == 3, invalid
