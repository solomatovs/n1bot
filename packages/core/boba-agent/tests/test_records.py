"""Кодек записей: каждая запись плана 5.9 пишется строкой и читается обратно
без потерь; имена полей на проводе — docs/prompt_assembly/specs/01_journal_write.md."""

from __future__ import annotations

import json
from typing import TypedDict

import mcp_types as mt
import pytest

from boba.agent.records import (
    ApiErrorRecord,
    ApiRequestRecord,
    AssistantMessage,
    AttachmentRecord,
    AttachmentRole,
    CallStatus,
    CommandOutputRecord,
    CommandRecord,
    CompactBoundaryRecord,
    CompactionPhase,
    CompactionRecord,
    CompactMetadata,
    CompactSummaryRecord,
    CompactTrigger,
    DateAttachment,
    HarnessCommandRecord,
    InformationalRecord,
    InterruptRecord,
    MetaRecord,
    NoResponseRecord,
    NoticeLevel,
    PermissionBehavior,
    PermissionMode,
    PermissionModeRecord,
    PermissionRule,
    PermissionRuleOperation,
    PermissionRuleRecord,
    PreservedMessages,
    PromptRecord,
    PromptSnapshot,
    QueueOperation,
    QueueOperationRecord,
    QueueRemoveReason,
    Record,
    RecordCodec,
    RecordError,
    RecordKind,
    RenderedMessage,
    ReplyRecord,
    ReplyStopReason,
    SummaryRequestRecord,
    Synthetic,
    TaskNotificationRecord,
    TextBlock,
    ThinkingBlock,
    ToolDenialKind,
    ToolResultBlock,
    ToolResultRecord,
    ToolUseBlock,
    TurnEndRecord,
    TurnOutcome,
    Usage,
    UserMessage,
)
from boba.llm.chat import LlmTrouble

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
    "prompt": PromptRecord(**chained(), message=UserMessage(content="hello")),
    "prompt-blocks": PromptRecord(
        **chained(), message=UserMessage(content=[TextBlock(text="hello")])
    ),
    "prompt-link": PromptRecord(
        **chained(),
        message=UserMessage(
            content=[
                TextBlock(text="look at this"),
                mt.ResourceLink(
                    name="note.txt",
                    uri="http://chat/canvas/t-1/upload/note.txt",
                    mime_type="text/plain",
                ),
            ]
        ),
    ),
    "task-notification": TaskNotificationRecord(
        **chained(), message=UserMessage(content="<task-notification/>")
    ),
    "meta": MetaRecord(**chained(), message=UserMessage(content="Output limit hit.")),
    "command": CommandRecord(
        **chained(),
        message=UserMessage(content="<command-name>/compact</command-name>"),
    ),
    "command-output": CommandOutputRecord(
        **chained(),
        message=UserMessage(
            content="<local-command-stdout>Compacted</local-command-stdout>"
        ),
    ),
    "tool-result": ToolResultRecord(
        **chained(),
        message=UserMessage(
            content=[ToolResultBlock(tool_use_id="abcDEF123", content="5")]
        ),
        tool_use_result="5",
        source_tool_assistant_uuid="a-1",
        status=CallStatus.COMPLETED,
    ),
    "tool-result-stub": ToolResultRecord(
        **chained(),
        message=UserMessage(
            content=[
                ToolResultBlock(tool_use_id="abcDEF123", content="x", is_error=True)
            ]
        ),
        source_tool_assistant_uuid="a-1",
        tool_denial_kind=ToolDenialKind.INTERRUPTED,
        status=CallStatus.FAILED,
    ),
    "interrupt": InterruptRecord(
        **chained(),
        message=UserMessage(content="[Request interrupted by user]"),
        interrupted_by_shutdown=True,
    ),
    "summary": CompactSummaryRecord(
        **chained(), message=UserMessage(content="This session is being continued")
    ),
    "reply-thinking": ReplyRecord(
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
    "reply-tool-use": ReplyRecord(
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
    "no-response": NoResponseRecord(
        **chained(),
        message=AssistantMessage(
            id="msg_b",
            model=Synthetic.MODEL,
            content=[TextBlock(text="No response requested.")],
            stop_reason=ReplyStopReason.STOP_SEQUENCE,
        ),
        request_id="req_b",
    ),
    "attachment": AttachmentRecord(
        **chained(),
        attachment=DateAttachment(date="2026-10-10", changed=False),
        rendered=[RenderedMessage(content="Today's date is 2026-10-10.")],
        rendered_role=AttachmentRole.SYSTEM,
    ),
    "snapshot": AttachmentRecord(
        **chained(),
        attachment=PromptSnapshot(
            system_prompt=["be brief"],
            tools=[],
            skills=[],
            model="fake",
            system_turns=False,
        ),
    ),
    "api-error": ApiErrorRecord(
        **chained(),
        error="boom",
        retry_in_ms=2000,
        retry_attempt=1,
        max_retries=3,
        trouble=LlmTrouble.BUSY,
        detail="HTTP 429 Too Many Requests",
    ),
    "informational": InformationalRecord(
        **chained(),
        content="The MCP server 'stand' isn't available right now.",
        level=NoticeLevel.WARNING,
        detail="HTTP 401 Unauthorized",
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
    "compaction": CompactionRecord(
        **chained(),
        compaction_id="s-1",
        phase=CompactionPhase.FAILED,
        trigger=CompactTrigger.MANUAL,
        command_uuid="c-1",
        error="the summary reply is empty",
    ),
    "harness-command": HarnessCommandRecord(
        **chained(),
        name="connections",
        text="/connections",
        outcome={"kind": "text", "text": "**Your connections**"},
    ),
    "summary-request": SummaryRequestRecord(
        **chained(),
        compaction_id="c-1",
        first_uuid="u-1",
        last_uuid="u-2",
        preface=["[earlier conversation truncated for compaction retry]"],
        instruction="Summarize the conversation.",
    ),
    "turn-end": TurnEndRecord(
        **chained(),
        outcome=TurnOutcome.API_ERROR,
        notice="The model is down.",
        level=NoticeLevel.ERROR,
        detail="HTTP 500",
        error="openai chat: POST http://model/v1: expected 2xx, got 500",
        usage=Usage(input_tokens=10, output_tokens=0),
        num_turns=1,
        duration_ms=120,
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
        line = RecordCodec().line(SAMPLES["tool-result"])
        document = json.loads(line)

        assert document["parentUuid"] is None
        assert document["sessionId"] == SESSION
        assert document["isSidechain"] is False
        assert document["sourceToolAssistantUUID"] == "a-1"
        assert document["toolUseResult"] == "5"
        assert "isMeta" not in document
        assert "is_error" not in document["message"]["content"][0]

    def test_link_is_written_in_the_standard_resource_link_form(self) -> None:
        """Вложение вопроса — блок resource_link формой MCP/ACP; текст вопроса
        для заголовка и ленты — без ссылки."""
        record = SAMPLES["prompt-link"]
        document = json.loads(RecordCodec().line(record))

        assert document["message"]["content"][1] == {
            "type": "resource_link",
            "name": "note.txt",
            "uri": "http://chat/canvas/t-1/upload/note.txt",
            "mimeType": "text/plain",
        }
        assert isinstance(record, PromptRecord)
        assert record.human_text() == "look at this"
        assert [link.name for link in record.message.links()] == ["note.txt"]

    def test_assistant_wire_keeps_anthropic_snake_case(self) -> None:
        document = json.loads(RecordCodec().line(SAMPLES["reply-tool-use"]))
        message = document["message"]

        assert message["stop_reason"] == "tool_use"
        assert message["stop_sequence"] is None
        assert message["usage"]["cache_read_input_tokens"] == 0
        assert document["apiBlockIndex"] == 1
        assert document["wireToolCallId"] == "call_0"

    def test_unknown_field_is_ignored(self) -> None:
        document = json.loads(RecordCodec().line(SAMPLES["prompt"]))
        document["cwd"] = "/work"
        document["promptId"] = "p"

        parsed = RecordCodec().parse(document)

        assert parsed == SAMPLES["prompt"]

    def test_every_kind_has_a_sample(self) -> None:
        kinds = {record.kind for record in SAMPLES.values()}

        assert kinds == set(RecordKind)

    def test_kind_and_the_reference_flags_are_written(self) -> None:
        """Вид записи — поле kind; флаги эталона, которые различают виды,
        класс вида пишет сам."""
        summary = json.loads(RecordCodec().line(SAMPLES["summary"]))
        meta = json.loads(RecordCodec().line(SAMPLES["task-notification"]))

        assert summary["kind"] == "compact_summary"
        assert summary["isCompactSummary"] is True
        assert summary["isVisibleInTranscriptOnly"] is True
        assert meta["isMeta"] is True
        assert meta["origin"] == {"kind": "task-notification"}

    def test_unknown_kind_is_an_error(self) -> None:
        with pytest.raises(RecordError, match="last-prompt"):
            RecordCodec().parse({"type": "last-prompt", "kind": "last-prompt"})

    def test_record_without_kind_is_an_error(self) -> None:
        document = json.loads(RecordCodec().line(SAMPLES["prompt"]))
        del document["kind"]

        with pytest.raises(RecordError, match="kind 'None'"):
            RecordCodec().parse(document)

    def test_missing_required_field_is_an_error(self) -> None:
        document = json.loads(RecordCodec().line(SAMPLES["prompt"]))
        del document["message"]

        with pytest.raises(RecordError, match="'prompt' record"):
            RecordCodec().parse(document)
