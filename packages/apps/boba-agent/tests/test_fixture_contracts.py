import re
from pathlib import Path

import psycopg
import pytest
from psycopg.rows import dict_row

from boba.agent.acp_validator import (
    AcpAssemblyError,
    AcpHistoryDraft,
    AcpHistoryValidator,
    AcpNoticeDraft,
    AcpStateDraft,
)
from boba.agent.models import HISTORY_RECORD_ADAPTER
from boba.agent.openai_validator import OpenAiRequestDraft, OpenAiRequestValidator
from boba.agent.queries import Query


@pytest.mark.anyio
@pytest.mark.integration
@pytest.mark.parametrize("protocol_version", [1, 2])
async def test_demo_records_and_all_sql_projections(sql_connection, protocol_version):
    query = Query.ACP_HISTORY
    validate = AcpHistoryValidator().require_valid
    extra = ()
    if protocol_version == 1:
        query = Query.ACP_V1_HISTORY
        validate = AcpHistoryValidator().require_v1
        extra = ([],)
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
            await cursor.execute(query.text(), (sid, None, None, True, *extra))
            draft = AcpHistoryDraft.model_validate(await cursor.fetchone())
            if sid in {
                "sql-acp-copy-demo",
                "sql-acp-orphan-demo",
                "sql-acp-duplicate-demo",
            }:
                with pytest.raises(AcpAssemblyError):
                    validate(draft)
            else:
                validate(draft)

            permission_ids = set()
            for row in rows:
                if row["session_id"] != sid:
                    continue

                if row["kind"] in {
                    "permission_request",
                    "permission_response",
                    "permission_decision",
                }:
                    permission_ids.add(row["record_id"])

            for notification in draft.notifications:
                assert notification.record_id not in permission_ids

        await cursor.execute(
            query.text(),
            ("sql-demo", None, None, True, *extra),
        )
        history = AcpHistoryDraft.model_validate(await cursor.fetchone())
        for notification in history.notifications:
            assert notification.record_id not in {"attempt", "turn-end"}

        await cursor.execute(
            "select min(seq) - 1 as after_seq, max(seq) as through_seq "
            "from boba.records where session_id = 'sql-acp-demo' "
            "and record_id in ('warning', 'retry')"
        )
        bounds = await cursor.fetchone()
        assert bounds is not None
        params = ("sql-acp-demo", bounds["after_seq"], bounds["through_seq"])
        await cursor.execute(query.text(), (*params, False, *extra))
        live = AcpHistoryDraft.model_validate(await cursor.fetchone())
        assert live.notifications == ()

        await cursor.execute(query.text(), (*params, True, *extra))
        replay = AcpHistoryDraft.model_validate(await cursor.fetchone())
        assert len(replay.notifications) == 2

        await cursor.execute(Query.ACP_NOTICE.text(), ("sql-demo", "attempt"))
        success = AcpNoticeDraft.model_validate(await cursor.fetchone())
        assert success.require_valid() == ()

        await cursor.execute(Query.ACP_NOTICE.text(), ("sql-acp-demo", "retry"))
        warning = AcpNoticeDraft.model_validate(await cursor.fetchone())
        assert len(warning.require_valid()) == 1

        await cursor.execute(Query.ACP_STATE.text(), ("sql-acp-demo", None))
        state = AcpStateDraft.model_validate(await cursor.fetchone())
        assert AcpHistoryValidator().require_state(state).usage["input_tokens"] == 12
