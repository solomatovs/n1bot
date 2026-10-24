"""Загрузчик истории: восстановление сессии из порта, починка оборванного
хода и сводка сессии для списка.

Журнал агента — прямая линия без веток, поэтому из алгоритма загрузки
(docs/prompt_assembly/specs/10_loader.md) берётся только то, что нужно
линии: пропуск испорченной строки, граница сжатия с перенесённым хвостом,
починка последнего хода (там же, раздел 2.7): вызовам последнего
ответа без результата дописывается результат-заглушка, оборванному ходу —
пометка человеку (запись informational), разговору без ответа на последнее
сообщение — ответ-заглушка «No response requested.». Дописанные записи
агент сбрасывает в журнал сразу после открытия сессии. `restore`
восстанавливает без починки — так читает сессию процесс, который её не
ведёт. SessionDigests собирает по
записям сводку сессии: рабочий каталог из снимка окружения и первый вопрос
человека.

Ошибки:
HistoryError — порт истории отказал при чтении.
SessionError — записи журнала идут не по порядку seq.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator, Iterator, Sequence
from contextlib import aclosing
from dataclasses import dataclass, field

from boba.agent.history import BrokenLine, HistoryStore, SessionId, StoredLine
from boba.agent.ids import Clock, IdMint
from boba.agent.records import (
    AssistantMessage,
    AssistantRecord,
    AttachmentRecord,
    CallStatus,
    ChainRecord,
    EnvironmentAttachment,
    InformationalRecord,
    InterruptRecord,
    NoResponseRecord,
    NoticeLevel,
    Record,
    RecordCodec,
    RecordError,
    ReplyStopReason,
    Synthetic,
    TextBlock,
    ToolDenialKind,
    ToolResultBlock,
    ToolResultRecord,
    ToolUseBlock,
    Usage,
    UserMessage,
    UserRecord,
)
from boba.agent.session import Session
from boba.agent.template import AgentTemplate, AgentTemplateFile

logger = logging.getLogger(__name__)

__all__ = ["HistoryLoader", "LoadReport", "SessionDigest", "SessionDigests"]


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
        """Восстановление с починкой оборванного хода: так открывает сессию
        процесс, который будет её вести."""
        report = await self.restore(session)
        report.repaired.extend(self._repair(session))
        for repaired in report.repaired:
            logger.info(
                "history %s: appended %s %s on resume",
                session.id.value,
                repaired.type.value,
                repaired.uuid,
            )

        return report

    async def restore(self, session: Session) -> LoadReport:
        """Восстановление как есть, без дописанных записей: записи журнала
        входят в сессию по одной, по мере чтения, и сессия сама отпускает
        то, что модели уже не нужно."""
        report = LoadReport()
        async with aclosing(self._store.read(session.id)) as lines:
            async for line in lines:
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
                    session.restore((record,))

        report.stored_seq = session.last_seq

        return report

    async def records(self, session: SessionId) -> AsyncGenerator[ChainRecord, None]:
        """Записи цепочки сессии из журнала потоком, по одной, без починки и
        без накопления: так их читает воспроизведение беседы. Испорченные
        строки и записи пропускаются с записью в журнал процесса."""
        async with aclosing(self._store.read(session)) as lines:
            async for line in lines:
                if isinstance(line, BrokenLine):
                    logger.warning(
                        "history %s: line %d skipped: %s",
                        session.value,
                        line.number,
                        line.reason,
                    )
                    continue

                try:
                    record = self._codec.parse(line.raw)
                except RecordError as exc:
                    logger.warning(
                        "history %s: line %d skipped: %s",
                        session.value,
                        line.number,
                        exc,
                    )
                    continue

                if isinstance(record, ChainRecord):
                    yield record

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
        """Оборванный ход: вызовам без результата — результат-заглушка, ходу,
        который агент не довёл до конца, — пометка человеку, разговору без
        ответа на последнее сообщение — ответ-заглушка. Маркер остановки
        пользователем — конец хода, а не обрыв: пометки за ним нет."""
        conversation = session.conversation()
        last = self._last_significant(conversation)
        if last is None:
            return

        dangling = list(self._dangling_calls(conversation))
        for call in dangling:
            yield session.add(self._stub_result(session, call))

        interrupted = bool(dangling)
        if not interrupted:
            interrupted = not self._ended(last)

        if interrupted:
            yield session.add(self._interrupted_notice(session))

        last = self._last_significant(session.conversation())
        if isinstance(last, AssistantRecord):
            return

        yield session.add(self._no_response(session))

    def _ended(self, last: ChainRecord) -> bool:
        """Последняя значимая запись закрывает ход: ответ модели или маркер
        остановки хода пользователем."""
        if isinstance(last, AssistantRecord):
            return True

        return isinstance(last, InterruptRecord)

    def _interrupted_notice(self, session: Session) -> InformationalRecord:
        return InformationalRecord(
            uuid=self._ids.uuid(),
            parent_uuid=session.last_chain_uuid(),
            timestamp=self._clock.now(),
            session_id=session.id.value,
            version=self._version,
            content=self._templates.read(AgentTemplateFile.TURN_INTERRUPTED_NOTICE),
            level=NoticeLevel.WARNING,
            detail="",
        )

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
            if not isinstance(record, ToolResultRecord):
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

    def _stub_result(
        self, session: Session, dangling: DanglingCall
    ) -> ToolResultRecord:
        text = self._templates.read(AgentTemplateFile.INTERRUPTED_TOOL_RESULT)
        block = ToolResultBlock(
            tool_use_id=dangling.call.id, content=text, is_error=True
        )

        return ToolResultRecord(
            uuid=self._ids.uuid(),
            parent_uuid=session.last_chain_uuid(),
            timestamp=self._clock.now(),
            session_id=session.id.value,
            version=self._version,
            message=UserMessage(content=[block]),
            source_tool_assistant_uuid=dangling.assistant_uuid,
            tool_denial_kind=ToolDenialKind.INTERRUPTED,
            status=CallStatus.FAILED,
        )

    def _no_response(self, session: Session) -> NoResponseRecord:
        text = self._templates.read(AgentTemplateFile.NO_RESPONSE_REQUESTED)
        message = AssistantMessage(
            id=self._ids.message_id(),
            model=Synthetic.MODEL.value,
            content=[TextBlock(text=text)],
            stop_reason=ReplyStopReason.STOP_SEQUENCE,
            usage=Usage(),
        )

        return NoResponseRecord(
            uuid=self._ids.uuid(),
            parent_uuid=session.last_chain_uuid(),
            timestamp=self._clock.now(),
            session_id=session.id.value,
            version=self._version,
            message=message,
            request_id=self._ids.request_id(),
        )


@dataclass(frozen=True)
class SessionDigest:
    """Сводка сессии по её записям: рабочий каталог из снимка окружения и
    первый вопрос человека; None — в записях этого нет."""

    cwd: str | None
    first_prompt: str | None

    def complete(self) -> bool:
        return self.cwd is not None and self.first_prompt is not None


class SessionDigests:
    """Сводки сессий из порта истории для списка сессий.

    Читает записи сессии по порядку и останавливает разбор, как только
    найдены и каталог, и первый вопрос; испорченные строки пропускает.
    Создаёт вход процесса рядом с хранилищем.
    """

    def __init__(self, store: HistoryStore) -> None:
        self._store = store
        self._codec = RecordCodec()

    async def digest(self, session: SessionId) -> SessionDigest:
        """Ошибки:
        HistoryError — порт истории отказал при чтении.
        """
        digest = SessionDigest(cwd=None, first_prompt=None)
        async with aclosing(self._store.read(session)) as lines:
            async for line in lines:
                if not isinstance(line, StoredLine):
                    continue

                try:
                    record = self._codec.parse(line.raw)
                except RecordError:
                    continue

                digest = self._absorb(digest, record)
                if digest.complete():
                    break

        return digest

    def _absorb(self, digest: SessionDigest, record: Record) -> SessionDigest:
        if isinstance(record, UserRecord) and digest.first_prompt is None:
            text = record.human_text()
            if text is not None:
                return SessionDigest(cwd=digest.cwd, first_prompt=text)

        if isinstance(record, AttachmentRecord) and digest.cwd is None:
            cwd = self._working_directory(record)
            if cwd is not None:
                return SessionDigest(cwd=cwd, first_prompt=digest.first_prompt)

        return digest

    def _working_directory(self, record: AttachmentRecord) -> str | None:
        attachment = record.attachment
        if not isinstance(attachment, EnvironmentAttachment):
            return None

        return attachment.snapshot.working_directory
