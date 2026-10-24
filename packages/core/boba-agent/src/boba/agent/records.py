"""Записи истории агента: модели строк журнала и их кодек.

Формат — журнал (разбор в docs/prompt_assembly):
записи цепочки `user`, `assistant`, `attachment`, `system`
связаны `uuid` → `parentUuid`, служебные записи цепочки не имеют.
Поля `seq` и `kind` — наши: порядковый номер записи в сессии (его выдаёт
сессия при добавлении) и вид записи. Вид называет класс модели: по нему
кодек выбирает модель, и смысл записи (вопрос, результат инструмента,
маркер остановки, пересказ сжатия…) больше нигде не угадывается по флагам
и тексту. Поля эталона, которые различают виды (`isMeta`,
`isCompactSummary`, `origin`), класс вида пишет сам
постоянными значениями. Запись читается в модель один раз, в загрузчике,
дальше по коду ходит модель; неизвестное поле известного вида
игнорируется, чтобы старый агент читал журнал нового.

Содержимое сообщений хранится блоками, а модель получает его в конверте
порта `ChatTurn`/`ChatReply`; перевод взаимно однозначен и живёт в сборщике
запроса и записи ответа (план, раздел 5.12):

    блок `thinking`                        ↔ `reasoning`
    блок `text`                            ↔ `content`
    блок `tool_use` {id, name, input}      ↔ `ToolCall(id, name, arguments)`
    блок `tool_result` в записи `user`     ↔ сообщение роли `tool` с `tool_call_id`
    блок `tool_reference` в `tool_result`  ↔ `tool_references` и строка текста
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
from abc import abstractmethod
from collections.abc import Mapping, Sequence
from enum import StrEnum
from typing import Annotated, Any, ClassVar, Literal, Self, TypeAlias

import mcp_types as mt
from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    ValidationError,
)
from pydantic.alias_generators import to_camel

from boba.llm.chat import LlmTrouble

__all__ = [
    "ApiErrorRecord",
    "ApiRequestRecord",
    "AssistantBlock",
    "AssistantMessage",
    "AssistantRecord",
    "AttachmentData",
    "AttachmentKind",
    "AttachmentRecord",
    "AttachmentRole",
    "BlockType",
    "CallStatus",
    "ChainRecord",
    "CommandOutputRecord",
    "CommandRecord",
    "CompactBoundaryRecord",
    "CompactMetadata",
    "CompactSummaryRecord",
    "CompactTrigger",
    "CompactionPhase",
    "CompactionRecord",
    "DateAttachment",
    "DeferredToolsDeltaAttachment",
    "DeferredToolsRecordAttachment",
    "EnvironmentAttachment",
    "EnvironmentChange",
    "EnvironmentField",
    "EnvironmentSnapshot",
    "HarnessCommandRecord",
    "InformationalRecord",
    "InterruptRecord",
    "InvokedSkill",
    "InvokedSkillsAttachment",
    "KeptNull",
    "LinkText",
    "MaxTurnsAttachment",
    "McpInstructionsAttachment",
    "MessageOrigin",
    "MetaRecord",
    "ModelAttachment",
    "ModelIdentity",
    "NoResponseRecord",
    "NoticeLevel",
    "OriginKind",
    "PayloadModel",
    "PermissionBehavior",
    "PermissionMode",
    "PermissionModeRecord",
    "PermissionRule",
    "PermissionRuleOperation",
    "PermissionRuleRecord",
    "PreservedMessages",
    "PromptRecord",
    "PromptSnapshot",
    "QueueOperation",
    "QueueOperationRecord",
    "QueueRemoveReason",
    "QueuedCommandAttachment",
    "Record",
    "RecordCodec",
    "RecordError",
    "RecordKind",
    "RecordType",
    "RenderedMessage",
    "ReplyRecord",
    "ReplyStopReason",
    "ServiceRecord",
    "SkillListingAttachment",
    "SnapshotSkill",
    "SnapshotTool",
    "Synthetic",
    "SystemSubtype",
    "TaskNotificationRecord",
    "TextBlock",
    "ThinkingBlock",
    "ToolDenialKind",
    "ToolReferenceBlock",
    "ToolResultBlock",
    "ToolResultRecord",
    "ToolUseBlock",
    "TurnEndRecord",
    "TurnOutcome",
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
    COMPACTION = "compaction"
    INFORMATIONAL = "informational"
    HARNESS_COMMAND = "harness_command"
    TURN_END = "turn_end"


class RecordKind(StrEnum):
    """Вид записи — поле kind: один класс модели на вид."""

    PROMPT = "prompt"
    TASK_NOTIFICATION = "task_notification"
    META = "meta"
    COMMAND = "command"
    COMMAND_OUTPUT = "command_output"
    TOOL_RESULT = "tool_result"
    INTERRUPT = "interrupt"
    COMPACT_SUMMARY = "compact_summary"
    REPLY = "reply"
    NO_RESPONSE = "no_response"
    ATTACHMENT = "attachment"
    API_ERROR = "api_error"
    INFORMATIONAL = "informational"
    COMPACTION = "compaction"
    COMPACT_BOUNDARY = "compact_boundary"
    HARNESS_COMMAND = "harness_command"
    TURN_END = "turn_end"
    QUEUE_OPERATION = "queue_operation"
    PERMISSION_MODE = "permission_mode"
    PERMISSION_RULE = "permission_rule"
    API_REQUEST = "api_request"


class AttachmentKind(StrEnum):
    """Виды вложений: поле type данных вложения."""

    PROMPT_SNAPSHOT = "prompt_snapshot"
    MODEL = "model"
    DATE = "date"
    ENVIRONMENT = "environment"
    DEFERRED_TOOLS_DELTA = "deferred_tools_delta"
    DEFERRED_TOOLS_RECORD = "deferred_tools_record"
    MCP_INSTRUCTIONS_DELTA = "mcp_instructions_delta"
    SKILL_LISTING = "skill_listing"
    INVOKED_SKILLS = "invoked_skills"
    QUEUED_COMMAND = "queued_command"
    MAX_TURNS_REACHED = "max_turns_reached"


class NoticeLevel(StrEnum):
    """Важность записи system для человека (поле level)."""

    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class BlockType(StrEnum):
    """Блоки содержимого сообщений; разбор — docs/prompt_assembly/specs/06."""

    THINKING = "thinking"
    TEXT = "text"
    TOOL_USE = "tool_use"
    TOOL_RESULT = "tool_result"
    TOOL_REFERENCE = "tool_reference"


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


class CallStatus(StrEnum):
    """Исход вызова инструмента в записи результата: выполнен, сорвался
    (ошибка тела, вызов оборван выключением агента) или отменён без ответа
    сервера (отказ в разрешении, остановка хода)."""

    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TurnOutcome(StrEnum):
    """Исходы хода — закрытый перечень раздела 5.13 плана; INTERRUPTED —
    ход оборван гибелью агента, конец ему пишет загрузчик при починке."""

    COMPLETED = "completed"
    ABORTED_STREAMING = "aborted_streaming"
    ABORTED_TOOLS = "aborted_tools"
    API_ERROR = "api_error"
    BLOCKING_LIMIT = "blocking_limit"
    RAPID_REFILL_BREAKER = "rapid_refill_breaker"
    COMPACT_FAILURE_BREAKER = "compact_failure_breaker"
    MAX_TURNS = "max_turns"
    OUTPUT_LIMIT = "output_limit"
    SHUTDOWN = "shutdown"
    INTERRUPTED = "interrupted"

    @property
    def refused(self) -> bool:
        """Ход кончился отказом агента ответить: отказ модели, предел длины
        ответа, запрос больше окна, срабатывание предохранителей сжатия. Такой
        конец отвечает на вопрос хода: модели заглушка ответа не нужна."""
        return self in (
            TurnOutcome.API_ERROR,
            TurnOutcome.OUTPUT_LIMIT,
            TurnOutcome.BLOCKING_LIMIT,
            TurnOutcome.RAPID_REFILL_BREAKER,
            TurnOutcome.COMPACT_FAILURE_BREAKER,
        )


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


class ToolReferenceBlock(MessageModel):
    """Ссылка на инструмент внутри результата: результат ToolSearch называет
    так отложенные инструменты, которые он загрузил."""

    type: Literal["tool_reference"] = BlockType.TOOL_REFERENCE.value
    tool_name: str


class ToolResultBlock(MessageModel):
    type: Literal["tool_result"] = BlockType.TOOL_RESULT.value
    tool_use_id: str
    content: str | Sequence[TextBlock | ToolReferenceBlock] = ""
    is_error: bool | None = Field(
        default=None,
        description="Только у ошибок: true; у обычного результата поля нет.",
    )

    def text(self) -> str:
        """Текст результата одной строкой; ссылки на инструменты в него не входят."""
        if isinstance(self.content, str):
            return self.content

        parts: list[str] = []
        for block in self.content:
            if isinstance(block, TextBlock):
                parts.append(block.text)

        return "\n".join(parts)

    def references(self) -> Sequence[str]:
        """Имена инструментов из ссылок результата по порядку."""
        if isinstance(self.content, str):
            return ()

        names: list[str] = []
        for block in self.content:
            if isinstance(block, ToolReferenceBlock):
                names.append(block.tool_name)

        return names


AssistantBlock: TypeAlias = ThinkingBlock | TextBlock | ToolUseBlock
UserBlock: TypeAlias = TextBlock | ToolResultBlock | mt.ResourceLink
"""Блоки записи `user`; ссылка на вложение вопроса — стандартный блок MCP
resource_link и пишется в журнал его формой."""


class LinkText(StrEnum):
    """Текст ссылки на вложение вопроса, каким её видит модель."""

    TEMPLATE = "[@{name}]({uri})"

    def render(self, link: mt.ResourceLink) -> str:
        return self.format(name=link.name, uri=link.uri)


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

    def text(self) -> str:
        """Текст сообщения: строка как есть, у блоков — тексты подряд."""
        if isinstance(self.content, str):
            return self.content

        parts: list[str] = []
        for block in self.content:
            if isinstance(block, TextBlock):
                parts.append(block.text)

        return "".join(parts)

    def tool_results(self) -> Sequence[ToolResultBlock]:
        if isinstance(self.content, str):
            return ()

        results: list[ToolResultBlock] = []
        for block in self.content:
            if isinstance(block, ToolResultBlock):
                results.append(block)

        return results

    def links(self) -> Sequence[mt.ResourceLink]:
        """Ссылки на вложения вопроса по порядку."""
        if isinstance(self.content, str):
            return ()

        found: list[mt.ResourceLink] = []
        for block in self.content:
            if isinstance(block, mt.ResourceLink):
                found.append(block)

        return found


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


class PayloadModel(BaseModel):
    """Основа данных вложения: имена полей на проводе — camelCase."""

    model_config = ConfigDict(
        alias_generator=to_camel, populate_by_name=True, frozen=True, extra="ignore"
    )


class SnapshotTool(PayloadModel):
    name: str
    description: str
    schema_: dict[str, Any] = Field(default_factory=dict, alias="schema")

    def text(self) -> str:
        """Текст описания для оценки места в окне."""
        return f"{self.name}: {self.description}"


class SnapshotSkill(PayloadModel):
    name: str
    description: str


class PromptSnapshot(PayloadModel):
    """Снимок: системный промпт секциями, инструменты, скиллы, модель, режим."""

    type: Literal[AttachmentKind.PROMPT_SNAPSHOT] = AttachmentKind.PROMPT_SNAPSHOT
    system_prompt: Sequence[str]
    tools: Sequence[SnapshotTool] = ()
    skills: Sequence[SnapshotSkill] = ()
    model: str
    system_turns: bool


class ModelIdentity(PayloadModel):
    model_id: str


class ModelAttachment(PayloadModel):
    type: Literal[AttachmentKind.MODEL] = AttachmentKind.MODEL
    identity: ModelIdentity
    text: str


class DateAttachment(PayloadModel):
    type: Literal[AttachmentKind.DATE] = AttachmentKind.DATE
    date: str
    changed: bool


class QueuedCommandAttachment(PayloadModel):
    """Сообщение, подмешанное посреди хода из очереди (раздел 5.7, путь 2)."""

    type: Literal[AttachmentKind.QUEUED_COMMAND] = AttachmentKind.QUEUED_COMMAND
    prompt: str
    source_uuid: str
    origin: OriginKind
    is_meta: bool


class MaxTurnsAttachment(PayloadModel):
    """Предел шагов хода достигнут; модели не отправляется."""

    type: Literal[AttachmentKind.MAX_TURNS_REACHED] = AttachmentKind.MAX_TURNS_REACHED
    max_turns: int
    turn_count: int


class EnvironmentSnapshot(PayloadModel):
    """Окружение процесса, которое объявляется модели; собирает вход процесса."""

    working_directory: str
    platform: str
    shell: str
    os_version: str

    def same_host(self, other: EnvironmentSnapshot) -> bool:
        """Платформа, оболочка и ОС не сменились: разницу можно описать полями."""
        if self.platform != other.platform:
            return False

        if self.shell != other.shell:
            return False

        return self.os_version == other.os_version


class EnvironmentField(StrEnum):
    """Поля окружения, у которых бывает разница."""

    WORKING_DIRECTORY = "workingDirectory"


class EnvironmentChange(PayloadModel):
    field: EnvironmentField
    previous: str = Field(
        validation_alias=AliasChoices("from", "previous"), serialization_alias="from"
    )


class EnvironmentAttachment(PayloadModel):
    """Окружение: полный блок при первом появлении, разница полей — потом."""

    type: Literal[AttachmentKind.ENVIRONMENT] = AttachmentKind.ENVIRONMENT
    snapshot: EnvironmentSnapshot
    changes: Sequence[EnvironmentChange] | None = None


class DeferredToolsDeltaAttachment(PayloadModel):
    """Изменение набора инструментов относительно объявленного, одними
    именами: added — отложенные, которые стали доступны через ToolSearch
    (readded среди них — те, что разговор уже объявлял и потом снимал: их
    строк в added_lines нет); surfaced — инструменты с полным определением,
    появившиеся посреди сессии; removed — снятые. tool_search_absent —
    отложенных не осталось, ToolSearch в запросе нет."""

    type: Literal[AttachmentKind.DEFERRED_TOOLS_DELTA] = (
        AttachmentKind.DEFERRED_TOOLS_DELTA
    )
    added_names: Sequence[str] = ()
    added_lines: Sequence[str] = ()
    removed_names: Sequence[str] = ()
    readded_names: Sequence[str] = ()
    surfaced_names: Sequence[str] = ()
    tool_search_absent: bool = False


class DeferredToolsRecordAttachment(PayloadModel):
    """Определения загруженных отложенных инструментов, которые впервые ушли
    в запрос; модели не отправляется."""

    type: Literal[AttachmentKind.DEFERRED_TOOLS_RECORD] = (
        AttachmentKind.DEFERRED_TOOLS_RECORD
    )
    entries: Sequence[SnapshotTool]


class McpInstructionsAttachment(PayloadModel):
    """Изменение инструкций серверов MCP относительно объявленного: блоки
    `## <сервер>` появившихся серверов и серверов с новым текстом, имена
    отключившихся (docs/prompt_assembly, mcp_instructions_delta)."""

    type: Literal[AttachmentKind.MCP_INSTRUCTIONS_DELTA] = (
        AttachmentKind.MCP_INSTRUCTIONS_DELTA
    )
    added_names: Sequence[str] = ()
    added_blocks: Sequence[str] = ()
    added_server_instructions: Sequence[str] = ()
    removed_names: Sequence[str] = ()


class SkillListingAttachment(PayloadModel):
    """Список скиллов: первый раз все, дальше только новые имена."""

    type: Literal[AttachmentKind.SKILL_LISTING] = AttachmentKind.SKILL_LISTING
    content: str
    skill_count: int
    is_initial: bool
    names: Sequence[str]


class InvokedSkill(PayloadModel):
    """Скилл, вызванный до сжатия, и текст, который модель тогда получила."""

    name: str
    content: str


class InvokedSkillsAttachment(PayloadModel):
    """Скиллы, вызванные в пересказанной части разговора, — заново после
    границы сжатия, новые первыми (docs/prompt_assembly, invoked_skills)."""

    type: Literal[AttachmentKind.INVOKED_SKILLS] = AttachmentKind.INVOKED_SKILLS
    skills: Sequence[InvokedSkill]


AttachmentData: TypeAlias = Annotated[
    PromptSnapshot
    | ModelAttachment
    | DateAttachment
    | QueuedCommandAttachment
    | MaxTurnsAttachment
    | EnvironmentAttachment
    | DeferredToolsDeltaAttachment
    | DeferredToolsRecordAttachment
    | McpInstructionsAttachment
    | SkillListingAttachment
    | InvokedSkillsAttachment,
    Field(discriminator="type"),
]
"""Данные вложения: модель выбирается по полю type."""


class ChainRecord(WireModel):
    """Общий конверт записей цепочки: вид записи `kind` и связь
    `uuid` → `parentUuid`."""

    type: RecordType
    kind: RecordKind
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
    """Основа записей `user`: сообщение роли user. Наследники — виды:
    вопрос человека, уведомление задачи, служебный текст, вызов и вывод
    локальной команды, результат инструмента, маркер остановки, пересказ
    сжатия."""

    type: Literal[RecordType.USER] = RecordType.USER
    message: UserMessage

    @abstractmethod
    def human_text(self) -> str | None:
        """Текст, который человек отправил сам: вопрос или вызов команды;
        None — запись не от человека."""


class PromptRecord(UserRecord):
    """Вопрос человека; uuid — идентификатор элемента очереди."""

    kind: Literal[RecordKind.PROMPT] = RecordKind.PROMPT
    origin: MessageOrigin = MessageOrigin(kind=OriginKind.HUMAN)

    def human_text(self) -> str | None:
        return self.message.text()


class TaskNotificationRecord(UserRecord):
    """Уведомление о задаче, пришедшее очередью: модель видит его как
    сообщение пользователя, человек — нет."""

    kind: Literal[RecordKind.TASK_NOTIFICATION] = RecordKind.TASK_NOTIFICATION
    is_meta: Literal[True] = True
    origin: MessageOrigin = MessageOrigin(kind=OriginKind.TASK_NOTIFICATION)

    def human_text(self) -> str | None:
        return None


class MetaRecord(UserRecord):
    """Служебный текст для модели от имени пользователя: контекст элемента
    очереди, просьба продолжить после предела ответа, пометка локальной
    команды."""

    kind: Literal[RecordKind.META] = RecordKind.META
    is_meta: Literal[True] = True

    def human_text(self) -> str | None:
        return None


class CommandRecord(UserRecord):
    """Вызов локальной команды пользователя (`/compact <указания>`) тегами
    command-name, command-message, command-args; uuid — идентификатор
    элемента очереди команды."""

    kind: Literal[RecordKind.COMMAND] = RecordKind.COMMAND
    origin: MessageOrigin = MessageOrigin(kind=OriginKind.HUMAN)

    def human_text(self) -> str | None:
        return self.message.text()


class CommandOutputRecord(UserRecord):
    """Вывод локальной команды тегом local-command-stdout: им кончается ход
    команды."""

    kind: Literal[RecordKind.COMMAND_OUTPUT] = RecordKind.COMMAND_OUTPUT

    def human_text(self) -> str | None:
        return None


class ToolResultRecord(UserRecord):
    """Результат вызова инструмента: блок tool_result, ответ сервера как он
    пришёл (`toolUseResult`) и исход вызова `status`."""

    kind: Literal[RecordKind.TOOL_RESULT] = RecordKind.TOOL_RESULT
    tool_use_result: Any = None
    source_tool_assistant_uuid: str = Field(
        validation_alias=AliasChoices(
            "sourceToolAssistantUUID", "source_tool_assistant_uuid"
        ),
        serialization_alias="sourceToolAssistantUUID",
    )
    tool_denial_kind: ToolDenialKind | None = None
    status: CallStatus

    def human_text(self) -> str | None:
        return None


class InterruptRecord(UserRecord):
    """Маркер остановки хода пользователем или выключением агента: текст
    по эталону уходит модели, ход им кончается."""

    kind: Literal[RecordKind.INTERRUPT] = RecordKind.INTERRUPT
    interrupted_by_shutdown: bool | None = None

    def human_text(self) -> str | None:
        return None


class CompactSummaryRecord(UserRecord):
    """Пересказ сжатия: с него начинается разговор после полной границы;
    uuid — идентификатор сжатия."""

    kind: Literal[RecordKind.COMPACT_SUMMARY] = RecordKind.COMPACT_SUMMARY
    is_compact_summary: Literal[True] = True
    is_visible_in_transcript_only: Literal[True] = True

    def human_text(self) -> str | None:
        return None


class AssistantRecord(ChainRecord):
    """Основа записей `assistant`: один блок ответа на запись. Наследники —
    ответ модели и заглушка «ответа нет»."""

    type: Literal[RecordType.ASSISTANT] = RecordType.ASSISTANT
    message: AssistantMessage
    api_block_index: int = 0
    request_id: str

    def is_synthetic(self) -> bool:
        return self.message.model == Synthetic.MODEL

    def with_zero_usage(self) -> Self:
        """Копия для перенесённого хвоста: учёт обнулён (раздел 5.16 плана)."""
        message = self.message.model_copy(update={"usage": Usage()})

        return self.model_copy(update={"message": message})


class ReplyRecord(AssistantRecord):
    """Блок ответа модели; `is_aborted_mid_stream` — ответ оборван
    остановкой посреди потока, `wire_tool_call_id` — идентификатор вызова
    у провайдера."""

    kind: Literal[RecordKind.REPLY] = RecordKind.REPLY
    is_aborted_mid_stream: bool | None = None
    wire_tool_call_id: str | None = None


class NoResponseRecord(AssistantRecord):
    """Синтетический ответ «No response requested.» на разговор, который
    кончился вопросом без ответа: дописывает загрузчик при починке."""

    kind: Literal[RecordKind.NO_RESPONSE] = RecordKind.NO_RESPONSE


class RenderedMessage(WireModel):
    content: str


class AttachmentRecord(ChainRecord):
    type: Literal[RecordType.ATTACHMENT] = RecordType.ATTACHMENT
    kind: Literal[RecordKind.ATTACHMENT] = RecordKind.ATTACHMENT
    attachment: AttachmentData
    rendered: Sequence[RenderedMessage] | None = None
    rendered_role: AttachmentRole | None = None


class ApiErrorRecord(ChainRecord):
    """Неудачная попытка запроса к модели, за которой следует повтор через
    retry_in_ms."""

    type: Literal[RecordType.SYSTEM] = RecordType.SYSTEM
    kind: Literal[RecordKind.API_ERROR] = RecordKind.API_ERROR
    subtype: Literal[SystemSubtype.API_ERROR] = SystemSubtype.API_ERROR
    error: str
    retry_in_ms: int = Field(ge=0)
    retry_attempt: int
    max_retries: int
    level: NoticeLevel = NoticeLevel.ERROR
    trouble: LlmTrouble
    """Вид беды для текста человеку."""

    detail: str
    """Короткая техническая причина: статус и суть ответа провайдера."""


class InformationalRecord(ChainRecord):
    """Пометка для человека в беседе: сервер MCP пропал или вернулся, ход
    оборван выключением агента. Модели не отправляется; клиент показывает
    её в ходе, где она записана."""

    type: Literal[RecordType.SYSTEM] = RecordType.SYSTEM
    kind: Literal[RecordKind.INFORMATIONAL] = RecordKind.INFORMATIONAL
    subtype: Literal[SystemSubtype.INFORMATIONAL] = SystemSubtype.INFORMATIONAL
    content: str = Field(min_length=1)
    level: NoticeLevel
    detail: str
    """Короткая техническая причина под текстом; пусто — причины нет."""


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
    pre_compact_discovered_tools: Sequence[str] = Field(
        default_factory=list,
        description=(
            "Отложенные инструменты, загруженные до сжатия, в порядке загрузки: "
            "ссылки на них уходят вместе с пересказанной частью."
        ),
    )


class CompactionPhase(StrEnum):
    """Что записано о сжатии записью compaction: начато, не удалось, прервано.
    Состоявшееся сжатие — граница и пересказ с uuid, равным id сжатия."""

    STARTED = "started"
    FAILED = "failed"
    CANCELLED = "cancelled"


class CompactionRecord(ChainRecord):
    """Ход сжатия в истории: из этих записей лента рисует сжатие вживую и при
    воспроизведении. Модели не уходит. `command_uuid` — запись вызова
    `/compact` у сжатия по команде; у сжатия по порогу её нет. `error` —
    причина у неудачи, у остальных фаз пусто."""

    type: Literal[RecordType.SYSTEM] = RecordType.SYSTEM
    kind: Literal[RecordKind.COMPACTION] = RecordKind.COMPACTION
    subtype: Literal[SystemSubtype.COMPACTION] = SystemSubtype.COMPACTION
    compaction_id: str = Field(min_length=1)
    phase: CompactionPhase
    trigger: CompactTrigger
    command_uuid: str | None
    error: str


class CompactBoundaryRecord(ChainRecord):
    type: Literal[RecordType.SYSTEM] = RecordType.SYSTEM
    kind: Literal[RecordKind.COMPACT_BOUNDARY] = RecordKind.COMPACT_BOUNDARY
    subtype: Literal[SystemSubtype.COMPACT_BOUNDARY] = SystemSubtype.COMPACT_BOUNDARY
    logical_parent_uuid: str | None
    content: str = "Conversation compacted"
    level: NoticeLevel = NoticeLevel.INFO
    compact_metadata: CompactMetadata


class HarnessCommandRecord(ChainRecord):
    """Команда запускающей программы или окно сервера MCP, которые клиент
    записал в историю сессии: человек видит её в беседе, модели она не
    уходит. `name` — имя команды, `text` — что человек набрал."""

    type: Literal[RecordType.SYSTEM] = RecordType.SYSTEM
    kind: Literal[RecordKind.HARNESS_COMMAND] = RecordKind.HARNESS_COMMAND
    subtype: Literal[SystemSubtype.HARNESS_COMMAND] = SystemSubtype.HARNESS_COMMAND
    name: str = Field(min_length=1)
    text: str


class TurnEndRecord(ChainRecord):
    """Конец хода: последняя запись каждого хода, её пишет цикл хода при
    любом исходе, а оборванному ходу — загрузчик при починке. По ней и
    живой ход, и воспроизведение кончают ход одинаково. `notice` — слова
    человеку о причине конца (отказ модели, полное окно, оборванный ход),
    пусто — пометки нет; `level` — её важность; `detail` — короткая
    техническая причина под ней; `error` — техническая причина целиком
    (ответ провайдера) для журнала. Модели не уходит."""

    type: Literal[RecordType.SYSTEM] = RecordType.SYSTEM
    kind: Literal[RecordKind.TURN_END] = RecordKind.TURN_END
    subtype: Literal[SystemSubtype.TURN_END] = SystemSubtype.TURN_END
    outcome: TurnOutcome
    notice: str
    level: NoticeLevel
    detail: str
    error: str
    usage: Usage
    num_turns: int = Field(ge=0)
    duration_ms: int = Field(ge=0)


class ServiceRecord(WireModel):
    """Общий конверт служебных записей: вне цепочки, без uuid."""

    type: RecordType
    kind: RecordKind
    seq: int = Field(default=0, ge=0)
    timestamp: str
    session_id: str


class QueueOperationRecord(ServiceRecord):
    type: Literal[RecordType.QUEUE_OPERATION] = RecordType.QUEUE_OPERATION
    kind: Literal[RecordKind.QUEUE_OPERATION] = RecordKind.QUEUE_OPERATION
    operation: QueueOperation
    content: str | None = None
    reason: QueueRemoveReason | None = None
    command_uuid: str | None = None


class PermissionModeRecord(ServiceRecord):
    type: Literal[RecordType.PERMISSION_MODE] = RecordType.PERMISSION_MODE
    kind: Literal[RecordKind.PERMISSION_MODE] = RecordKind.PERMISSION_MODE
    permission_mode: PermissionMode


class PermissionRule(WireModel):
    tool: str
    behavior: PermissionBehavior


class PermissionRuleRecord(ServiceRecord):
    type: Literal[RecordType.PERMISSION_RULE] = RecordType.PERMISSION_RULE
    kind: Literal[RecordKind.PERMISSION_RULE] = RecordKind.PERMISSION_RULE
    operation: PermissionRuleOperation
    rule: PermissionRule


class ApiRequestRecord(ServiceRecord):
    type: Literal[RecordType.API_REQUEST] = RecordType.API_REQUEST
    kind: Literal[RecordKind.API_REQUEST] = RecordKind.API_REQUEST
    id: str
    request_id: str
    params: Mapping[str, Any]
    digest: str


Record: TypeAlias = Annotated[
    PromptRecord
    | TaskNotificationRecord
    | MetaRecord
    | CommandRecord
    | CommandOutputRecord
    | ToolResultRecord
    | InterruptRecord
    | CompactSummaryRecord
    | ReplyRecord
    | NoResponseRecord
    | AttachmentRecord
    | ApiErrorRecord
    | InformationalRecord
    | CompactionRecord
    | CompactBoundaryRecord
    | HarnessCommandRecord
    | TurnEndRecord
    | QueueOperationRecord
    | PermissionModeRecord
    | PermissionRuleRecord
    | ApiRequestRecord,
    Field(discriminator="kind"),
]
"""Запись журнала: модель выбирается по полю kind."""


class KeptNull(StrEnum):
    """Ключи, у которых null пишется в строку"""

    PARENT_UUID = "parentUuid"
    LOGICAL_PARENT_UUID = "logicalParentUuid"
    STOP_SEQUENCE = "stop_sequence"
    COMMAND_UUID = "commandUuid"

    @classmethod
    def covers(cls, key: str) -> bool:
        values = [member.value for member in cls]

        return key in values


class RecordCodec:
    """Разбор строки журнала в запись и запись записи в строку.

    Вид записи — поле kind: по нему выбирается модель. Поле со значением
    None в строку не попадает, кроме ключей KeptNull, так читает
    prompt_assembly.
    """

    KIND: ClassVar[str] = "kind"

    def __init__(self) -> None:
        self._records: TypeAdapter[Record] = TypeAdapter(Record)

    def parse(self, raw: Mapping[str, Any]) -> Record:
        kind = raw.get(self.KIND)
        try:
            RecordKind(kind)
        except ValueError as exc:
            known = [member.value for member in RecordKind]
            msg = (
                f"This record has kind '{kind}', which this agent does not know. "
                f"Known kinds are {known}."
            )
            raise RecordError(msg) from exc

        try:
            return self._records.validate_python(raw)
        except ValidationError as exc:
            msg = f"This '{kind}' record does not have the shape of its kind: {exc}"
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
