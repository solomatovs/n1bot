import psycopg
import pytest
from psycopg.rows import dict_row

from boba.agent.acp_validator import (
    AcpAssemblyError,
    AcpHistoryDraft,
    AcpHistoryValidator,
    AcpNoticeDraft,
)
from boba.agent.queries import Query

pytestmark = [pytest.mark.anyio, pytest.mark.integration]


async def test_saved_history_projects_to_acp(sql_connection, demo_records):
    rows = demo_records
    query = Query.ACP_V1_HISTORY
    validate = AcpHistoryValidator().require_v1
    async with psycopg.AsyncRawCursor(sql_connection, row_factory=dict_row) as cursor:
        for sid in {row["session_id"] for row in rows}:
            await cursor.execute(query.text(), (sid, None, None, True, []))
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
            ("sql-demo", None, None, True, []),
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
        await cursor.execute(query.text(), (*params, False, []))
        live = AcpHistoryDraft.model_validate(await cursor.fetchone())
        assert live.notifications == ()

        await cursor.execute(query.text(), (*params, True, []))
        replay = AcpHistoryDraft.model_validate(await cursor.fetchone())
        assert len(replay.notifications) == 2

        await cursor.execute(Query.ACP_V1_NOTICE.text(), ("sql-demo", "attempt"))
        success = AcpNoticeDraft.model_validate(await cursor.fetchone())
        assert success.require_v1() == ()

        await cursor.execute(Query.ACP_V1_NOTICE.text(), ("sql-acp-demo", "retry"))
        warning = AcpNoticeDraft.model_validate(await cursor.fetchone())
        assert len(warning.require_v1()) == 1
