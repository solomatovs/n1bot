"""Перевод сообщений langchain одного треда в записи журнала агента.

Соответствие:
HumanMessage → `user` с текстом вопроса; вложения вопроса — следом записью
    `attachment` вида chat_files, модели они уходят списком путей.
AIMessage → по записи `assistant` на блок (thinking, text, tool_use) с общим
    `message.id`, `stop_reason` по наличию вызовов; вызовы подготовки хода
    (prefetch) — те же записи от модели `<synthetic>`.
AIMessage с пометкой `error` → синтетическая запись об ошибке модели.
AIMessage с пометкой `stopped` → оборванный ответ (`isAbortedMidStream`) и
    маркер прерывания записью `user`, как пишет их сам агент.
ToolMessage → `user` с блоком `tool_result`; артефакт, адрес журнала и типы
    содержимого аргументов — в `toolUseResult` под своими ключами.

Идентификаторы источника сохраняются там, где по ним привязаны элементы и
оценки chainlit: id вопроса — uuid записи `user`, id ответа — `message.id`
записей `assistant`, id вызова — id блока `tool_use` и `tool_use_id` итога.
Время записей — время последнего checkpoint'а треда: своего времени у
сообщений источника нет.

Ошибки:
TranslateError — идентификатор треда не годится в идентификатор сессии.
Сообщение, которое не ложится в запись, пропускается с заметкой в итоге.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, ClassVar, Final

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from pydantic import ValidationError

from boba.agent.history import SessionId
from boba.agent.ids import Clock, IdMint
from boba.agent.records import (
    AssistantBlock,
    AssistantMessage,
    AssistantRecord,
    AttachmentPayload,
    AttachmentRecord,
    AttachmentRole,
    MessageOrigin,
    OriginKind,
    RenderedMessage,
    ReplyStopReason,
    Synthetic,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    Usage,
    UserMessage,
    UserRecord,
)
from boba.agent.session import Session
from boba.agent.template import AgentTemplate, AgentTemplateFile
from boba.history_migrate.source import ThreadTranscript

__all__ = [
    "CheckpointClock",
    "MigratedAttachment",
    "MigratedModel",
    "RecordTranslator",
    "ToolUseResultKey",
    "TranslateError",
    "Translation",
]


class TranslateError(Exception):
    """Тред не переводится в сессию."""


class MessageExtra(StrEnum):
    """Ключи additional_kwargs и response_metadata сообщений источника."""

    REASONING_CONTENT = "reasoning_content"
    ATTACHMENTS = "attachments"
    PREFETCH_ELAPSED = "prefetch_elapsed_ms"
    STOPPED = "stopped"
    ERROR = "error"
    MODEL_NAME = "model_name"
    JOURNAL = "boba_journal"
    MEDIA = "boba_call_media"


class AttachmentField(StrEnum):
    """Ключи записи вложения в additional_kwargs вопроса и в payload записи."""

    NAME = "name"
    PATH = "path"
    FILES = "files"


class ContentBlockKey(StrEnum):
    """Ключи блока содержимого сообщения langchain, когда content — список."""

    TYPE = "type"
    TEXT = "text"


class ToolCallKey:
    """Ключи TypedDict ToolCall langchain: pyright принимает только литерал."""

    ID: Final = "id"
    NAME: Final = "name"
    ARGS: Final = "args"


class UsageKey:
    """Ключи TypedDict UsageMetadata langchain."""

    INPUT: Final = "input_tokens"
    OUTPUT: Final = "output_tokens"


class ToolStatus(StrEnum):
    """Статус ToolMessage langchain."""

    SUCCESS = "success"
    ERROR = "error"


class ToolUseResultKey(StrEnum):
    """Ключи `toolUseResult` мигрированного итога инструмента."""

    ARTIFACT = "artifact"
    JOURNAL = "journal"
    MEDIA = "media"


class MigratedAttachment(StrEnum):
    """Виды вложений, которые заводит миграция."""

    FILES = "chat_files"


class MigratedModel(StrEnum):
    """Имя модели записи, когда источник его не сохранил."""

    UNKNOWN = "<unknown>"


class CheckpointClock(Clock):
    """Реализация Clock временем checkpoint'а: все записи треда несут его.

    Время checkpoint'а — ISO 8601 с зоной; нечитаемое время заменяется
    текущим, и exact становится False.
    """

    FORMAT: ClassVar[str] = "%Y-%m-%dT%H:%M:%S"

    def __init__(self, raw: str, fallback: Clock) -> None:
        self.exact = True
        self._stamp = self._render(raw, fallback)

    def now(self) -> str:
        return self._stamp

    def _render(self, raw: str, fallback: Clock) -> str:
        try:
            moment = datetime.fromisoformat(raw)
        except ValueError:
            self.exact = False
            return fallback.now()

        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=UTC)

        moment = moment.astimezone(UTC)
        millis = moment.microsecond // 1000

        return f"{moment.strftime(self.FORMAT)}.{millis:03d}Z"


@dataclass(frozen=True)
class Reply:
    """Ответ модели, который ложится записями `assistant`: по записи на блок."""

    message_id: str
    model: str
    blocks: Sequence[AssistantBlock]
    stop_reason: ReplyStopReason
    usage: Usage
    aborted: bool = False


@dataclass
class Translation:
    """Итог перевода треда: сессия с записями, заметки и число пропусков."""

    session: Session
    notes: list[str] = field(default_factory=list)
    skipped: int = 0


class RecordTranslator:
    """Переводчик тредов: на каждый тред собирает ThreadRecords и прогоняет
    через него сообщения.

    Создаётся входом скрипта; зовёт его прогон (HistoryMigration) на каждый
    тред источника.
    """

    def __init__(
        self, ids: IdMint, templates: AgentTemplate, clock: Clock, version: str
    ) -> None:
        self._ids = ids
        self._templates = templates
        self._clock = clock
        self._version = version

    def translate(self, transcript: ThreadTranscript) -> Translation:
        """Ошибки:
        TranslateError — идентификатор треда не годится в идентификатор сессии.
        """
        try:
            session_id = SessionId(value=transcript.thread_id)
        except ValidationError as exc:
            msg = (
                f"thread {transcript.thread_id!r} cannot become a session: "
                f"its id does not fit SessionId: {exc}"
            )
            raise TranslateError(msg) from exc

        clock = CheckpointClock(transcript.stamp, self._clock)
        records = ThreadRecords(
            Session(session_id), self._ids, self._templates, clock, self._version
        )
        if not clock.exact:
            records.note(
                f"checkpoint time {transcript.stamp!r} is not ISO 8601; records "
                "carry the migration time"
            )

        for message in transcript.messages:
            records.take(message)

        return records.result()


class ThreadRecords:
    """Сборка записей одного треда: сессия выдаёт seq и родителя, объект
    держит соответствие вызовов записям `assistant` и последнюю виденную модель.

    Создаётся переводчиком на тред; наружу отдаёт Translation.
    """

    ATTACHMENTS_NOTE: ClassVar[str] = (
        "Attached files, available to tools at these paths:"
    )

    def __init__(
        self,
        session: Session,
        ids: IdMint,
        templates: AgentTemplate,
        clock: Clock,
        version: str,
    ) -> None:
        self._session = session
        self._ids = ids
        self._templates = templates
        self._clock = clock
        self._version = version
        self._calls: dict[str, str] = {}
        self._model: str = MigratedModel.UNKNOWN.value
        self._notes: list[str] = []
        self._skipped = 0

    def note(self, text: str) -> None:
        self._notes.append(text)

    def result(self) -> Translation:
        return Translation(
            session=self._session, notes=self._notes, skipped=self._skipped
        )

    def take(self, message: BaseMessage) -> None:
        match message:
            case HumanMessage():
                self._human(message)
            case ToolMessage():
                self._tool(message)
            case AIMessage():
                self._assistant(message)
            case _:
                kind = type(message).__name__
                self._skip(f"{kind} {message.id!r} has no record kind")

    def _skip(self, reason: str) -> None:
        self._skipped += 1
        self._notes.append(f"skipped: {reason}")

    def _human(self, message: HumanMessage) -> None:
        record = UserRecord(
            uuid=self._question_uuid(message),
            parent_uuid=self._session.last_chain_uuid(),
            timestamp=self._clock.now(),
            session_id=self._session.id.value,
            version=self._version,
            message=UserMessage(content=self._text(message)),
            origin=MessageOrigin(kind=OriginKind.HUMAN),
        )
        self._session.add(record)

        files = list(self._attachments(message))
        if not files:
            return

        lines: list[str] = []
        for item in files:
            name = item[AttachmentField.NAME.value]
            path = item[AttachmentField.PATH.value]
            lines.append(f"- {name}: {path}")

        rendered = f"{self.ATTACHMENTS_NOTE}\n" + "\n".join(lines)
        payload = AttachmentPayload.model_validate(
            {
                ContentBlockKey.TYPE.value: MigratedAttachment.FILES.value,
                AttachmentField.FILES.value: files,
            }
        )
        attachment = AttachmentRecord(
            uuid=self._ids.uuid(),
            parent_uuid=self._session.last_chain_uuid(),
            timestamp=self._clock.now(),
            session_id=self._session.id.value,
            version=self._version,
            attachment=payload,
            rendered=[RenderedMessage(content=rendered)],
            rendered_role=AttachmentRole.USER,
        )
        self._session.add(attachment)

    def _question_uuid(self, message: HumanMessage) -> str:
        """uuid записи вопроса — id сообщения chainlit, по нему привязаны
        элементы; без id или при повторе чеканится новый."""
        if not message.id:
            self.note("a question without an id got a minted uuid")
            return self._ids.uuid()

        if self._session.find(message.id) is not None:
            self.note(f"question id {message.id} repeats in the thread; minted a uuid")
            return self._ids.uuid()

        return message.id

    def _attachments(self, message: HumanMessage) -> Iterator[dict[str, str]]:
        raw = message.additional_kwargs.get(MessageExtra.ATTACHMENTS.value)
        if not isinstance(raw, list):
            return

        for item in raw:
            if not isinstance(item, Mapping):
                self.note(f"attachment entry {item!r} is not a mapping; dropped")
                continue

            name = item.get(AttachmentField.NAME.value)
            path = item.get(AttachmentField.PATH.value)
            if name is None or path is None:
                self.note(f"attachment entry {dict(item)!r} lacks name or path")
                continue

            yield {
                AttachmentField.NAME.value: str(name),
                AttachmentField.PATH.value: str(path),
            }

    def _assistant(self, message: AIMessage) -> None:
        extra = message.additional_kwargs
        if extra.get(MessageExtra.ERROR.value):
            self._api_failure(message)
            return

        if extra.get(MessageExtra.STOPPED.value):
            self._interrupted(message)
            return

        blocks = list(self._blocks(message))
        if not blocks:
            self._skip(f"AIMessage {message.id!r} has no text, reasoning or calls")
            return

        stop_reason = ReplyStopReason.END_TURN
        if message.tool_calls:
            stop_reason = ReplyStopReason.TOOL_USE

        self._write_reply(
            Reply(
                message_id=self._message_id(message),
                model=self._model_of(message),
                blocks=blocks,
                stop_reason=stop_reason,
                usage=self._usage(message),
            )
        )

    def _api_failure(self, message: AIMessage) -> None:
        text = self._text(message)
        record = AssistantRecord(
            uuid=self._ids.uuid(),
            parent_uuid=self._session.last_chain_uuid(),
            timestamp=self._clock.now(),
            session_id=self._session.id.value,
            version=self._version,
            message=AssistantMessage(
                id=self._message_id(message),
                model=Synthetic.MODEL.value,
                content=[TextBlock(text=text)],
                stop_reason=ReplyStopReason.STOP_SEQUENCE,
                usage=Usage(),
            ),
            request_id=self._ids.request_id(),
            is_api_error_message=True,
            error=text,
        )
        self._session.add(record)

    def _interrupted(self, message: AIMessage) -> None:
        blocks = list(self._blocks(message))
        if blocks:
            self._write_reply(
                Reply(
                    message_id=self._message_id(message),
                    model=Synthetic.MODEL.value,
                    blocks=blocks,
                    stop_reason=ReplyStopReason.END_TURN,
                    usage=self._usage(message),
                    aborted=True,
                )
            )

        marker = UserRecord(
            uuid=self._ids.uuid(),
            parent_uuid=self._session.last_chain_uuid(),
            timestamp=self._clock.now(),
            session_id=self._session.id.value,
            version=self._version,
            message=UserMessage(
                content=self._templates.read(AgentTemplateFile.REQUEST_INTERRUPTED)
            ),
        )
        self._session.add(marker)

    def _write_reply(self, reply: Reply) -> None:
        request_id = self._ids.request_id()
        mid_stream: bool | None = None
        if reply.aborted:
            mid_stream = True

        for index, block in enumerate(reply.blocks):
            record = AssistantRecord(
                uuid=self._ids.uuid(),
                parent_uuid=self._session.last_chain_uuid(),
                timestamp=self._clock.now(),
                session_id=self._session.id.value,
                version=self._version,
                message=AssistantMessage(
                    id=reply.message_id,
                    model=reply.model,
                    content=[block],
                    stop_reason=reply.stop_reason,
                    usage=reply.usage,
                ),
                api_block_index=index,
                request_id=request_id,
                is_aborted_mid_stream=mid_stream,
            )
            written = self._session.add(record)
            if isinstance(block, ToolUseBlock):
                self._calls[block.id] = written.uuid

    def _message_id(self, message: AIMessage) -> str:
        if message.id:
            return message.id

        self.note("an assistant message without an id got a minted message id")

        return self._ids.message_id()

    def _model_of(self, message: AIMessage) -> str:
        if MessageExtra.PREFETCH_ELAPSED.value in message.additional_kwargs:
            return Synthetic.MODEL.value

        named = message.response_metadata.get(MessageExtra.MODEL_NAME.value)
        if named:
            self._model = str(named)

        return self._model

    def _blocks(self, message: AIMessage) -> Iterator[AssistantBlock]:
        reasoning = message.additional_kwargs.get(MessageExtra.REASONING_CONTENT.value)
        if reasoning:
            yield ThinkingBlock(thinking=str(reasoning))

        text = self._text(message)
        if text:
            yield TextBlock(text=text)

        for call in message.tool_calls:
            call_id = call.get(ToolCallKey.ID)
            name = call.get(ToolCallKey.NAME)
            if not call_id or not name:
                self.note(f"tool call {dict(call)!r} lacks id or name; dropped")
                continue

            args = call.get(ToolCallKey.ARGS)
            if not isinstance(args, Mapping):
                args = {}

            yield ToolUseBlock(id=str(call_id), name=str(name), input=dict(args))

    @staticmethod
    def _usage(message: AIMessage) -> Usage:
        usage = message.usage_metadata
        if usage is None:
            return Usage()

        return Usage(
            input_tokens=usage[UsageKey.INPUT], output_tokens=usage[UsageKey.OUTPUT]
        )

    def _tool(self, message: ToolMessage) -> None:
        is_error: bool | None = None
        if message.status == ToolStatus.ERROR.value:
            is_error = True

        block = ToolResultBlock(
            tool_use_id=message.tool_call_id,
            content=self._text(message),
            is_error=is_error,
        )
        source = self._calls.get(message.tool_call_id)
        if source is None:
            self.note(
                f"tool result {message.tool_call_id} answers a call this thread "
                "does not record"
            )

        record = UserRecord(
            uuid=self._ids.uuid(),
            parent_uuid=self._session.last_chain_uuid(),
            timestamp=self._clock.now(),
            session_id=self._session.id.value,
            version=self._version,
            message=UserMessage(content=[block]),
            tool_use_result=self._tool_use_result(message),
            source_tool_assistant_uuid=source,
        )
        self._session.add(record)

    @staticmethod
    def _tool_use_result(message: ToolMessage) -> dict[str, Any] | None:
        result: dict[str, Any] = {}
        if message.artifact is not None:
            result[ToolUseResultKey.ARTIFACT.value] = message.artifact

        journal = message.response_metadata.get(MessageExtra.JOURNAL.value)
        if journal is not None:
            result[ToolUseResultKey.JOURNAL.value] = journal

        media = message.response_metadata.get(MessageExtra.MEDIA.value)
        if media is not None:
            result[ToolUseResultKey.MEDIA.value] = media

        if not result:
            return None

        return result

    @staticmethod
    def _text(message: BaseMessage) -> str:
        """Текст сообщения: строка как есть, у списка блоков — склейка текстовых."""
        content = message.content
        if isinstance(content, str):
            return content

        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
                continue

            if block.get(ContentBlockKey.TYPE.value) != ContentBlockKey.TEXT.value:
                continue

            parts.append(str(block.get(ContentBlockKey.TEXT.value, "")))

        return "".join(parts)
