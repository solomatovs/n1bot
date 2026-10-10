"""Загрузчик истории: восстановление сессии из порта и починка оборванного хода.

Журнал агента — прямая линия без веток, поэтому из алгоритма загрузки
(docs/prompt_assembly/specs/10_loader.md) берётся только то, что нужно
линии: пропуск испорченной строки, граница сжатия с перенесённым хвостом,
починка последнего хода (там же, раздел 2.7): вызовам последнего
ответа без результата дописывается результат-заглушка, разговору без ответа
на последнее сообщение — ответ-заглушка «No response requested.». Дописанные
записи остаются в сессии несброшенными: писатель положит их в журнал перед
первой записью следующего хода.

Ошибки:
HistoryError — порт истории отказал при чтении.
SessionError — записи журнала идут не по порядку seq.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field

from boba.agent.history import BrokenLine, HistoryStore, StoredLine
from boba.agent.ids import Clock, IdMint
from boba.agent.records import (
    AssistantMessage,
    AssistantRecord,
    AttachmentRecord,
    ChainRecord,
    Record,
    RecordCodec,
    RecordError,
    ReplyStopReason,
    Synthetic,
    TextBlock,
    ToolDenialKind,
    ToolResultBlock,
    ToolUseBlock,
    Usage,
    UserMessage,
    UserRecord,
)
from boba.agent.session import Session
from boba.agent.template import AgentTemplate, AgentTemplateFile

logger = logging.getLogger(__name__)

__all__ = ["HistoryLoader", "LoadReport"]


@dataclass
class LoadReport:
    """Итог загрузки: что пропущено, что дописано, до какого seq журнал полон."""

    stored_seq: int = 0
    skipped_lines: int = 0
    skipped_records: int = 0
    repaired: list[ChainRecord] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class DanglingCall:
    """Вызов последнего ответа, оставшийся без результата."""

    call: ToolUseBlock
    assistant_uuid: str


class HistoryLoader:
    """Строит сессию из записей порта и чинит оборванный ход."""

    def __init__(
        self,
        store: HistoryStore,
        templates: AgentTemplate,
        ids: IdMint,
        clock: Clock,
        version: str,
    ) -> None:
        self._store = store
        self._codec = RecordCodec()
        self._templates = templates
        self._ids = ids
        self._clock = clock
        self._version = version

    async def load(self, session: Session) -> LoadReport:
        report = LoadReport()
        records: list[Record] = []
        async for line in self._store.read(session.id):
            if isinstance(line, BrokenLine):
                report.skipped_lines += 1
                report.notes.append(f"line {line.number} skipped: {line.reason}")
                logger.warning(
                    "history %s: line %d skipped: %s",
                    session.id.value,
                    line.number,
                    line.reason,
                )
                continue

            record = self._parse(session, line, report)
            if record is not None:
                records.append(record)

        session.restore(records)
        report.stored_seq = session.last_seq
        report.repaired.extend(self._repair(session))
        for repaired in report.repaired:
            logger.info(
                "history %s: appended %s %s on resume",
                session.id.value,
                repaired.type.value,
                repaired.uuid,
            )

        return report

    def _parse(
        self, session: Session, line: StoredLine, report: LoadReport
    ) -> Record | None:
        try:
            return self._codec.parse(line.raw)
        except RecordError as exc:
            report.skipped_records += 1
            report.notes.append(f"line {line.number} skipped: {exc}")
            logger.warning(
                "history %s: line %d skipped: %s", session.id.value, line.number, exc
            )

            return None

    def _repair(self, session: Session) -> Iterator[ChainRecord]:
        conversation = session.conversation()
        last = self._last_significant(conversation)
        if last is None:
            return

        for dangling in self._dangling_calls(conversation):
            yield session.add(self._stub_result(session, dangling))

        last = self._last_significant(session.conversation())
        if isinstance(last, AssistantRecord):
            return

        yield session.add(self._no_response(session))

    def _last_significant(
        self, conversation: Sequence[ChainRecord]
    ) -> ChainRecord | None:
        """Последняя запись, по которой судят о конце хода: не system."""
        for record in reversed(conversation):
            if isinstance(record, UserRecord | AssistantRecord | AttachmentRecord):
                return record

        return None

    def _dangling_calls(
        self, conversation: Sequence[ChainRecord]
    ) -> Iterator[DanglingCall]:
        """Вызовы последнего ответа модели, на которые нет результата."""
        response = list(self._last_response(conversation))
        if not response:
            return

        answered: set[str] = set()
        for record in conversation:
            if not isinstance(record, UserRecord):
                continue

            for result in record.message.tool_results():
                answered.add(result.tool_use_id)

        for record in response:
            for call in record.message.tool_uses():
                if call.id in answered:
                    continue

                yield DanglingCall(call=call, assistant_uuid=record.uuid)

    def _last_response(
        self, conversation: Sequence[ChainRecord]
    ) -> Iterator[AssistantRecord]:
        """Записи последнего ответа модели: один message.id, в порядке файла."""
        message_id = ""
        for record in reversed(conversation):
            if isinstance(record, AssistantRecord) and not record.is_synthetic():
                message_id = record.message.id
                break

        if not message_id:
            return

        for record in conversation:
            if isinstance(record, AssistantRecord) and record.message.id == message_id:
                yield record

    def _stub_result(self, session: Session, dangling: DanglingCall) -> UserRecord:
        text = self._templates.read(AgentTemplateFile.INTERRUPTED_TOOL_RESULT)
        block = ToolResultBlock(
            tool_use_id=dangling.call.id, content=text, is_error=True
        )

        return UserRecord(
            uuid=self._ids.uuid(),
            parent_uuid=session.last_chain_uuid(),
            timestamp=self._clock.now(),
            session_id=session.id.value,
            version=self._version,
            message=UserMessage(content=[block]),
            tool_use_result=text,
            source_tool_assistant_uuid=dangling.assistant_uuid,
            tool_denial_kind=ToolDenialKind.INTERRUPTED,
        )

    def _no_response(self, session: Session) -> AssistantRecord:
        text = self._templates.read(AgentTemplateFile.NO_RESPONSE_REQUESTED)
        message = AssistantMessage(
            id=self._ids.message_id(),
            model=Synthetic.MODEL.value,
            content=[TextBlock(text=text)],
            stop_reason=ReplyStopReason.STOP_SEQUENCE,
            usage=Usage(),
        )

        return AssistantRecord(
            uuid=self._ids.uuid(),
            parent_uuid=session.last_chain_uuid(),
            timestamp=self._clock.now(),
            session_id=session.id.value,
            version=self._version,
            message=message,
            request_id=self._ids.request_id(),
        )
