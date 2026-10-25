"""Загрузчик истории: восстановление сессии из порта, починка оборванного
хода и сводка сессии для списка.

Журнал агента — прямая линия без веток, поэтому из алгоритма загрузки
(docs/prompt_assembly/specs/10_loader.md) берётся только то, что нужно
линии: пропуск испорченной строки, граница сжатия с перенесённым хвостом,
починка последнего хода (там же, раздел 2.7). Ход закрыт, если за его
записями стоит запись turn_end; оборванному ходу вызовам последнего ответа
без результата дописывается результат-заглушка и конец хода с исходом
interrupted и пометкой человеку, разговору без ответа на последнее
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

from boba.agent.history import (
    BrokenLine,
    HistoryStore,
    SessionId,
    StoredHistory,
    StoredLine,
)
from boba.agent.ids import Clock, IdMint
from boba.agent.records import (
    AssistantMessage,
    CallStatus,
    ChainRecord,
    EnvironmentAttachment,
    HistoryFold,
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
    TurnEndRecord,
    TurnMark,
    TurnOutcome,
    Usage,
    UserMessage,
)
from boba.agent.session import Session
from boba.agent.tags import AgentTag
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

    async def stored(self, session: SessionId) -> StoredHistory:
        """Разговор сессии, прочитанный из хранилища заново, без починки:
        по нему запрос к модели пересобирается так, как его соберёт
        возобновление."""
        restored = Session(session)
        await self.restore(restored)

        return StoredHistory(restored.conversation())

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
        """Оборванный ход — за его записями нет turn_end: вызовам без
        результата — результат-заглушка, затем конец хода с пометкой
        человеку; разговору без ответа на последнее сообщение — ответ-заглушка.
        Заглушка ответа и команда запускающей программы хода не открывают."""
        conversation = session.conversation()
        if not self._speaks(conversation):
            return

        if self._open(conversation):
            for call in self._dangling_calls(conversation):
                yield session.add(self._stub_result(session, call))

            yield session.add(self._interrupted_end(session))

        if self._answered(session.conversation()):
            return

        yield session.add(self._no_response(session))

    def _open(self, conversation: Sequence[ChainRecord]) -> bool:
        """За последним концом хода есть записи хода: ход не закрыт."""
        for record in reversed(conversation):
            mark = record.turn_mark()
            if mark.closes_turn:
                return False

            if mark.in_turn:
                return True

        return False

    def _answered(self, conversation: Sequence[ChainRecord]) -> bool:
        """Последнее сообщение разговора получило ответ: ответом модели,
        заглушкой ответа или отказом агента ответить."""
        for record in reversed(conversation):
            mark = record.turn_mark()
            if mark.answers:
                return True

            if mark is TurnMark.ASKS:
                return False

        return True

    def _speaks(self, conversation: Sequence[ChainRecord]) -> bool:
        """В разговоре есть сообщения: вопрос или ответ."""
        for record in conversation:  # noqa: SIM110
            if record.turn_mark().speaks:
                return True

        return False

    def _interrupted_end(self, session: Session) -> TurnEndRecord:
        return TurnEndRecord(
            uuid=self._ids.uuid(),
            parent_uuid=session.last_chain_uuid(),
            timestamp=self._clock.now(),
            session_id=session.id.value,
            version=self._version,
            outcome=TurnOutcome.INTERRUPTED,
            notice=self._templates.read(AgentTemplateFile.TURN_INTERRUPTED_NOTICE),
            level=NoticeLevel.WARNING,
            detail="",
            error="",
            usage=Usage(),
            num_turns=0,
            duration_ms=0,
        )

    def _dangling_calls(
        self, conversation: Sequence[ChainRecord]
    ) -> Iterator[DanglingCall]:
        """Вызовы последнего ответа модели, на которые нет результата."""
        response = list(self._last_response(conversation))
        if not response:
            return

        answered: set[str] = set()
        for record in conversation:
            answered.update(record.answered_calls())

        for record in response:
            for call in record.tool_calls():
                if call.id in answered:
                    continue

                yield DanglingCall(call=call, assistant_uuid=record.uuid)

    def _last_response(
        self, conversation: Sequence[ChainRecord]
    ) -> Iterator[ChainRecord]:
        """Записи последнего ответа модели: один message.id, в порядке файла."""
        message_id = ""
        for record in reversed(conversation):
            reply = record.model_reply()
            if reply is not None:
                message_id = reply.message_id
                break

        if not message_id:
            return

        for record in conversation:
            reply = record.model_reply()
            if reply is not None and reply.message_id == message_id:
                yield record

    def _stub_result(
        self, session: Session, dangling: DanglingCall
    ) -> ToolResultRecord:
        text = self._templates.read(AgentTemplateFile.INTERRUPTED_TOOL_RESULT)
        block = ToolResultBlock(
            tool_use_id=dangling.call.id,
            content=AgentTag.TOOL_USE_ERROR.wrap(text),
            is_error=True,
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


class DigestFold(HistoryFold):
    """Свёртка сводки сессии по её записям: первый рабочий каталог из
    снимка окружения и первый вопрос человека. Строит SessionDigests на
    каждую сессию."""

    def __init__(self) -> None:
        self.cwd: str | None = None
        self.first_prompt: str | None = None

    def take(self, record: ChainRecord) -> None:
        record.announce(self)
        if self.first_prompt is not None:
            return

        self.first_prompt = record.human_text()

    def environment(self, environment: EnvironmentAttachment) -> None:
        if self.cwd is not None:
            return

        self.cwd = environment.snapshot.working_directory

    def digest(self) -> SessionDigest:
        return SessionDigest(cwd=self.cwd, first_prompt=self.first_prompt)


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
        fold = DigestFold()
        async with aclosing(self._store.read(session)) as lines:
            async for line in lines:
                if not isinstance(line, StoredLine):
                    continue

                try:
                    record = self._codec.parse(line.raw)
                except RecordError:
                    continue

                if not isinstance(record, ChainRecord):
                    continue

                fold.take(record)
                if fold.digest().complete():
                    break

        return fold.digest()
