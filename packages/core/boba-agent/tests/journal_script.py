"""Сценарий журнала для тестов: записи разговора, какими их пишет агент.

Строит записи через сессию, чтобы seq и связи цепочки были настоящими, и
складывает их в порт истории. Идентификаторы и время — настоящие, тесты
сравнивают записи по месту и виду, не по значениям.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import TypeVar

from boba.agent.history import HistoryStore, SessionLease
from boba.agent.ids import IdMint, SystemClock
from boba.agent.records import (
    AssistantBlock,
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
    PermissionMode,
    PermissionModeRecord,
    PreservedMessages,
    QueueOperation,
    QueueOperationRecord,
    Record,
    RenderedMessage,
    ReplyStopReason,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    Usage,
    UserMessage,
    UserRecord,
)
from boba.agent.session import Session

VERSION = "0.0.26.dev4"

R = TypeVar("R", bound=Record)


@dataclass(frozen=True)
class WrittenCall:
    """Вызов инструмента и запись assistant, которая его несёт."""

    call_id: str
    assistant_uuid: str


class JournalScript:
    """Пишет сценарий разговора в сессию и в порт истории."""

    def __init__(
        self, session: Session, store: HistoryStore, lease: SessionLease
    ) -> None:
        self._session = session
        self._store = store
        self._lease = lease
        self._ids = IdMint()
        self._clock = SystemClock()

    @property
    def session(self) -> Session:
        return self._session

    async def permission_mode(self, mode: PermissionMode) -> PermissionModeRecord:
        record = PermissionModeRecord(
            timestamp=self._clock.now(),
            session_id=self._session.id.value,
            permission_mode=mode,
        )

        return await self._put(record)

    async def enqueue(self, text: str) -> QueueOperationRecord:
        record = QueueOperationRecord(
            timestamp=self._clock.now(),
            session_id=self._session.id.value,
            operation=QueueOperation.ENQUEUE,
            content=text,
        )

        return await self._put(record)

    async def prompt(self, text: str) -> UserRecord:
        record = UserRecord(
            uuid=self._ids.uuid(),
            parent_uuid=self._session.last_chain_uuid(),
            timestamp=self._clock.now(),
            session_id=self._session.id.value,
            version=VERSION,
            message=UserMessage(content=text),
            origin=MessageOrigin(kind=OriginKind.HUMAN),
        )

        return await self._put(record)

    async def attachment(self, kind: str, text: str) -> AttachmentRecord:
        record = AttachmentRecord(
            uuid=self._ids.uuid(),
            parent_uuid=self._session.last_chain_uuid(),
            timestamp=self._clock.now(),
            session_id=self._session.id.value,
            version=VERSION,
            attachment=AttachmentPayload(type=kind),
            rendered=[RenderedMessage(content=text)],
            rendered_role=AttachmentRole.SYSTEM,
        )

        return await self._put(record)

    async def reply(
        self,
        text: str,
        calls: Sequence[str] = (),
        thinking: str = "",
        usage: Usage = Usage(input_tokens=100, output_tokens=20),
    ) -> list[WrittenCall]:
        """Ответ модели по блоку на запись: thinking, text, tool_use по именам."""
        blocks: list[AssistantBlock] = []
        if thinking:
            blocks.append(ThinkingBlock(thinking=thinking))

        if text:
            blocks.append(TextBlock(text=text))

        for name in calls:
            blocks.append(ToolUseBlock(id=self._ids.call_id(), name=name, input={}))

        stop = ReplyStopReason.END_TURN
        if calls:
            stop = ReplyStopReason.TOOL_USE

        message_id = self._ids.message_id()
        request_id = self._ids.request_id()
        written: list[WrittenCall] = []
        for index, block in enumerate(blocks):
            record = AssistantRecord(
                uuid=self._ids.uuid(),
                parent_uuid=self._session.last_chain_uuid(),
                timestamp=self._clock.now(),
                session_id=self._session.id.value,
                version=VERSION,
                message=AssistantMessage(
                    id=message_id,
                    model="fake-model",
                    content=[block],
                    stop_reason=stop,
                    usage=usage,
                ),
                api_block_index=index,
                request_id=request_id,
            )
            stored = await self._put(record)
            if isinstance(block, ToolUseBlock):
                call = WrittenCall(call_id=block.id, assistant_uuid=stored.uuid)
                written.append(call)

        return written

    async def tool_result(self, call: WrittenCall, text: str) -> UserRecord:
        block = ToolResultBlock(tool_use_id=call.call_id, content=text)
        record = UserRecord(
            uuid=self._ids.uuid(),
            parent_uuid=self._session.last_chain_uuid(),
            timestamp=self._clock.now(),
            session_id=self._session.id.value,
            version=VERSION,
            message=UserMessage(content=[block]),
            tool_use_result=text,
            source_tool_assistant_uuid=call.assistant_uuid,
        )

        return await self._put(record)

    async def compaction(
        self, summary: str, preserved: Sequence[str]
    ) -> tuple[CompactBoundaryRecord, UserRecord]:
        """Граница, пересказ и вложение даты одной пачкой, как пишет агент."""
        logical_parent = self._session.last_chain_uuid()
        summary_uuid = self._ids.uuid()
        all_uuids = [summary_uuid, *preserved]
        boundary = CompactBoundaryRecord(
            uuid=self._ids.uuid(),
            parent_uuid=None,
            timestamp=self._clock.now(),
            session_id=self._session.id.value,
            version=VERSION,
            logical_parent_uuid=logical_parent,
            compact_metadata=CompactMetadata(
                trigger=CompactTrigger.AUTO,
                pre_tokens=1000,
                post_tokens=200,
                duration_ms=10,
                preserved_messages=PreservedMessages(
                    anchor_uuid=summary_uuid, uuids=list(preserved), all_uuids=all_uuids
                ),
            ),
        )
        stored_boundary = self._session.add(boundary)
        date = AttachmentRecord(
            uuid=self._ids.uuid(),
            parent_uuid=stored_boundary.uuid,
            timestamp=self._clock.now(),
            session_id=self._session.id.value,
            version=VERSION,
            attachment=AttachmentPayload(type="date"),
            rendered=[RenderedMessage(content="Today's date is 2026-10-10.")],
            rendered_role=AttachmentRole.SYSTEM,
        )
        stored_date = self._session.add(date)
        wrapped = UserRecord(
            uuid=summary_uuid,
            parent_uuid=stored_date.uuid,
            timestamp=self._clock.now(),
            session_id=self._session.id.value,
            version=VERSION,
            message=UserMessage(content=summary),
            is_compact_summary=True,
            is_visible_in_transcript_only=True,
        )
        stored_summary = self._session.add(wrapped)
        await self._store.append(
            self._lease, [stored_boundary, stored_date, stored_summary]
        )

        return stored_boundary, stored_summary

    async def _put(self, record: R) -> R:
        stored = self._session.add(record)
        await self._store.append(self._lease, [stored])

        return stored
