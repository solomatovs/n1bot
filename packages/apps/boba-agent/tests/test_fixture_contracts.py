import re
from pathlib import Path

import psycopg
import pytest
from psycopg.rows import dict_row

from boba.agent.acp_validator import (
    AcpAssemblyError,
    AcpHistoryDraft,
    AcpHistoryValidator,
)
from boba.agent.models import HISTORY_RECORD_ADAPTER
from boba.agent.openai_validator import OpenAiRequestDraft, OpenAiRequestValidator
from boba.agent.queries import Query


@pytest.mark.anyio
@pytest.mark.integration
async def test_demo_records_and_all_sql_projections(sql_connection):
    root = Path(__file__).resolve().parents[4]
    source = (root / "experiments/history-sql/fixtures.sql").read_text()
    source = re.sub(r"(?m)^(begin|commit);\s*$", "", source)
    await sql_connection.execute(source, prepare=False)
    async with psycopg.AsyncRawCursor(sql_connection, row_factory=dict_row) as cursor:
        await cursor.execute(
            "select to_jsonb(r) as record from boba.records r "
            "where session_id like 'sql-%' order by session_id, seq"
        )
        rows = [row["record"] for row in await cursor.fetchall()]
        for row in rows:
            HISTORY_RECORD_ADAPTER.validate_python(row)
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
        for sid in {row["session_id"] for row in rows}:
            await cursor.execute(Query.ACP_HISTORY.text(), (sid, None, None))
            draft = AcpHistoryDraft.model_validate(await cursor.fetchone())
            if sid in {
                "sql-acp-copy-demo",
                "sql-acp-orphan-demo",
                "sql-acp-duplicate-demo",
            }:
                with pytest.raises(AcpAssemblyError):
                    AcpHistoryValidator().require_valid(draft)
            else:
                AcpHistoryValidator().require_valid(draft)
