"""Кодек записей: каждая запись плана 5.9 пишется строкой и читается обратно
без потерь; имена полей на проводе — docs/prompt_assembly/specs/01_journal_write.md."""

from __future__ import annotations

import json
from typing import TypedDict

import pytest

from boba.agent.records import (
    ApiErrorRecord,
    ApiRequestRecord,
    AssistantMessage,
    AssistantRecord,
    AttachmentPayload,
    AttachmentRecord,
    AttachmentRole,
    CompactBoundaryRecord,
    CompactMetadata,
    CompactTrigger,
    MessageOrigin,
    OriginKind,
    PermissionBehavior,
    PermissionMode,
    PermissionModeRecord,
    PermissionRule,
    PermissionRuleOperation,
    PermissionRuleRecord,
    PreservedMessages,
    QueueOperation,
    QueueOperationRecord,
    QueueRemoveReason,
    Record,
    RecordCodec,
    RecordError,
    RenderedMessage,
    ReplyStopReason,
    ThinkingBlock,
    ToolDenialKind,
    ToolResultBlock,
    ToolUseBlock,
    Usage,
    UserMessage,
    UserRecord,
)

SESSION = "11111111-2222-3333-4444-555555555555"
STAMP = "2026-10-10T10:00:00.000Z"
VERSION = "0.0.26.dev4"


class Chained(TypedDict):
    """Поля конверта записи цепочки для образцов."""

    uuid: str
    parent_uuid: str | None
    timestamp: str
    session_id: str
    version: str
    seq: int


def chained() -> Chained:
    return Chained(
        uuid="u-1",
        parent_uuid=None,
        timestamp=STAMP,
        session_id=SESSION,
        version=VERSION,
        seq=1,
    )


SAMPLES: dict[str, Record] = {
    "user-text": UserRecord(
        **chained(),
        message=UserMessage(content="hello"),
        origin=MessageOrigin(kind=OriginKind.HUMAN),
    ),
    "user-tool-result": UserRecord(
        **chained(),
        message=UserMessage(
            content=[ToolResultBlock(tool_use_id="abcDEF123", content="5")]
        ),
        tool_use_result="5",
        source_tool_assistant_uuid="a-1",
    ),
    "user-stub": UserRecord(
        **chained(),
        message=UserMessage(
            content=[
                ToolResultBlock(tool_use_id="abcDEF123", content="x", is_error=True)
            ]
        ),
        tool_use_result="x",
        tool_denial_kind=ToolDenialKind.INTERRUPTED,
        is_meta=True,
    ),
    "assistant-thinking": AssistantRecord(
        **chained(),
        message=AssistantMessage(
            id="msg_a",
            model="fake",
            content=[ThinkingBlock(thinking="hm")],
            stop_reason=ReplyStopReason.END_TURN,
            usage=Usage(input_tokens=3, output_tokens=4),
        ),
        request_id="req_a",
    ),
    "assistant-tool-use": AssistantRecord(
        **chained(),
        message=AssistantMessage(
            id="msg_a",
            model="fake",
            content=[ToolUseBlock(id="abcDEF123", name="add", input={"a": 1})],
            stop_reason=ReplyStopReason.TOOL_USE,
        ),
        api_block_index=1,
        request_id="req_a",
        wire_tool_call_id="call_0",
    ),
    "attachment": AttachmentRecord(
        **chained(),
        attachment=AttachmentPayload.model_validate(
            {"type": "date", "date": "2026-10-10", "changed": False}
        ),
        rendered=[RenderedMessage(content="Today's date is 2026-10-10.")],
        rendered_role=AttachmentRole.SYSTEM,
    ),
    "snapshot": AttachmentRecord(
        **chained(),
        attachment=AttachmentPayload.model_validate(
            {"type": "prompt_snapshot", "systemPrompt": ["be brief"], "tools": []}
        ),
    ),
    "api-error": ApiErrorRecord(
        **chained(), error="boom", retry_attempt=1, max_retries=3
    ),
    "boundary": CompactBoundaryRecord(
        **chained(),
        logical_parent_uuid="u-0",
        compact_metadata=CompactMetadata(
            trigger=CompactTrigger.AUTO,
            pre_tokens=1000,
            post_tokens=100,
            duration_ms=5,
            preserved_messages=PreservedMessages(
                anchor_uuid="s-1", uuids=["a-9"], all_uuids=["s-1", "a-9"]
            ),
        ),
    ),
    "queue": QueueOperationRecord(
        timestamp=STAMP,
        session_id=SESSION,
        seq=1,
        operation=QueueOperation.REMOVE,
        reason=QueueRemoveReason.ABSORBED_MID_TURN,
        command_uuid="c-1",
    ),
    "mode": PermissionModeRecord(
        timestamp=STAMP, session_id=SESSION, seq=1, permission_mode=PermissionMode.AUTO
    ),
    "rule": PermissionRuleRecord(
        timestamp=STAMP,
        session_id=SESSION,
        seq=1,
        operation=PermissionRuleOperation.ADD,
        rule=PermissionRule(tool="add", behavior=PermissionBehavior.ALLOW),
    ),
    "request": ApiRequestRecord(
        timestamp=STAMP,
        session_id=SESSION,
        seq=1,
        id="r-1",
        request_id="req_a",
        params={"messages": []},
        digest="sha256:0",
    ),
}


class TestRecordCodec:
    @pytest.mark.parametrize("name", list(SAMPLES))
    def test_round_trip(self, name: str) -> None:
        codec = RecordCodec()
        record = SAMPLES[name]

        line = codec.line(record)
        parsed = codec.parse(json.loads(line))

        assert parsed == record
        assert type(parsed) is type(record)

    def test_wire_names_follow_claude_code(self) -> None:
        line = RecordCodec().line(SAMPLES["user-tool-result"])
        document = json.loads(line)

        assert document["parentUuid"] is None
        assert document["sessionId"] == SESSION
        assert document["isSidechain"] is False
        assert document["sourceToolAssistantUUID"] == "a-1"
        assert document["toolUseResult"] == "5"
        assert "isMeta" not in document
        assert "is_error" not in document["message"]["content"][0]

    def test_assistant_wire_keeps_anthropic_snake_case(self) -> None:
        document = json.loads(RecordCodec().line(SAMPLES["assistant-tool-use"]))
        message = document["message"]

        assert message["stop_reason"] == "tool_use"
        assert message["stop_sequence"] is None
        assert message["usage"]["cache_read_input_tokens"] == 0
        assert document["apiBlockIndex"] == 1
        assert document["wireToolCallId"] == "call_0"

    def test_unknown_field_is_ignored(self) -> None:
        document = json.loads(RecordCodec().line(SAMPLES["user-text"]))
        document["cwd"] = "/work"
        document["promptId"] = "p"

        parsed = RecordCodec().parse(document)

        assert parsed == SAMPLES["user-text"]

    def test_unknown_type_is_an_error(self) -> None:
        with pytest.raises(RecordError, match="last-prompt"):
            RecordCodec().parse({"type": "last-prompt", "leafUuid": "x"})

    def test_unknown_system_subtype_is_an_error(self) -> None:
        with pytest.raises(RecordError, match="away_summary"):
            RecordCodec().parse({"type": "system", "subtype": "away_summary"})

    def test_missing_required_field_is_an_error(self) -> None:
        document = json.loads(RecordCodec().line(SAMPLES["user-text"]))
        del document["message"]

        with pytest.raises(RecordError, match="UserRecord"):
            RecordCodec().parse(document)
