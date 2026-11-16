import psycopg
import pytest
from psycopg.rows import dict_row

from boba.agent.acp_validator import (
    AcpAssemblyError,
    AcpElicitationDraft,
    AcpHistoryDraft,
    AcpHistoryValidator,
    AcpNoticeDraft,
    AcpPermissionDraft,
)
from boba.agent.queries import Query

pytestmark = [pytest.mark.anyio, pytest.mark.integration]


async def test_saved_history_projects_to_acp(sql_connection, demo_records):
    rows = demo_records
    query = Query.ACP_V1_HISTORY
    validate = AcpHistoryValidator().require_v1
    async with psycopg.AsyncRawCursor(sql_connection, row_factory=dict_row) as cursor:
        for sid in (
            "sql-acp-demo",
            "openai-tasks",
            "openai-compact",
            "openai-task-compact",
            "openai-media",
        ):
            await cursor.execute(query.text(), (sid, None, None, True, []))
            draft = AcpHistoryDraft.model_validate(await cursor.fetchone())
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
            ("sql-acp-demo", None, None, True, []),
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

        await cursor.execute(Query.ACP_V1_NOTICE.text(), ("sql-acp-demo", "attempt"))
        success = AcpNoticeDraft.model_validate(await cursor.fetchone())
        assert success.require_v1() == ()

        await cursor.execute(Query.ACP_V1_NOTICE.text(), ("sql-acp-demo", "retry"))
        warning = AcpNoticeDraft.model_validate(await cursor.fetchone())
        assert len(warning.require_v1()) == 1


async def test_action_queries_distinguish_pending_and_closed_questions(
    sql_connection, demo_records
):
    validator = AcpHistoryValidator()
    async with psycopg.AsyncRawCursor(sql_connection, row_factory=dict_row) as cursor:
        await cursor.execute(
            Query.ACP_V1_PERMISSION.text(), ("sql-acp-actions", "permission")
        )
        validator.require_v1_permission(
            AcpPermissionDraft.model_validate(await cursor.fetchone())
        )
        await cursor.execute(
            Query.ACP_V1_PERMISSION.text(), ("sql-acp-demo", "permission")
        )
        with pytest.raises(AcpAssemblyError, match="permission_already_resolved"):
            validator.require_v1_permission(
                AcpPermissionDraft.model_validate(await cursor.fetchone())
            )

        for record_id in ("form", "url"):
            await cursor.execute(
                Query.ACP_V1_ELICITATION.text(), ("sql-acp-actions", record_id)
            )
            validator.require_v1_elicitation(
                AcpElicitationDraft.model_validate(await cursor.fetchone())
            )
        await cursor.execute(
            Query.ACP_V1_ELICITATION.text(), ("sql-acp-demo", "question")
        )
        with pytest.raises(AcpAssemblyError, match="elicitation_already"):
            validator.require_v1_elicitation(
                AcpElicitationDraft.model_validate(await cursor.fetchone())
            )


@pytest.mark.parametrize(
    "corruption", ["missing_call", "duplicate_call", "bad_copy", "bad_source"]
)
async def test_history_validator_rejects_corrupt_references(
    sql_connection, demo_records, corruption
):
    async with psycopg.AsyncRawCursor(sql_connection, row_factory=dict_row) as cursor:
        match corruption:
            case "missing_call":
                await cursor.execute(
                    "delete from boba.records where session_id = 'sql-acp-demo' and category = 'tool_call'"
                )
            case "duplicate_call":
                await cursor.execute(
                    "update boba.records set category = 'tool_call', key = 'call' where session_id = 'sql-acp-demo' and record_id = 'warning'"
                )
            case "bad_copy":
                await cursor.execute(
                    "update boba.records set copied_from_id = 'missing' where session_id = 'sql-acp-demo' and record_id = 'warning'"
                )
            case "bad_source":
                await cursor.execute(
                    "update boba.records set payload = payload || '{\"source_record_id\": \"missing\"}'::jsonb where session_id = 'sql-acp-demo' and record_id = 'result'"
                )
        await cursor.execute(
            Query.ACP_V1_HISTORY.text(), ("sql-acp-demo", None, None, True, [])
        )
        draft = AcpHistoryDraft.model_validate(await cursor.fetchone())
        with pytest.raises(AcpAssemblyError):
            AcpHistoryValidator().require_v1(draft)


async def test_completed_url_action_is_not_requested_again(
    sql_connection, demo_records
):
    async with psycopg.AsyncRawCursor(sql_connection, row_factory=dict_row) as cursor:
        await cursor.execute(
            """
            insert into boba.records (
                session_id, seq, record_id, operation_id, operation_position,
                kind, category, operation, created_at, version, payload
            )
            values (
                'sql-acp-actions', 9, 'url-completed', 'url-completed-op', 0,
                'elicitation_completed', 'operational', 'set', now(), 'test',
                '{"elicitation_id": "url", "outcome": "completed"}'::jsonb
            )
            """
        )
        await cursor.execute(Query.ACP_V1_ELICITATION.text(), ("sql-acp-actions", "url"))
        draft = AcpElicitationDraft.model_validate(await cursor.fetchone())
        assert draft.response_count == 0
        assert draft.completion_count == 1
        with pytest.raises(AcpAssemblyError, match="elicitation_already"):
            AcpHistoryValidator().require_v1_elicitation(draft)
