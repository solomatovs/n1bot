"""Записи истории агента: модели строк журнала и их кодек.

Формат — журнал (разбор в docs/prompt_assembly):
записи цепочки `user`, `assistant`, `attachment`, `system`
связаны `uuid` → `parentUuid`, служебные записи цепочки не имеют.
Поле `seq` — наше: порядковый номер записи в сессии, его выдаёт сессия при добавлении.
Запись читается в модель один раз, в загрузчике, дальше по коду ходит модель;
неизвестное поле известного вида игнорируется, чтобы старый агент читал журнал нового.

Содержимое сообщений хранится блоками, а модель получает его в конверте
порта `ChatTurn`/`ChatReply`; перевод взаимно однозначен и живёт в сборщике
запроса и записи ответа (план, раздел 5.12):

    блок `thinking`                        ↔ `reasoning`
    блок `text`                            ↔ `content`
    блок `tool_use` {id, name, input}      ↔ `ToolCall(id, name, arguments)`
    блок `tool_result` в записи `user`     ↔ сообщение роли `tool` с `tool_call_id`
    `stop_reason` end_turn/tool_use/max_tokens ↔ `finish_reason` stop/tool_calls/length
    `usage.input_tokens`/`output_tokens`   ↔ `ChatUsage.input_tokens`/`output_tokens`

Признак ошибки у результата инструмента в конверте порта отсутствует и уходит
модели текстом в обёртке `<tool_use_error>`; в журнале он хранится как есть.

Ошибки:
RecordError — строка не является записью известного вида или не проходит
    проверку модели.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from enum import StrEnum
from typing import Any, ClassVar, Literal, TypeAlias

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, ValidationError
from pydantic.alias_generators import to_camel

__all__ = [
    "ApiErrorRecord",
    "ApiRequestRecord",
    "AssistantBlock",
    "AssistantMessage",
    "AssistantRecord",
    "AttachmentPayload",
    "AttachmentRecord",
    "AttachmentRole",
    "BlockType",
    "ChainRecord",
    "CompactBoundaryRecord",
    "CompactMetadata",
    "CompactTrigger",
    "KeptNull",
    "MessageOrigin",
    "OriginKind",
    "PermissionBehavior",
    "PermissionMode",
    "PermissionModeRecord",
    "PermissionRule",
    "PermissionRuleOperation",
    "PermissionRuleRecord",
    "PreservedMessages",
    "QueueOperation",
    "QueueOperationRecord",
    "QueueRemoveReason",
    "Record",
    "RecordCodec",
    "RecordError",
    "RecordType",
    "RenderedMessage",
    "ReplyStopReason",
    "ServiceRecord",
    "Synthetic",
    "SystemSubtype",
    "TextBlock",
    "ThinkingBlock",
    "ToolDenialKind",
    "ToolResultBlock",
    "ToolUseBlock",
    "Usage",
    "UserBlock",
    "UserMessage",
    "UserRecord",
]


class RecordError(Exception):
    """Строка журнала не разбирается в запись известного вида."""


class RecordType(StrEnum):
    """Виды записей журнала первой версии."""

    USER = "user"
    ASSISTANT = "assistant"
    ATTACHMENT = "attachment"
    SYSTEM = "system"
    QUEUE_OPERATION = "queue-operation"
    PERMISSION_MODE = "permission-mode"
    PERMISSION_RULE = "permission-rule"
    API_REQUEST = "api-request"

    @property
    def chained(self) -> bool:
        """Запись цепочки: несёт uuid и parentUuid."""
        return self in (
            RecordType.USER,
            RecordType.ASSISTANT,
            RecordType.ATTACHMENT,
            RecordType.SYSTEM,
        )


class SystemSubtype(StrEnum):
    """Подвиды записи system."""

    API_ERROR = "api_error"
    COMPACT_BOUNDARY = "compact_boundary"


class BlockType(StrEnum):
    """Блоки содержимого сообщений; разбор — docs/prompt_assembly/specs/06."""

    THINKING = "thinking"
    TEXT = "text"
    TOOL_USE = "tool_use"
    TOOL_RESULT = "tool_result"


class ReplyStopReason(StrEnum):
    """Причина конца ответа в записи assistant; перевод из finish_reason — в таблице
    докстринга модуля."""

    END_TURN = "end_turn"
    TOOL_USE = "tool_use"
    MAX_TOKENS = "max_tokens"
    STOP_SEQUENCE = "stop_sequence"


class Synthetic(StrEnum):
    """Пометки записей, созданных самим агентом."""

    MODEL = "<synthetic>"


class ToolDenialKind(StrEnum):
    """Почему результат инструмента — отказ, а не итог работы."""

    USER_REJECTED = "user-rejected"
    INTERRUPTED = "interrupted"
    CANCELLED = "cancelled"


class AttachmentRole(StrEnum):
    """Чем вложение уходит модели в режиме отдельных системных сообщений."""

    USER = "user"
    SYSTEM = "system"


class OriginKind(StrEnum):
    """Откуда пришло сообщение записи user."""

    HUMAN = "human"
    TASK_NOTIFICATION = "task-notification"


class CompactTrigger(StrEnum):
    """Что запустило сжатие."""

    AUTO = "auto"
    MANUAL = "manual"


class QueueOperation(StrEnum):
    """Действия над очередью ввода в протоколе журнала."""

    ENQUEUE = "enqueue"
    DEQUEUE = "dequeue"
    REMOVE = "remove"
    POP_ALL = "popAll"
    POP_ONE = "popOne"


class QueueRemoveReason(StrEnum):
    """Причины удаления элемента очереди без обработки."""

    ABSORBED_MID_TURN = "absorbed_mid_turn"
    DELIVERED_AS_TOOL_RESULT = "delivered_as_tool_result"
    DROPPED_BY_HOOK = "dropped_by_hook"
    CLEARED_ON_CANCEL = "cleared_on_cancel"


class PermissionMode(StrEnum):
    """Режимы разрешений"""

    DEFAULT = "default"
    ACCEPT_EDITS = "acceptEdits"
    PLAN = "plan"
    DONT_ASK = "dontAsk"
    BYPASS_PERMISSIONS = "bypassPermissions"
    AUTO = "auto"


class PermissionRuleOperation(StrEnum):
    """Выдача и снятие правила разрешения."""

    ADD = "add"
    REMOVE = "remove"


class PermissionBehavior(StrEnum):
    """Что правило предписывает для инструмента."""

    ALLOW = "allow"


class WireModel(BaseModel):
    """Основа моделей провода журнала: имена полей в JSON — camelCase."""

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        extra="ignore",
        frozen=True,
    )


class MessageModel(BaseModel):
    """Основа моделей сообщения и его блоков: имена полей в JSON — snake_case."""

    model_config = ConfigDict(extra="ignore", frozen=True)


class ThinkingBlock(MessageModel):
    type: Literal["thinking"] = BlockType.THINKING.value
    thinking: str
    signature: str = ""


class TextBlock(MessageModel):
    type: Literal["text"] = BlockType.TEXT.value
    text: str


class ToolUseBlock(MessageModel):
    type: Literal["tool_use"] = BlockType.TOOL_USE.value
    id: str
    name: str
    input: Mapping[str, Any]


class ToolResultBlock(MessageModel):
    type: Literal["tool_result"] = BlockType.TOOL_RESULT.value
    tool_use_id: str
    content: str | Sequence[TextBlock] = ""
    is_error: bool | None = Field(
        default=None,
        description="Только у ошибок: true; у обычного результата поля нет.",
    )

    def text(self) -> str:
        """Текст результата одной строкой."""
        if isinstance(self.content, str):
            return self.content

        parts: list[str] = []
        for block in self.content:
            parts.append(block.text)

        return "\n".join(parts)


AssistantBlock: TypeAlias = ThinkingBlock | TextBlock | ToolUseBlock
UserBlock: TypeAlias = TextBlock | ToolResultBlock


class Usage(MessageModel):
    """Учёт токенов ответа; нули — провайдер учёт не прислал либо запись
    синтетическая."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0

    def is_zero(self) -> bool:
        return self.input_tokens == 0 and self.output_tokens == 0


class UserMessage(MessageModel):
    role: Literal["user"] = "user"
    content: str | Sequence[UserBlock]

    def tool_results(self) -> Sequence[ToolResultBlock]:
        if isinstance(self.content, str):
            return ()

        results: list[ToolResultBlock] = []
        for block in self.content:
            if isinstance(block, ToolResultBlock):
                results.append(block)

        return results


class AssistantMessage(MessageModel):
    """Ответ модели в записи: ровно один блок на запись."""

    id: str
    type: Literal["message"] = "message"
    role: Literal["assistant"] = "assistant"
    model: str
    content: Sequence[AssistantBlock]
    stop_reason: ReplyStopReason
    stop_sequence: None = None
    usage: Usage = Usage()

    def tool_uses(self) -> Sequence[ToolUseBlock]:
        uses: list[ToolUseBlock] = []
        for block in self.content:
            if isinstance(block, ToolUseBlock):
                uses.append(block)

        return uses


class MessageOrigin(WireModel):
    kind: OriginKind


class ChainRecord(WireModel):
    """Общий конверт записей цепочки."""

    type: RecordType
    uuid: str
    parent_uuid: str | None
    timestamp: str
    session_id: str
    is_sidechain: bool = False
    version: str
    seq: int = Field(
        default=0,
        ge=0,
        description="Порядковый номер в сессии с 1; 0 — ещё не выдан сессией.",
    )


class UserRecord(ChainRecord):
    type: Literal[RecordType.USER] = RecordType.USER
    message: UserMessage
    is_meta: bool | None = None
    is_compact_summary: bool | None = None
    is_visible_in_transcript_only: bool | None = None
    tool_use_result: Any = None
    source_tool_assistant_uuid: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "sourceToolAssistantUUID", "source_tool_assistant_uuid"
        ),
        serialization_alias="sourceToolAssistantUUID",
    )
    tool_denial_kind: ToolDenialKind | None = None
    interrupted_message_id: str | None = None
    interrupted_by_shutdown: bool | None = None
    origin: MessageOrigin | None = None


class AssistantRecord(ChainRecord):
    type: Literal[RecordType.ASSISTANT] = RecordType.ASSISTANT
    message: AssistantMessage
    api_block_index: int = 0
    request_id: str
    is_api_error_message: bool | None = None
    error: str | None = None
    is_aborted_mid_stream: bool | None = None
    wire_tool_call_id: str | None = None

    def is_synthetic(self) -> bool:
        return self.message.model == Synthetic.MODEL

    def with_zero_usage(self) -> AssistantRecord:
        """Копия для перенесённого хвоста: учёт обнулён (раздел 5.16 плана)."""
        message = self.message.model_copy(update={"usage": Usage()})

        return self.model_copy(update={"message": message})


class AttachmentPayload(BaseModel):
    """Данные вложения: вид и поля по виду (раздел 5.17 плана)."""

    model_config = ConfigDict(extra="allow", frozen=True)

    type: str


class RenderedMessage(WireModel):
    content: str


class AttachmentRecord(ChainRecord):
    type: Literal[RecordType.ATTACHMENT] = RecordType.ATTACHMENT
    attachment: AttachmentPayload
    rendered: Sequence[RenderedMessage] | None = None
    rendered_role: AttachmentRole | None = None


class ApiErrorRecord(ChainRecord):
    type: Literal[RecordType.SYSTEM] = RecordType.SYSTEM
    subtype: Literal[SystemSubtype.API_ERROR] = SystemSubtype.API_ERROR
    error: str
    retry_attempt: int
    max_retries: int
    level: str = "error"


class PreservedMessages(WireModel):
    anchor_uuid: str
    uuids: Sequence[str]
    all_uuids: Sequence[str]


class CompactMetadata(WireModel):
    trigger: CompactTrigger
    pre_tokens: int
    post_tokens: int
    duration_ms: int
    preserved_messages: PreservedMessages


class CompactBoundaryRecord(ChainRecord):
    type: Literal[RecordType.SYSTEM] = RecordType.SYSTEM
    subtype: Literal[SystemSubtype.COMPACT_BOUNDARY] = SystemSubtype.COMPACT_BOUNDARY
    logical_parent_uuid: str | None
    content: str = "Conversation compacted"
    level: str = "info"
    compact_metadata: CompactMetadata


class ServiceRecord(WireModel):
    """Общий конверт служебных записей: вне цепочки, без uuid."""

    type: RecordType
    seq: int = Field(default=0, ge=0)
    timestamp: str
    session_id: str


class QueueOperationRecord(ServiceRecord):
    type: Literal[RecordType.QUEUE_OPERATION] = RecordType.QUEUE_OPERATION
    operation: QueueOperation
    content: str | None = None
    reason: QueueRemoveReason | None = None
    command_uuid: str | None = None


class PermissionModeRecord(ServiceRecord):
    type: Literal[RecordType.PERMISSION_MODE] = RecordType.PERMISSION_MODE
    permission_mode: PermissionMode


class PermissionRule(WireModel):
    tool: str
    behavior: PermissionBehavior


class PermissionRuleRecord(ServiceRecord):
    type: Literal[RecordType.PERMISSION_RULE] = RecordType.PERMISSION_RULE
    operation: PermissionRuleOperation
    rule: PermissionRule


class ApiRequestRecord(ServiceRecord):
    type: Literal[RecordType.API_REQUEST] = RecordType.API_REQUEST
    id: str
    request_id: str
    params: Mapping[str, Any]
    digest: str


Record: TypeAlias = (
    UserRecord
    | AssistantRecord
    | AttachmentRecord
    | ApiErrorRecord
    | CompactBoundaryRecord
    | QueueOperationRecord
    | PermissionModeRecord
    | PermissionRuleRecord
    | ApiRequestRecord
)


class KeptNull(StrEnum):
    """Ключи, у которых null пишется в строку"""

    PARENT_UUID = "parentUuid"
    LOGICAL_PARENT_UUID = "logicalParentUuid"
    STOP_SEQUENCE = "stop_sequence"

    @classmethod
    def covers(cls, key: str) -> bool:
        values = [member.value for member in cls]

        return key in values


class RecordCodec:
    """Разбор строки журнала в запись и запись записи в строку.

    Вид определяется полем type, у system — ещё и subtype. Поле со значением
    None в строку не попадает, кроме ключей KeptNull,
    так читает prompt_assembly.
    """

    TYPE: ClassVar[str] = "type"
    SUBTYPE: ClassVar[str] = "subtype"

    def __init__(self) -> None:
        self._models: dict[RecordType, type[Record]] = {
            RecordType.USER: UserRecord,
            RecordType.ASSISTANT: AssistantRecord,
            RecordType.ATTACHMENT: AttachmentRecord,
            RecordType.QUEUE_OPERATION: QueueOperationRecord,
            RecordType.PERMISSION_MODE: PermissionModeRecord,
            RecordType.PERMISSION_RULE: PermissionRuleRecord,
            RecordType.API_REQUEST: ApiRequestRecord,
        }
        self._system: dict[SystemSubtype, type[Record]] = {
            SystemSubtype.API_ERROR: ApiErrorRecord,
            SystemSubtype.COMPACT_BOUNDARY: CompactBoundaryRecord,
        }

    def parse(self, raw: Mapping[str, Any]) -> Record:
        model = self._model_of(raw)
        try:
            return model.model_validate(raw)
        except ValidationError as exc:
            msg = (
                f"This '{raw.get(self.TYPE)}' record does not have the shape of "
                f"{model.__name__}: {exc}"
            )
            raise RecordError(msg) from exc

    def line(self, record: Record) -> str:
        """Строка журнала без перевода строки."""
        document = record.model_dump(by_alias=True, mode="json")

        return json.dumps(self._without_none(document), ensure_ascii=False)

    def _without_none(self, document: Mapping[str, Any]) -> dict[str, Any]:
        """Снимает ключи с None на всех уровнях, кроме тех, что пишет как null."""
        stripped: dict[str, Any] = {}
        for key, value in document.items():
            if value is None and not KeptNull.covers(key):
                continue

            stripped[key] = self._nested(value)

        return stripped

    def _nested(self, value: Any) -> Any:
        if isinstance(value, Mapping):
            return self._without_none(value)

        if isinstance(value, list):
            items: list[Any] = []
            for item in value:
                items.append(self._nested(item))

            return items

        return value

    def _model_of(self, raw: Mapping[str, Any]) -> type[Record]:
        kind = raw.get(self.TYPE)
        try:
            record_type = RecordType(kind)
        except ValueError as exc:
            known = [member.value for member in RecordType]
            msg = (
                f"This record has type '{kind}', which this agent does not know. "
                f"Known types are {known}."
            )
            raise RecordError(msg) from exc

        if record_type is not RecordType.SYSTEM:
            return self._models[record_type]

        subtype = raw.get(self.SUBTYPE)
        try:
            system_subtype = SystemSubtype(subtype)
        except ValueError as exc:
            known_subtypes = [member.value for member in SystemSubtype]
            msg = (
                f"This system record has subtype '{subtype}', which this agent does "
                f"not know. Known subtypes are {known_subtypes}."
            )
            raise RecordError(msg) from exc

        return self._system[system_subtype]
