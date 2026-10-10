"""Сессия: выдача seq, восстановление по порядку, проекция разговора от
последней полной границы сжатия с перенесённым хвостом."""

from __future__ import annotations

import pytest

from boba.agent.history import SessionId
from boba.agent.records import (
    AssistantMessage,
    AssistantRecord,
    AttachmentPayload,
    AttachmentRecord,
    CompactBoundaryRecord,
    CompactMetadata,
    CompactTrigger,
    PermissionMode,
    PermissionModeRecord,
    PreservedMessages,
    ReplyStopReason,
    TextBlock,
    Usage,
    UserMessage,
    UserRecord,
)
from boba.agent.session import Session, SessionError

SESSION = SessionId(value="s-1")
STAMP = "2026-10-10T10:00:00.000Z"
VERSION = "0.0.26.dev4"


def user(uuid: str, parent: str | None, text: str, summary: bool = False) -> UserRecord:
    flag: bool | None = None
    if summary:
        flag = True

    return UserRecord(
        uuid=uuid,
        parent_uuid=parent,
        timestamp=STAMP,
        session_id=SESSION.value,
        version=VERSION,
        message=UserMessage(content=text),
        is_compact_summary=flag,
    )


def assistant(uuid: str, parent: str | None, text: str) -> AssistantRecord:
    return AssistantRecord(
        uuid=uuid,
        parent_uuid=parent,
        timestamp=STAMP,
        session_id=SESSION.value,
        version=VERSION,
        message=AssistantMessage(
            id=f"msg_{uuid}",
            model="fake",
            content=[TextBlock(text=text)],
            stop_reason=ReplyStopReason.END_TURN,
            usage=Usage(input_tokens=50, output_tokens=5),
        ),
        request_id=f"req_{uuid}",
    )


def boundary(uuid: str, anchor: str, preserved: list[str]) -> CompactBoundaryRecord:
    return CompactBoundaryRecord(
        uuid=uuid,
        parent_uuid=None,
        timestamp=STAMP,
        session_id=SESSION.value,
        version=VERSION,
        logical_parent_uuid=None,
        compact_metadata=CompactMetadata(
            trigger=CompactTrigger.AUTO,
            pre_tokens=1,
            post_tokens=1,
            duration_ms=1,
            preserved_messages=PreservedMessages(
                anchor_uuid=anchor, uuids=preserved, all_uuids=[anchor, *preserved]
            ),
        ),
    )


def attachment(uuid: str, parent: str | None) -> AttachmentRecord:
    return AttachmentRecord(
        uuid=uuid,
        parent_uuid=parent,
        timestamp=STAMP,
        session_id=SESSION.value,
        version=VERSION,
        attachment=AttachmentPayload(type="date"),
    )


class TestSeq:
    def test_add_numbers_records_from_one(self) -> None:
        session = Session(SESSION)

        first = session.add(user("u1", None, "a"))
        second = session.add(
            PermissionModeRecord(
                timestamp=STAMP,
                session_id=SESSION.value,
                permission_mode=PermissionMode.DEFAULT,
            )
        )

        assert (first.seq, second.seq) == (1, 2)
        assert session.last_seq == 2
        assert [r.seq for r in session.since(1)] == [2]

    def test_add_rejects_a_record_that_carries_seq(self) -> None:
        session = Session(SESSION)

        with pytest.raises(SessionError, match="already carries seq 7"):
            session.add(user("u1", None, "a").model_copy(update={"seq": 7}))

    def test_restore_requires_increasing_seq(self) -> None:
        session = Session(SESSION)
        first = user("u1", None, "a").model_copy(update={"seq": 3})
        second = user("u2", "u1", "b").model_copy(update={"seq": 3})

        with pytest.raises(SessionError, match="seq 3 comes after seq 3"):
            session.restore([first, second])

    def test_projections_split_chain_and_service(self) -> None:
        session = Session(SESSION)
        session.add(
            PermissionModeRecord(
                timestamp=STAMP,
                session_id=SESSION.value,
                permission_mode=PermissionMode.DEFAULT,
            )
        )
        session.add(user("u1", None, "a"))
        session.add(assistant("a1", "u1", "b"))

        assert [r.uuid for r in session.chain()] == ["u1", "a1"]
        assert [r.type.value for r in session.service()] == ["permission-mode"]
        assert session.last_chain_uuid() == "a1"
        assert session.find("u1") is not None


class TestConversation:
    def test_without_boundary_is_the_whole_chain(self) -> None:
        session = Session(SESSION)
        session.add(user("u1", None, "a"))
        session.add(assistant("a1", "u1", "b"))

        assert [r.uuid for r in session.conversation()] == ["u1", "a1"]

    def test_boundary_moves_the_preserved_tail_behind_the_summary(self) -> None:
        session = Session(SESSION)
        session.add(user("u1", None, "a"))
        session.add(assistant("a1", "u1", "b"))
        session.add(user("u2", "a1", "c"))
        session.add(assistant("a2", "u2", "d"))
        session.add(boundary("b1", "s1", ["u2", "a2"]))
        session.add(attachment("d1", "b1"))
        session.add(user("s1", "d1", "summary", summary=True))
        session.add(user("u3", "s1", "e"))

        conversation = session.conversation()

        assert [r.uuid for r in conversation] == ["b1", "d1", "s1", "u2", "a2", "u3"]
        tail_reply = conversation[4]
        assert isinstance(tail_reply, AssistantRecord)
        assert tail_reply.message.usage.is_zero()
        original = session.find("a2")
        assert isinstance(original, AssistantRecord)
        assert original.message.usage.input_tokens == 50

    def test_boundary_without_its_summary_is_ignored(self) -> None:
        session = Session(SESSION)
        session.add(user("u1", None, "a"))
        session.add(assistant("a1", "u1", "b"))
        session.add(boundary("b1", "s1", ["a1"]))

        assert [r.uuid for r in session.conversation()] == ["u1", "a1", "b1"]

    def test_latest_complete_boundary_wins(self) -> None:
        session = Session(SESSION)
        session.add(user("u1", None, "a"))
        session.add(assistant("a1", "u1", "b"))
        session.add(boundary("b1", "s1", ["a1"]))
        session.add(user("s1", "b1", "first summary", summary=True))
        session.add(user("u2", "s1", "c"))
        session.add(assistant("a2", "u2", "d"))
        session.add(boundary("b2", "s2", ["u2", "a2"]))
        session.add(user("s2", "b2", "second summary", summary=True))

        assert [r.uuid for r in session.conversation()] == ["b2", "s2", "u2", "a2"]

    def test_cache_is_dropped_on_add(self) -> None:
        session = Session(SESSION)
        session.add(user("u1", None, "a"))
        before = len(session.conversation())
        session.add(assistant("a1", "u1", "b"))

        assert (before, len(session.conversation())) == (1, 2)
