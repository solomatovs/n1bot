"""Запросы из поставки агента: накопленный ввод, принятие пачки и повтор сборки."""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

import psycopg
import pytest
from psycopg.rows import dict_row
from pydantic import TypeAdapter

from boba.agent.openai_validator import OpenAiRequestDraft, OpenAiRequestValidator
from boba.agent.queries import Query


def record(  # noqa: PLR0913 — поля строки истории
    record_id, kind, category, payload, *, role=None, request_id=None
):
    return {
        "session_id": None,
        "seq": None,
        "record_id": record_id,
        "kind": kind,
        "category": category,
        "role": role,
        "key": "model" if kind == "model" else None,
        "operation": "set",
        "request_id": request_id,
        "copied_from_id": None,
        "created_at": "2026-11-14T09:00:00Z",
        "version": "0.0.33.dev4",
        "payload": payload,
    }


def text(value):
    return {"content": [{"kind": "text", "text": value}]}


@pytest.mark.anyio
@pytest.mark.integration
async def test_all_inputs_waiting_for_tools_enter_one_request(sql_connection):
    session_id = f"sql-batch-test-{uuid4().hex}"
    validator = OpenAiRequestValidator()
    messages = [
        "Проверь документацию.",
        "И конфигурацию.\r\n  世界",
        "Перечисли ошибки.",
    ]
    rows = [
        record(
            "model",
            "model",
            "setting",
            {"provider": "openai", "value": {"model": "test"}},
        ),
        record("first", "user_message", "input", text("Проверь проект."), role="user"),
        record(
            "first-accepted",
            "message_accepted",
            "admission",
            {"source_record_id": "first"},
        ),
        record("r0", "llm_request_boundary", "request", {}),
        record(
            "calls",
            "assistant_message",
            "message",
            {
                "content": [
                    {
                        "kind": "tool_call",
                        "call_id": name,
                        "name": name,
                        "arguments": {},
                    }
                    for name in ("check", "tests")
                ],
                "completion": "complete",
            },
            role="assistant",
            request_id="r0",
        ),
        record("next-1", "user_message", "input", text(messages[0]), role="user"),
        record("next-2", "user_message", "input", text(messages[1]), role="user"),
        record(
            "result-check",
            "tool_result",
            "message",
            {**text("Проверено."), "call_id": "check", "is_error": False},
            role="tool",
        ),
        record("next-3", "user_message", "input", text(messages[2]), role="user"),
        record(
            "result-tests",
            "tool_result",
            "message",
            {**text("Пройдено."), "call_id": "tests", "is_error": False},
            role="tool",
        ),
    ]
    async with psycopg.AsyncRawCursor(sql_connection, row_factory=dict_row) as cursor:

        async def query(name, *params):
            await cursor.execute(name.text(), params)
            row = await cursor.fetchone()
            assert row is not None
            return row

        await query(Query.LOCK, session_id)
        receipt = await query(Query.APPEND, session_id, json.dumps(rows), 0)
        assert receipt["error"] is None
        await cursor.execute(Query.PENDING_INPUTS.text(), (session_id, None))
        pending = await cursor.fetchall()
        assert [row["record_id"] for row in pending] == ["next-1", "next-2", "next-3"]

        draft = OpenAiRequestDraft.model_validate(
            await query(Query.OPENAI, session_id, None, 10)
        )
        before = validator.require_valid(draft)
        before_messages = TypeAdapter(list[dict[str, Any]]).validate_python(
            before["messages"]
        )
        assert [message["role"] for message in before_messages] == [
            "user",
            "assistant",
            "tool",
            "tool",
        ]
        draft = OpenAiRequestDraft.model_validate(
            await query(Query.OPENAI, session_id, None, 9)
        )
        assert {issue.code for issue in validator.validate(draft)} == {
            "missing_tool_result"
        }

        accepted = [
            record(
                "accept-" + row["record_id"],
                "message_accepted",
                "admission",
                {"source_record_id": row["record_id"]},
            )
            for row in pending
        ]
        accepted.append(record("combined", "llm_request_boundary", "request", {}))
        receipt = await query(Query.APPEND, session_id, json.dumps(accepted), 10)
        assert receipt["error"] is None
        assert [row["seq"] for row in receipt["records"]] == [11, 12, 13, 14]
        repeat = await query(Query.APPEND, session_id, json.dumps(accepted), 10)
        assert repeat["retry"]
        assert repeat["records"] == receipt["records"]
        assert (await query(Query.HISTORY_INTEGRITY, session_id, 1, 14))["errors"] == []

        after = validator.require_valid(
            OpenAiRequestDraft.model_validate(
                await query(Query.OPENAI, session_id, "combined", None)
            )
        )
        after_messages = TypeAdapter(list[dict[str, Any]]).validate_python(
            after["messages"]
        )
        assert after_messages[:4] == before_messages
        assert [message["role"] for message in after_messages[4:]] == ["user"] * 3
        assert [
            message["content"][0]["text"] for message in after_messages[4:]
        ] == messages
        by_seq = validator.require_valid(
            OpenAiRequestDraft.model_validate(
                await query(Query.OPENAI, session_id, None, 13)
            )
        )
        assert by_seq == after
        await cursor.execute(Query.PENDING_INPUTS.text(), (session_id, None))
        assert await cursor.fetchall() == []
        await cursor.execute(Query.READ.text(), (session_id, 10, 13))
        assert [row["record_id"] for row in await cursor.fetchall()] == [
            "accept-next-1",
            "accept-next-2",
            "accept-next-3",
        ]
