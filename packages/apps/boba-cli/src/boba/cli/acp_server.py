"""Вход ACP v2: агент `boba --stdio` над ядром boba-agent на SDK acp.experimental.v2.

Сервер JSON-RPC по stdio на стороне агента AgentSide (boba-transport-acp):
`initialize` с `capabilities.session.mcp.acp`, `session/new`,
`session/prompt`, `session/cancel`, запрос `session/request_permission` к
клиенту и форма `elicitation/create` на вопрос AskUserQuestion, если клиент
объявил `elicitation.form` (без форм инструмента вопроса у модели нет);
история — `session/resume` (с `replayFrom: start` беседа воспроизводится
уведомлениями до ответа), `session/list` страницами с курсором,
`session/close`, `session/delete`, `session/fork`. Сессию открывает
сборка (SessionAssembly): агент ядра над инструментами серверов, которые
клиент объявил записями `type: "acp"`; с серверами агент говорит запросами
MCP внутри `mcp/message` (AcpToolServer, SessionMcp): server/discover —
возможности и инструкции серверов (инструкции модель получает вложением
хода), инструменты, ресурсы — его инструментами list_mcp_resources и
read_mcp_resource, промпт — по вопросу `/<команда> <аргументы>` с полным
именем промпта: команды промптов клиенту объявляет не агент, а сами
серверы. Свои команды сессии агент объявляет `available_commands_update`
после ответа на `session/new|resume|fork` (SessionCommands). Описание и
списки живут по сроку свежести ответа (ttlMs) и перечитываются в начале
хода, а промпт, которого вопрос-команда не нашёл, — сразу; смена списков по потоку
subscriptions/listen делает их устаревшими сразу. События ядра приёмник
SessionRelay переводит в уведомления по таблице AcpMapping.

Ход по v2: ответ на `session/prompt` — `messageId` вставленного вопроса (он же
uuid записи `user`), ход описывают `state_update`: `running` на первом
событии хода и `idle` со `stopReason` в конце. Сбой после вставки вопроса
кончает ход `idle` с `stopReason: "error"`, а не ошибкой ответа. Второй
`prompt` в идущем ходе встаёт в очередь ядра и получает ответ, когда его
собственный ход вставил запись `user`; вопросы очереди не склеиваются.
`session/cancel` прерывает ход и очищает очередь, снятые вопросы отвечают
-32800, как и при `session/close` до вставки.

Сессию, которую держит другой процесс, `resume` открывает наблюдателем
(ObserverSession): историю он отдаёт, а `prompt` отвечает -32603 «held by
another process», пока замок занят; когда держатель вышел, следующий
`prompt` открывает сессию по-настоящему.

Настройки сессии — `configOptions` `mode` (режимы разрешений ядра) и `model`
(профили `models.<имя>` настроек) в ответах `session/new|resume|fork`;
`session/set_config_option` меняет их, после смены уходит
`config_option_update`. После хода и сжатия уходит `usage_update`; сжатие
идёт `compaction_update` по стадиям (in_progress, затем completed с
пересказом, failed с причиной или cancelled), воспроизведение отдаёт
состоявшиеся сжатия так же — по записи пересказа; пометка агента посреди
хода (тред не помещается в окно) — `notice`. После первого хода и при
воспроизведении —
`session_info_update` с заголовком, после каждого сброса записей в
хранилище истории — `session_info_update` с `updatedAt` последней
записи; ошибка вне хода уходит `notice`. Точка
среза `session/fork` — расширение `_meta["boba/upToMessageId"]`.

Ошибки:
RequestError — ошибка JSON-RPC клиенту: параметры запроса (-32602),
    неизвестная сессия (-32002), сборка, хранилище, серверы MCP или сессия у
    другого процесса (-32603).
SessionRefusedError — сборка сессии отвергла параметры запроса; поднимает
    реализация SessionAssembly, вход переводит в -32602.
SessionOpenError — сессию открыть не удалось: хранилище, серверы MCP,
    профиль; поднимает реализация SessionAssembly и AcpToolServer, вход
    переводит в -32603.
SessionBusyError — модель меняют при идущем ходе; поднимает реализация
    OpenedSession, вход переводит в -32603.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from abc import abstractmethod
from collections.abc import (
    AsyncGenerator,
    Awaitable,
    Callable,
    Iterator,
    Mapping,
    Sequence,
)
from contextlib import AsyncExitStack, aclosing
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from functools import partial
from pathlib import Path
from typing import Any, ClassVar, Generic, Protocol, TypeAlias, TypeVar

import mcp_types as mt
from acp.exceptions import RequestError
from acp.experimental.v2 import Agent as AcpAgentProtocol
from acp.experimental.v2 import AgentSideConnection, schema
from acp.experimental.v2.meta import AGENT_METHODS
from mcp_types.methods import parse_server_notification, parse_server_result
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from boba.agent.agent import Agent
from boba.agent.attachments import ConnectedServer
from boba.agent.control import (
    AllowAnswer,
    CanUseToolRequest,
    DenyAnswer,
    PermissionAnswer,
)
from boba.agent.events import (
    AgentEvent,
    ControlCancelEvent,
    ControlRequestEvent,
    ErrorEvent,
    EventSink,
    HistoryWrittenEvent,
    InitEvent,
    RecordEvent,
    ResultEvent,
    StreamEvent,
    ToolProgressEvent,
    ToolStartedEvent,
)
from boba.agent.history import (
    HistoryError,
    RecordNotFoundError,
    SessionHeldError,
    SessionId,
    SessionNotFoundError,
)
from boba.agent.ids import IdMint
from boba.agent.mcp import (
    Audience,
    ToolCard,
    ToolCards,
    ToolHints,
    ToolReplies,
    ToolReply,
)
from boba.agent.permissions import PermissionModeError
from boba.agent.queue import QueueEntry, QueueKind, QueuePriority, QueueRefusedError
from boba.agent.records import (
    ApiErrorRecord,
    AssistantRecord,
    CallStatus,
    ChainRecord,
    CompactionPhase,
    CompactionRecord,
    CompactSummaryRecord,
    InformationalRecord,
    NoticeLevel,
    PermissionMode,
    Record,
    ReplyRecord,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolResultRecord,
    ToolUseBlock,
    TurnEndRecord,
    TurnOutcome,
    UserRecord,
)
from boba.agent.session import SessionState
from boba.agent.settings import (
    McpClientJson,
)
from boba.agent.tags import LocalCommandText
from boba.agent.template import AgentTemplate, AgentTemplateFile, ModelTroubleTexts
from boba.agent.tools import (
    AskOption,
    AskQuestion,
    AskUserQuestionInput,
    AskUserQuestionTool,
    McpServerSet,
    ServerNotice,
    ToolServer,
)
from boba.agent.turn import ContextUsage
from boba.cancellation import StopReason as CoreStopReason
from boba.cli.threads import LoopCall, LoopThread, SessionLimitError, SessionThreads
from boba.core.commands import CommandArgument, CommandArgumentsError, CommandLines
from boba.core.mcp_names import McpToolNameError, McpToolNames
from boba.core.validation import ValidationText
from boba.transport.acp.protocol import (
    AcpErrorCode,
    AcpTransportError,
    AcpVersion,
    AgentSide,
    ElicitationRound,
    ErrorDataKey,
    McpErrorOutcome,
    McpMessage,
    McpOutcome,
    McpVersion,
    MetaKey,
    StopReason,
    ToolStatus,
)

logger = logging.getLogger(__name__)

__all__ = [
    "AcpAgent",
    "AcpMapping",
    "AcpToolServer",
    "CallProgressRelay",
    "ClientLink",
    "ConfigOptionId",
    "ForkMeta",
    "McpServerSpecs",
    "ObservedSession",
    "OpenedSession",
    "PermissionChoice",
    "PromptText",
    "SessionAssembly",
    "SessionBusyError",
    "SessionOpenError",
    "SessionOptions",
    "SessionPage",
    "SessionRefusedError",
    "SessionRelay",
    "SessionSettings",
    "SessionSummary",
    "SessionTitles",
    "ToolHintSource",
    "ToolKindName",
]

ReplyT = TypeVar("ReplyT", bound=BaseModel)
"""Модель ответа сервера MCP на запрос агента."""
PageT = TypeVar(
    "PageT",
    mt.ListToolsResult,
    mt.ListPromptsResult,
    mt.ListResourcesResult,
    mt.ListResourceTemplatesResult,
)
"""Страница списка сервера MCP."""
ListRequest: TypeAlias = (
    mt.ListToolsRequest
    | mt.ListPromptsRequest
    | mt.ListResourcesRequest
    | mt.ListResourceTemplatesRequest
)

ContentBlock: TypeAlias = (
    schema.TextContentBlock
    | schema.ImageContentBlock
    | schema.AudioContentBlock
    | schema.ResourceContentBlock
    | schema.EmbeddedResourceContentBlock
    | schema.OtherContentBlock
)
McpServerBlock: TypeAlias = (
    schema.HttpMcpServer
    | schema.AcpMcpServer
    | schema.StdioMcpServer
    | schema.OtherMcpServer
)
ConfigOption: TypeAlias = (
    schema.SelectSessionConfigOption
    | schema.BooleanSessionConfigOption
    | schema.OtherSessionConfigOption
)
FormValue: TypeAlias = str | int | float | bool | list[str]
ElicitationAnswer: TypeAlias = (
    schema.AcceptElicitationResponse
    | schema.DeclineElicitationResponse
    | schema.CancelElicitationResponse
    | schema.OtherElicitationResponse
)
FormProperty: TypeAlias = (
    schema.ElicitationStringPropertySchema
    | schema.ElicitationNumberPropertySchema
    | schema.ElicitationIntegerPropertySchema
    | schema.ElicitationBooleanPropertySchema
    | schema.ElicitationMultiSelectPropertySchema
    | schema.ElicitationOtherPropertySchema
)
Update: TypeAlias = (
    schema.UserMessageUpdate
    | schema.AgentMessageUpdate
    | schema.AgentThoughtUpdate
    | schema.AgentMessageChunk
    | schema.AgentThoughtChunk
    | schema.SessionToolCallUpdate
    | schema.UsageUpdate
    | schema.ConfigOptionUpdate
    | schema.SessionInfoUpdate
    | schema.SessionCompactionUpdate
    | schema.SessionNotice
    | schema.RunningSessionStateUpdate
    | schema.RequiresActionSessionStateUpdate
    | schema.IdleSessionStateUpdate
    | schema.AvailableCommandsUpdate
)


class SessionRefusedError(Exception):
    """Сборка сессии отвергла параметры запроса."""


class SessionOpenError(Exception):
    """Сессию открыть не удалось: хранилище, сервер MCP, профиль."""


class SessionBusyError(Exception):
    """Ход идёт: модель сессии менять поздно."""


class ConfigOptionId(StrEnum):
    """Настройки сессии (`configOptions`): идентификатор совпадает с
    категорией ACP того же имени."""

    MODE = "mode"
    MODEL = "model"


class ToolKindName(StrEnum):
    """Виды инструментов ACP (`kind`), которые агент выводит из пометок."""

    READ = "read"
    EDIT = "edit"
    EXECUTE = "execute"
    OTHER = "other"


class PermissionChoice(StrEnum):
    """Варианты ответа на `session/request_permission`: `optionId` совпадает
    с видом варианта ACP."""

    ALLOW_ONCE = "allow_once"
    ALLOW_ALWAYS = "allow_always"
    REJECT_ONCE = "reject_once"

    @property
    def label(self) -> str:
        if self is PermissionChoice.ALLOW_ONCE:
            return "Allow"

        if self is PermissionChoice.ALLOW_ALWAYS:
            return "Allow and remember"

        return "Reject"


class FormSchemaKey(StrEnum):
    """Ключи JSON Schema формы сервера MCP, которые агент переводит в ACP."""

    PROPERTIES = "properties"
    ENUM = "enum"
    ENUM_NAMES = "enumNames"
    ONE_OF = "oneOf"


class McpCapability(StrEnum):
    """Ключи `clientCapabilities`, которые агент объявляет серверам MCP."""

    ELICITATION = "elicitation"
    FORM = "form"


class FormKey(StrEnum):
    """Ключи полей формы `elicitation/create` своего вопроса агента: поле
    вопроса по его номеру и парное поле «Other» со своим текстом."""

    QUESTION = "q{number}"
    OTHER = "q{number}.other"

    def of(self, number: int) -> str:
        return self.value.format(number=number)


@dataclass(frozen=True)
class FormQuestion:
    """Вопрос AskUserQuestion в форме: ключ его поля и ключ парного поля
    «Other»."""

    key: str
    other: str
    question: AskQuestion


@dataclass(frozen=True)
class QuestionForm:
    """Вопрос AskUserQuestion формой `elicitation/create`: текст запроса,
    схема полей и вопросы по ключам полей — по ним ответ формы
    разворачивается обратно в ответы по текстам вопросов."""

    message: str
    requested: schema.ElicitationSchema
    questions: Sequence[FormQuestion]


class ForkMeta(BaseModel):
    """Расширение `boba/upToMessageId` в `_meta` запроса `session/fork`:
    `messageId` вопроса (uuid его записи `user`), на котором ветка кончается —
    в ветку идут записи до него. Без ключа ветка — копия всей сессии. Чужие
    ключи `_meta` не разбираются."""

    model_config = ConfigDict(extra="ignore")

    up_to_message_id: str | None = Field(
        default=None, alias=MetaKey.UP_TO_MESSAGE_ID.value, min_length=1
    )


@dataclass(frozen=True)
class SessionSummary:
    """Сессия хранилища для `session/list`: рабочий каталог, первый вопрос
    человека (None — вопросов не было) и время последней записи."""

    session_id: str
    cwd: str
    first_prompt: str | None
    updated_at: str


@dataclass(frozen=True)
class SessionPage:
    """Страница сессий хранилища и курсор следующей; None — страница последняя."""

    sessions: Sequence[SessionSummary]
    next_cursor: str | None


class SessionTitles:
    """Заголовок сессии для клиента: первая строка первого вопроса, не
    длиннее предела."""

    LIMIT: ClassVar[int] = 80
    ELLIPSIS: ClassVar[str] = "…"

    def title(self, first_prompt: str | None) -> str | None:
        if first_prompt is None:
            return None

        line = ""
        for candidate in first_prompt.splitlines():
            stripped = candidate.strip()
            if stripped:
                line = stripped
                break

        if not line:
            return None

        if len(line) > self.LIMIT:
            return line[: self.LIMIT] + self.ELLIPSIS

        return line


@dataclass(frozen=True)
class SessionSettings:
    """Состояние настроек сессии: действующий режим разрешений и доступные
    режимы, имя действующего профиля модели и имена доступных профилей."""

    mode: PermissionMode
    modes: Sequence[PermissionMode]
    model: str
    models: Sequence[str]

    def __post_init__(self) -> None:
        if self.mode not in self.modes:
            msg = f"the current mode {self.mode.value!r} is not among the available"
            raise ValueError(msg)

        if self.model not in self.models:
            msg = f"the current model {self.model!r} is not among the available"
            raise ValueError(msg)


@dataclass(frozen=True)
class ModeCard:
    """Имя и описание режима разрешений для клиента."""

    name: str
    description: str


class CompactionUpdateStatus(StrEnum):
    """Стадии `compaction_update` ACP."""

    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class OpenCompactions:
    """Сжатия воспроизведения, начатые записью и ещё не кончившиеся: агент
    мог остановиться посреди пересказа, и в истории осталось только начало.
    Такое сжатие закрывается неудачей, когда за ним идёт вопрос человека или
    кончается история — кроме идущего сейчас (live): его итог придёт вживую.
    Создаёт воспроизведение (AcpAgent) на каждый проход."""

    def __init__(self) -> None:
        self._open: list[str] = []

    def before(self, record: ChainRecord) -> Sequence[str]:
        """Сжатия, которые закрывает вопрос человека record."""
        if not isinstance(record, UserRecord):
            return ()

        if record.human_text() is None:
            return ()

        unfinished = list(self._open)
        self._open.clear()

        return unfinished

    def after(self, record: ChainRecord) -> None:
        if isinstance(record, CompactionRecord):
            self._follow(record)
            return

        if not isinstance(record, CompactSummaryRecord):
            return

        if record.uuid in self._open:
            self._open.remove(record.uuid)

    def unfinished(self, live: bool) -> Sequence[str]:
        """Сжатия без конца в конце истории; live — последнее из них ещё
        идёт."""
        unfinished = list(self._open)
        self._open.clear()
        if live and unfinished:
            unfinished.pop()

        return unfinished

    def _follow(self, record: CompactionRecord) -> None:
        if record.phase is CompactionPhase.STARTED:
            self._open.append(record.compaction_id)
            return

        if record.compaction_id in self._open:
            self._open.remove(record.compaction_id)


class SessionCommand(StrEnum):
    """Команды сессии агента: имя команды без косой черты."""

    COMPACT = "compact"


@dataclass(frozen=True)
class CommandCard:
    """Объявление команды сессии клиенту: что она делает и подсказка ввода
    её аргументов."""

    description: str
    hint: str


class SessionCommands:
    """Команды сессии агента в форме ACP: полный список
    `available_commands_update`, который клиент получает после ответа на
    `session/new|resume|fork` (поля `availableCommands` в ответах схема SDK
    не несёт). Реестр декларативный (CARDS); вызов команды — обычный
    `session/prompt` с текстом `/<имя> <аргументы>`. Создаёт вход
    (AcpAgent), объявление уходит клиенту через SessionMcp.announce_later."""

    CARDS: ClassVar[Mapping[SessionCommand, CommandCard]] = {
        SessionCommand.COMPACT: CommandCard(
            "Compact the conversation into a summary",
            "optional instructions for the summary",
        ),
    }

    def __init__(self) -> None:
        self._lines = CommandLines()

    def queued(self, asked: QueueEntry) -> QueueEntry | None:
        """Вопрос-вызов команды сессии → элемент очереди, который её исполняет
        (`/compact <указания>` → compact с указаниями текстом); None — вопрос
        не команда сессии."""
        call = self._lines.parsed(asked.text)
        if call is None:
            return None

        if call.name != SessionCommand.COMPACT.value:
            return None

        update = {"kind": QueueKind.COMPACT, "text": call.rest}
        return asked.model_copy(update=update)

    def update(self) -> schema.AvailableCommandsUpdate:
        commands: list[schema.AvailableCommand] = []
        for command, card in self.CARDS.items():
            hint = schema.TextAvailableCommandInput(hint=card.hint)
            commands.append(
                schema.AvailableCommand(
                    name=command.value, description=card.description, input=hint
                )
            )

        return schema.AvailableCommandsUpdate(available_commands=commands)


class SessionOptions:
    """Настройки сессии в форме ACP: `configOptions` `mode` и `model` из одного
    состояния SessionSettings; то же состояние — уведомлением
    `config_option_update`. Создаёт вход; ответы `session/new|resume|fork`
    собирают через него."""

    CARDS: ClassVar[Mapping[PermissionMode, ModeCard]] = {
        PermissionMode.DEFAULT: ModeCard(
            "Default", "Asks before risky actions; read-only tools run freely"
        ),
        PermissionMode.ACCEPT_EDITS: ModeCard(
            "Accept edits", "Edits run without asking; destructive actions still ask"
        ),
        PermissionMode.PLAN: ModeCard("Plan", "Tools that change anything do not run"),
        PermissionMode.DONT_ASK: ModeCard(
            "Don't ask", "Anything not allowed in advance is rejected"
        ),
        PermissionMode.BYPASS_PERMISSIONS: ModeCard(
            "Bypass permissions", "No permission checks; everything runs"
        ),
        PermissionMode.AUTO: ModeCard(
            "Auto", "A classifier model answers permission questions"
        ),
    }

    def config_options(self, settings: SessionSettings) -> list[ConfigOption]:
        return [self._mode_option(settings), self._model_option(settings)]

    def options_update(self, settings: SessionSettings) -> schema.ConfigOptionUpdate:
        return schema.ConfigOptionUpdate(config_options=self.config_options(settings))

    def parse_mode(self, value: str, settings: SessionSettings) -> PermissionMode:
        """Ошибки:
        RequestError — такого режима нет или он недоступен при параметрах
            запуска (-32602).
        """
        try:
            mode = PermissionMode(value)
        except ValueError as exc:
            raise self._refused(value, ConfigOptionId.MODE, settings.modes) from exc

        if mode not in settings.modes:
            raise self._refused(value, ConfigOptionId.MODE, settings.modes)

        return mode

    def parse_model(self, value: str, settings: SessionSettings) -> str:
        """Ошибки:
        RequestError — профиля с таким именем нет в настройках (-32602).
        """
        if value not in settings.models:
            raise self._refused(value, ConfigOptionId.MODEL, settings.models)

        return value

    def _mode_option(
        self, settings: SessionSettings
    ) -> schema.SelectSessionConfigOption:
        options: list[schema.SessionConfigSelectOption] = []
        for mode in settings.modes:
            card = self.CARDS[mode]
            options.append(
                schema.SessionConfigSelectOption(
                    value=mode.value, name=card.name, description=card.description
                )
            )

        return schema.SelectSessionConfigOption(
            config_id=ConfigOptionId.MODE,
            name="Permission mode",
            description="How the agent treats tool calls that change something",
            category=ConfigOptionId.MODE,
            current_value=settings.mode.value,
            options=options,
        )

    def _model_option(
        self, settings: SessionSettings
    ) -> schema.SelectSessionConfigOption:
        options: list[schema.SessionConfigSelectOption] = []
        for name in settings.models:
            options.append(schema.SessionConfigSelectOption(value=name, name=name))

        return schema.SelectSessionConfigOption(
            config_id=ConfigOptionId.MODEL,
            name="Model",
            description="Model profile from the agent settings used for the next turns",
            category=ConfigOptionId.MODEL,
            current_value=settings.model,
            options=options,
        )

    @staticmethod
    def _refused(
        value: str, option_id: ConfigOptionId, known: Sequence[object]
    ) -> RequestError:
        names: list[str] = []
        for item in known:
            names.append(str(item))

        details = (
            f"configOption '{option_id.value}' has no value {value!r}; "
            f"available: {names}"
        )

        return RequestError.invalid_params({ErrorDataKey.DETAILS.value: details})


class ToolHintSource(Protocol):
    """Откуда берутся пометки инструмента по имени: агент ядра или
    наблюдатель с подключёнными серверами."""

    @abstractmethod
    def tool_hints(self, name: str) -> ToolHints | None: ...


class ToolTitles:
    """Заголовок вызова для клиента: имя инструмента и краткие аргументы."""

    LIMIT: ClassVar[int] = 60
    ELLIPSIS: ClassVar[str] = "…"

    def title(self, name: str, arguments: Mapping[str, Any]) -> str:
        if not arguments:
            return name

        rendered = json.dumps(dict(arguments), ensure_ascii=False, sort_keys=True)
        if len(rendered) > self.LIMIT:
            rendered = rendered[: self.LIMIT] + self.ELLIPSIS

        return f"{name} {rendered}"


class PermissionAsk(BaseModel):
    """Запрос разрешения клиенту: заголовок, варианты ответа и вызов."""

    model_config = ConfigDict(frozen=True)

    title: str
    options: Sequence[schema.PermissionOption]
    subject: schema.ToolCallPermissionSubjectVariant


class AcpMapping:
    """Соответствие ядра протоколу: исходы хода → `idle` со `stopReason`,
    события → уведомления `session/update`, записи для человека (повтор
    запроса к модели, отказ модели, состояние серверов MCP, оборванный ход)
    → `notice` одинаково вживую и в воспроизведении, пометки инструмента → `kind`,
    вопрос `can_use_tool` → запрос разрешения и ответ на него, вопрос
    AskUserQuestion → форма `elicitation/create` и ответ формы → ответы
    вопроса.

    Единственное место, где ядро встречается со схемой ACP; создаёт вход и
    отдаёт приёмникам событий сессий.
    """

    STOP_REASONS: ClassVar[Mapping[TurnOutcome, StopReason]] = {
        TurnOutcome.COMPLETED: StopReason.END_TURN,
        TurnOutcome.API_ERROR: StopReason.REFUSAL,
        TurnOutcome.MAX_TURNS: StopReason.MAX_TURN_REQUESTS,
        TurnOutcome.OUTPUT_LIMIT: StopReason.REFUSAL,
        TurnOutcome.ABORTED_STREAMING: StopReason.CANCELLED,
        TurnOutcome.ABORTED_TOOLS: StopReason.CANCELLED,
        TurnOutcome.SHUTDOWN: StopReason.CANCELLED,
        TurnOutcome.BLOCKING_LIMIT: StopReason.REFUSAL,
        TurnOutcome.COMPACT_FAILURE_BREAKER: StopReason.REFUSAL,
        TurnOutcome.RAPID_REFILL_BREAKER: StopReason.REFUSAL,
        TurnOutcome.INTERRUPTED: StopReason.END_TURN,
    }
    MILLIS: ClassVar[float] = 1000.0

    CALL_STATUSES: ClassVar[Mapping[CallStatus, ToolStatus]] = {
        CallStatus.COMPLETED: ToolStatus.COMPLETED,
        CallStatus.FAILED: ToolStatus.FAILED,
        CallStatus.CANCELLED: ToolStatus.CANCELLED,
    }
    """Исход вызова записи результата → статус вызова ACP."""

    COMPACTION_STATUSES: ClassVar[Mapping[CompactionPhase, CompactionUpdateStatus]] = {
        CompactionPhase.STARTED: CompactionUpdateStatus.IN_PROGRESS,
        CompactionPhase.FAILED: CompactionUpdateStatus.FAILED,
        CompactionPhase.CANCELLED: CompactionUpdateStatus.CANCELLED,
    }
    INTERRUPTED: ClassVar[str] = (
        "the compaction did not finish: the agent stopped before it wrote the "
        "summary; run /compact again"
    )
    """Причина у сжатия, чьё начало есть в истории, а конца нет."""

    def __init__(self) -> None:
        self._titles = ToolTitles()
        self._templates = AgentTemplate()
        self._troubles = ModelTroubleTexts(self._templates)
        self._commands = LocalCommandText()

    def turn_ended(self, record: TurnEndRecord) -> Iterator[Update]:
        """Конец хода по его записи — одинаково вживую и в воспроизведении:
        пометка о причине, если она есть, затем `idle` со `stopReason`
        исхода. Ход, оборванный гибелью агента, кончается штатным idle: его
        пометка уже говорит, что случилось."""
        if record.notice:
            detail = record.detail
            if detail == record.notice:
                detail = ""

            yield self.notice(record.level, record.notice, detail)

        yield schema.IdleSessionStateUpdate(
            stop_reason=self.STOP_REASONS[record.outcome]
        )

    def notice(
        self, level: NoticeLevel, text: str, detail: str
    ) -> schema.SessionNotice:
        """Пометка: text — слова человеку заголовком, detail — короткая
        техническая причина описанием (пусто — без описания)."""
        description: str | None = None
        if detail:
            description = detail

        return schema.SessionNotice(
            severity=level.value, title=text, description=description
        )

    def serving_stopped(self) -> Sequence[Update]:
        """Обслуживание сессии сорвалось: пометка и конец хода отказом;
        причина — в журнале агента."""
        text = self._templates.read(AgentTemplateFile.SERVING_STOPPED_NOTICE)

        return (
            self.notice(NoticeLevel.ERROR, text, ""),
            schema.IdleSessionStateUpdate(stop_reason=StopReason.REFUSAL),
        )

    def notices(self, record: ChainRecord) -> Iterator[schema.SessionNotice]:
        """Пометки человеку посреди хода, которые несёт запись: повтор
        запроса к модели и состояние серверов MCP."""
        if isinstance(record, InformationalRecord):
            yield self.notice(record.level, record.content, record.detail)
            return

        if isinstance(record, ApiErrorRecord):
            yield self._retry(record)

    def _retry(self, record: ApiErrorRecord) -> schema.SessionNotice:
        """Неудачная попытка запроса к модели и пауза до повтора."""
        seconds = record.retry_in_ms / self.MILLIS
        text = self._troubles.retry(
            record.trouble, record.retry_attempt, record.max_retries, seconds
        )
        return self.notice(NoticeLevel.WARNING, text, record.detail)

    def tool_kind(self, hints: ToolHints | None) -> ToolKindName:
        """`kind` по пометкам MCP: читающий — read, разрушающий — execute,
        меняющий — edit; инструмент без карточки — other."""
        if hints is None:
            return ToolKindName.OTHER

        if hints.read_only:
            return ToolKindName.READ

        if hints.destructive:
            return ToolKindName.EXECUTE

        return ToolKindName.EDIT

    def chunks(self, event: StreamEvent) -> Sequence[Update]:
        updates: list[Update] = []
        if event.reasoning:
            updates.append(
                schema.AgentThoughtChunk(
                    message_id=event.message_id,
                    content=schema.TextContentBlock(text=event.reasoning),
                )
            )

        if event.content:
            updates.append(
                schema.AgentMessageChunk(
                    message_id=event.message_id,
                    content=schema.TextContentBlock(text=event.content),
                )
            )

        return updates

    def tool_call(
        self, block: ToolUseBlock, hints: ToolHints | None
    ) -> schema.SessionToolCallUpdate:
        """Первое уведомление вызова; у вопроса пользователю (AskUserQuestion)
        в `_meta` — признак MetaKey.QUESTION, по нему клиент рисует вопрос."""
        meta: dict[str, Any] | None = None
        if block.name == AskUserQuestionTool.NAME:
            meta = {MetaKey.QUESTION.value: True}

        return schema.SessionToolCallUpdate(
            tool_call_id=block.id,
            name=block.name,
            title=self._titles.title(block.name, block.input),
            kind=self.tool_kind(hints),
            status=ToolStatus.PENDING,
            raw_input=dict(block.input),
            field_meta=meta,
        )

    def tool_results(
        self, record: ToolResultRecord
    ) -> Sequence[schema.SessionToolCallUpdate]:
        updates: list[schema.SessionToolCallUpdate] = []
        for block in record.message.tool_results():
            updates.append(self._tool_result(block, record))

        return updates

    def user_updates(self, record: UserRecord) -> Iterator[Update]:
        """Запись `user`: итоги вызовов либо вопрос человека целым
        сообщением `user_message` с uuid записи как `messageId` — текст, за
        ним ссылки на вложения блоками resource_link; запись вызова локальной
        команды — текстом вызова, каким его набрал пользователь; служебные
        записи в ленту не идут."""
        if isinstance(record, ToolResultRecord):
            yield from self.tool_results(record)
            return

        text = self.prompt_text(record)
        if text is None:
            return

        content: list[ContentBlock] = [schema.TextContentBlock(text=text)]
        for link in record.message.links():
            dumped = link.model_dump(mode="json", by_alias=True, exclude_none=True)
            content.append(schema.ResourceContentBlock.model_validate(dumped))

        yield schema.UserMessageUpdate(message_id=record.uuid, content=content)

    def prompt_text(self, record: UserRecord) -> str | None:
        """Текст вопроса человека, каким его видит клиент: запись вызова
        локальной команды — текстом вызова; None — запись не вопрос."""
        text = record.human_text()
        if text is None:
            return None

        call = self._commands.parsed(text)
        if call is None:
            return text

        return call.text()

    def replayed(self, record: ChainRecord, hints: ToolHintSource) -> Iterator[Update]:
        """Запись беседы → её уведомления: сообщения целиком (`user_message`,
        `agent_thought`, `agent_message`), вызовы и их итоги — как в живом
        ходе, пометки человеку — теми же `notice`, что вживую. Конец хода
        остановкой или отказом модели — тот же idle, что вживую: маркер
        остановки — `cancelled`, запись об отказе модели — `refusal`; прочие
        синтетические записи агента (заглушки) не воспроизводятся,
        оборванный ответ — воспроизводится; служебные сообщения, вложения и
        границы сжатия в ленту не идут; записи хода сжатия и пересказ — его
        стадиями `compaction_update`, как вживую."""
        yield from self.notices(record)
        if isinstance(record, CompactionRecord):
            yield self.compaction(record)
            return

        if isinstance(record, CompactSummaryRecord):
            yield self.compacted(record)
            return

        if isinstance(record, UserRecord):
            yield from self.user_updates(record)
            return

        if isinstance(record, TurnEndRecord):
            yield from self.turn_ended(record)
            return

        if isinstance(record, ReplyRecord):
            yield from self._replayed_reply(record, hints)

    def _replayed_reply(
        self, record: ReplyRecord, hints: ToolHintSource
    ) -> Iterator[Update]:
        message_id = record.message.id
        thoughts: list[ContentBlock] = []
        texts: list[ContentBlock] = []
        calls: list[ToolUseBlock] = []
        for block in record.message.content:
            if isinstance(block, ThinkingBlock):
                thoughts.append(schema.TextContentBlock(text=block.thinking))
                continue

            if isinstance(block, TextBlock):
                texts.append(schema.TextContentBlock(text=block.text))
                continue

            calls.append(block)

        if thoughts:
            yield schema.AgentThoughtUpdate(message_id=message_id, content=thoughts)

        if texts:
            yield schema.AgentMessageUpdate(message_id=message_id, content=texts)

        for block in calls:
            yield self.tool_call(block, hints.tool_hints(block.name))

    def _tool_result(
        self, block: ToolResultBlock, record: ToolResultRecord
    ) -> schema.SessionToolCallUpdate:
        """Итог вызова: текст модели содержимым, исход вызова записи —
        статусом, ответ tools/call как он записан в `toolUseResult` —
        `rawOutput`."""
        text = schema.TextContentBlock(text=block.text())

        return schema.SessionToolCallUpdate(
            tool_call_id=block.tool_use_id,
            status=self.CALL_STATUSES[record.status],
            content=[schema.ContentToolCallContent(content=text)],
            raw_output=record.tool_use_result,
        )

    def tool_started(self, event: ToolStartedEvent) -> schema.SessionToolCallUpdate:
        return schema.SessionToolCallUpdate(
            tool_call_id=event.tool_use_id, status=ToolStatus.IN_PROGRESS
        )

    def tool_progress(self, event: ToolProgressEvent) -> schema.SessionToolCallUpdate:
        """Ход вызова — одна строка содержимого, которая заменяет прежнюю, и
        `_meta` уведомления сервера как есть. Числа без итога ничего не
        говорят: у отчёта с текстом без total строка — сам текст, а
        уведомление без текста и без total (например, о журнале вызова)
        несёт только `_meta`."""
        meta: dict[str, Any] | None = None
        if event.meta:
            meta = dict(event.meta)

        bare = not event.message
        if event.total is not None:
            bare = False

        if bare:
            return schema.SessionToolCallUpdate(
                tool_call_id=event.tool_use_id,
                status=ToolStatus.IN_PROGRESS,
                field_meta=meta,
            )

        done = f"{event.progress:g}"
        if event.total is not None:
            done = f"{done} of {event.total:g}"

        if not event.message:
            line = done
        elif event.total is None:
            line = event.message
        else:
            line = f"{event.message} — {done}"

        text = schema.TextContentBlock(text=line)

        return schema.SessionToolCallUpdate(
            tool_call_id=event.tool_use_id,
            status=ToolStatus.IN_PROGRESS,
            content=[schema.ContentToolCallContent(content=text)],
            field_meta=meta,
        )

    def usage_update(self, usage: ContextUsage) -> schema.UsageUpdate:
        """Заполненность контекста: `used` — оценка разговора, `size` —
        действующее окно профиля."""
        return schema.UsageUpdate(used=usage.used, size=usage.size)

    def title_update(self, title: str) -> schema.SessionInfoUpdate:
        return schema.SessionInfoUpdate(title=title)

    def activity_update(self, event: HistoryWrittenEvent) -> schema.SessionInfoUpdate:
        """Время последней активности сессии — время последней записи,
        сохранённой в истории: то же, что `updatedAt` в `session/list`."""
        updated_at = datetime.fromisoformat(event.recorded_at)

        return schema.SessionInfoUpdate(updated_at=updated_at)

    def compaction(self, record: CompactionRecord) -> schema.SessionCompactionUpdate:
        """Запись хода сжатия → его стадия: начато, не удалось с причиной,
        прервано."""
        error: str | None = None
        if record.error:
            error = record.error

        status = self.COMPACTION_STATUSES[record.phase]

        return schema.SessionCompactionUpdate(
            compaction_id=record.compaction_id, status=status.value, error=error
        )

    def compacted(self, record: CompactSummaryRecord) -> schema.SessionCompactionUpdate:
        """Пересказ — состоявшееся сжатие с ним; id сжатия — uuid пересказа."""
        content = record.message.content
        if not isinstance(content, str):
            msg = (
                f"The compaction summary record {record.uuid} holds blocks instead "
                "of text. This is a bug."
            )
            raise RuntimeError(msg)

        return schema.SessionCompactionUpdate(
            compaction_id=record.uuid,
            status=CompactionUpdateStatus.COMPLETED.value,
            summary=[schema.TextContentBlock(text=content)],
        )

    def interrupted(self, compaction_id: str) -> schema.SessionCompactionUpdate:
        """Сжатие, у которого в истории есть начало и нет конца: агент
        остановился раньше, чем записал итог."""
        return schema.SessionCompactionUpdate(
            compaction_id=compaction_id,
            status=CompactionUpdateStatus.FAILED.value,
            error=self.INTERRUPTED,
        )

    def permission_request(
        self, request: CanUseToolRequest, hints: ToolHints | None
    ) -> PermissionAsk:
        title = self._titles.title(request.tool_name, request.input)
        tool_call = schema.ToolCallUpdate(
            tool_call_id=request.tool_use_id,
            name=request.tool_name,
            title=title,
            kind=self.tool_kind(hints),
            status=ToolStatus.PENDING,
            raw_input=dict(request.input),
        )
        options: list[schema.PermissionOption] = []
        for choice in PermissionChoice:
            options.append(
                schema.PermissionOption(
                    option_id=choice.value, name=choice.label, kind=choice
                )
            )

        return PermissionAsk(
            title=title,
            options=options,
            subject=schema.ToolCallPermissionSubjectVariant(tool_call=tool_call),
        )

    def permission_answer(
        self, response: schema.RequestPermissionResponse, request: CanUseToolRequest
    ) -> PermissionAnswer:
        """Ответ клиента → ответ ядру; отмена и неизвестный вариант — отказ."""
        outcome = response.outcome
        if not isinstance(outcome, schema.SelectedPermissionOutcomeVariant):
            return DenyAnswer()

        try:
            choice = PermissionChoice(outcome.option_id)
        except ValueError:
            logger.warning(
                "permission: the client chose unknown option %r for %s; treated "
                "as a rejection",
                outcome.option_id,
                request.tool_name,
            )
            return DenyAnswer()

        if choice is PermissionChoice.REJECT_ONCE:
            return DenyAnswer()

        if choice is PermissionChoice.ALLOW_ALWAYS:
            return AllowAnswer(updated_permissions=list(request.permission_suggestions))

        return AllowAnswer()

    def question_form(self, request: CanUseToolRequest) -> QuestionForm:
        """Вопрос AskUserQuestion формой: поле на вопрос (одиночный выбор —
        строка с `oneOf`, множественный — массив с `items.anyOf`), заголовок
        вопроса — `title`, текст — `description`, у варианта — описание и
        предпросмотр в `_meta`; у каждого вопроса парное строковое поле
        «Other», его ключ — в `_meta` поля вопроса. `required` пуст: ответ
        на вопрос — вариант либо свой текст, схема так не умеет.

        Ошибки:
        ValidationError — ввод вызова не по схеме AskUserQuestion.
        """
        asked = AskUserQuestionInput.model_validate(request.input)
        properties: dict[str, FormProperty] = {}
        questions: list[FormQuestion] = []
        texts: list[str] = []
        for number, question in enumerate(asked.questions, start=1):
            key = FormKey.QUESTION.of(number)
            other = FormKey.OTHER.of(number)
            options = list(self._enum_options(question.options))
            properties[key] = self._question_field(question, options, other)
            properties[other] = schema.ElicitationStringPropertySchema(
                title=AskQuestion.OTHER
            )
            questions.append(FormQuestion(key=key, other=other, question=question))
            texts.append(question.question)

        return QuestionForm(
            message="\n".join(texts),
            requested=schema.ElicitationSchema(properties=properties),
            questions=questions,
        )

    def question_answer(
        self, response: ElicitationAnswer, form: QuestionForm
    ) -> PermissionAnswer:
        """Ответ формы → ответ ядру: принятая форма — ответы по текстам
        вопросов (метки через ", " в порядке отметки, свой текст последним,
        выбранный пустой «Other» — словом Other), отказ — DenyAnswer, отмена
        и неизвестное действие — ответ без ответов."""
        if isinstance(response, schema.DeclineElicitationResponse):
            return DenyAnswer()

        if not isinstance(response, schema.AcceptElicitationResponse):
            return AllowAnswer()

        content: Mapping[str, FormValue] = {}
        if response.content is not None:
            content = response.content

        answers: dict[str, str] = {}
        for asked in form.questions:
            parts = list(self._picked(asked, content.get(asked.key)))
            typed = content.get(asked.other)
            if isinstance(typed, str):
                text = typed.strip()
                if not text:
                    text = AskQuestion.OTHER

                parts.append(text)

            if parts:
                answers[asked.question.question] = AskQuestion.JOIN.join(parts)

        return AllowAnswer(updated_input={AskUserQuestionTool.ANSWERS_KEY: answers})

    def server_form(self, requested: Mapping[str, Any]) -> schema.ElicitationSchema:
        """Схема формы сервера MCP — схемой ACP: поля те же, устаревшие
        `enumNames` строкового поля становятся вариантами `oneOf`.

        Ошибки:
        ValidationError — схема не форма ACP.
        """
        fields: dict[str, Any] = {}
        properties = requested.get(FormSchemaKey.PROPERTIES.value)
        if isinstance(properties, Mapping):
            for key, field in properties.items():
                fields[str(key)] = self._titled_enum(field)

        shown = dict(requested)
        shown[FormSchemaKey.PROPERTIES.value] = fields

        return schema.ElicitationSchema.model_validate(shown)

    @staticmethod
    def _titled_enum(field: Any) -> Any:
        """Строковое поле с `enum` и `enumNames` → поле с `oneOf`; прочее —
        как есть."""
        if not isinstance(field, Mapping):
            return field

        shown: dict[str, Any] = dict(field)
        names = shown.pop(FormSchemaKey.ENUM_NAMES.value, None)
        if not isinstance(names, list):
            return shown

        values = shown.get(FormSchemaKey.ENUM.value)
        if not isinstance(values, list):
            return shown

        options: list[dict[str, Any]] = []
        for value, title in zip(values, names, strict=False):
            options.append({"const": value, "title": title})

        shown.pop(FormSchemaKey.ENUM.value)
        shown[FormSchemaKey.ONE_OF.value] = options

        return shown

    def server_answer(self, answer: ElicitationAnswer) -> mt.ElicitResult:
        """Ответ формы клиента → ответ серверу MCP; неизвестное действие —
        отмена."""
        if isinstance(answer, schema.AcceptElicitationResponse):
            content: dict[str, FormValue | None] = {}
            if answer.content is not None:
                content.update(answer.content)

            return mt.ElicitResult(action="accept", content=content)

        if isinstance(answer, schema.DeclineElicitationResponse):
            return mt.ElicitResult(action="decline")

        return mt.ElicitResult(action="cancel")

    @staticmethod
    def _question_field(
        question: AskQuestion, options: list[schema.EnumOption], other: str
    ) -> FormProperty:
        """Поле вопроса: строка с `oneOf` у одиночного выбора, массив с
        `items.anyOf` у множественного."""
        meta = {MetaKey.OTHER.value: other}
        if question.multi_select:
            return schema.ElicitationMultiSelectPropertySchema(
                title=question.header,
                description=question.question,
                items=schema.TitledMultiSelectItems(any_of=options),
                field_meta=meta,
            )

        return schema.ElicitationStringPropertySchema(
            title=question.header,
            description=question.question,
            one_of=options,
            field_meta=meta,
        )

    @staticmethod
    def _enum_options(options: Sequence[AskOption]) -> Iterator[schema.EnumOption]:
        for option in options:
            meta: dict[str, Any] | None = None
            if option.preview:
                meta = {MetaKey.PREVIEW.value: option.preview}

            yield schema.EnumOption(
                const=option.label,
                title=option.label,
                description=option.description,
                field_meta=meta,
            )

    @staticmethod
    def _picked(asked: FormQuestion, value: FormValue | None) -> Iterator[str]:
        """Метки вариантов вопроса в значении поля: строка одиночного выбора
        либо список множественного; значение не из вариантов — в журнал."""
        values: list[str] = []
        if isinstance(value, str):
            values = [value]

        if isinstance(value, list):
            values = value

        labels = asked.question.labels()
        for picked in values:
            if picked in labels:
                yield picked
                continue

            logger.warning(
                "question %r: the client answered %r, which is not one of its "
                "options %s; the value is dropped",
                asked.question.question,
                picked,
                labels,
            )


class PromptText:
    """Вопрос из блоков `session/prompt` элементом очереди: текст как есть,
    ссылки на ресурсы — блоками resource_link записи `user`, текст только
    для модели (annotations.audience без "user") — служебным контекстом
    вопроса, а не его словами; прочие блоки агент не объявлял."""

    def render(self, blocks: Sequence[ContentBlock]) -> QueueEntry:
        """Ошибки:
        RequestError — блок неподдержанного вида или пустой вопрос (-32602).
        """
        parts: list[str] = []
        links: list[mt.ResourceLink] = []
        context: list[str] = []
        for block in blocks:
            if isinstance(block, schema.ResourceContentBlock):
                dumped = block.model_dump(mode="json", by_alias=True, exclude_none=True)
                links.append(mt.ResourceLink.model_validate(dumped))
                continue

            if not isinstance(block, schema.TextContentBlock):
                parts.append(self._part(block))
                continue

            audience = None
            if block.annotations is not None:
                audience = block.annotations.audience

            if Audience.USER.reads(audience):
                parts.append(block.text)
                continue

            context.append(block.text)

        text = "".join(parts)
        empty = not text.strip()
        if empty and not links:
            data = {ErrorDataKey.DETAILS.value: "the prompt has no text to send"}
            raise RequestError.invalid_params(data)

        return QueueEntry(
            kind=QueueKind.PROMPT, text=text, links=links, context=context
        )

    def _part(self, block: ContentBlock) -> str:
        if isinstance(block, schema.TextContentBlock):
            return block.text

        details = (
            f"prompt blocks of type '{block.type}' are not supported: this agent "
            "declared no image, audio or embedded context capability; send text "
            "and resource_link blocks"
        )
        raise RequestError.invalid_params({ErrorDataKey.DETAILS.value: details})


class McpServerSpecs:
    """Серверы MCP из `session/new|resume|fork`: принимаются только записи
    `type: "acp"` (MCP-over-ACP). Имя сервера входит в полные имена его
    инструментов и проверяется их формой (McpToolNames)."""

    def __init__(self) -> None:
        self._names = McpToolNames()

    def servers(
        self, blocks: Sequence[McpServerBlock]
    ) -> Sequence[schema.AcpMcpServer]:
        """Ошибки:
        RequestError — запись не `acp`, повтор имени либо имя, которое не
            годится для полных имён инструментов (-32602).
        """
        accepted: list[schema.AcpMcpServer] = []
        named: set[str] = set()
        for server in blocks:
            if not isinstance(server, schema.AcpMcpServer):
                details = (
                    f"mcpServers entry of type '{server.type}' is refused: this "
                    "agent declared mcp.acp only and reaches tools through "
                    "mcp/message; declare the server with type 'acp'"
                )
                raise RequestError.invalid_params({ErrorDataKey.DETAILS.value: details})

            try:
                self._names.checked_server(server.name)
            except McpToolNameError as exc:
                details = f"mcpServers entry '{server.name}' is refused: {exc}"
                raise RequestError.invalid_params(
                    {ErrorDataKey.DETAILS.value: details}
                ) from exc

            if server.name in named:
                details = (
                    f"mcpServers names '{server.name}' twice; names must be unique"
                )
                raise RequestError.invalid_params({ErrorDataKey.DETAILS.value: details})

            named.add(server.name)
            accepted.append(server)

        return accepted


class ClientLink:
    """Связь агента с клиентом: сторона агента (mcp/message, idle с ошибкой)
    и соединение SDK (уведомления, разрешения, формы) живут в главном цикле
    процесса, а зовут их сессии из своих потоков — каждый вызов уходит в
    главный цикл (LoopCall), а уведомления хода вызова mcp/message
    возвращаются в цикл сессии, которая его послала. Создаёт AcpAgent.attach,
    держат SessionRelay и AcpToolServer."""

    def __init__(
        self, side: AgentSide, connection: AgentSideConnection, home: LoopCall
    ) -> None:
        self._side = side
        self._connection = connection
        self._home = home

    async def mcp_request(
        self,
        message: McpMessage,
        receiver: Callable[[McpMessage], Awaitable[None]] | None,
    ) -> McpOutcome:
        """Запрос mcp/message клиенту; уведомления его хода получает receiver
        в цикле того, кто спросил.

        Ошибки:
        AcpTransportError, RequestError — как у AgentSide.request.
        """
        relayed: Callable[[McpMessage], Awaitable[None]] | None = None
        if receiver is not None:
            relayed = self._relayed(receiver, LoopCall(asyncio.get_running_loop()))

        return await self._home.run(self._side.request(message, relayed))

    async def session_update(self, session_id: str, update: Update) -> None:
        await self._home.run(self._connection.session_update(session_id, update))

    async def create_elicitation(
        self,
        message: str,
        session_id: str,
        tool_call_id: str,
        requested: schema.ElicitationSchema,
        meta: Mapping[str, Any],
    ) -> ElicitationAnswer:
        asked = self._connection.create_elicitation(
            message=message,
            mode="form",
            session_id=session_id,
            tool_call_id=tool_call_id,
            requested_schema=requested,
            **meta,
        )

        return await self._home.run(asked)

    async def request_permission(
        self, session_id: str, ask: PermissionAsk
    ) -> schema.RequestPermissionResponse:
        asked = self._connection.request_permission(
            session_id=session_id,
            title=ask.title,
            options=list(ask.options),
            subject=ask.subject,
        )

        return await self._home.run(asked)

    @staticmethod
    def _relayed(
        receiver: Callable[[McpMessage], Awaitable[None]], caller: LoopCall
    ) -> Callable[[McpMessage], Awaitable[None]]:
        async def received(message: McpMessage) -> None:
            await receiver(message)

        async def relayed(message: McpMessage) -> None:
            await caller.run(received(message))

        return relayed


class CallProgressRelay:
    """Ход одного вызова: уведомление `notifications/progress` его запроса
    mcp/message → событие ToolProgressEvent ядра в приёмник сессии."""

    def __init__(self, sink: EventSink, tool_use_id: str) -> None:
        self._sink = sink
        self._tool_use_id = tool_use_id
        self._started = time.monotonic()

    async def accept(self, message: McpMessage) -> None:
        """Ошибки:
        RequestError — уведомление не разбирается как уведомление сервера MCP
            (-32602).
        """
        params: Mapping[str, Any] = {}
        if message.params is not None:
            params = message.params

        try:
            notification = parse_server_notification(
                message.method, McpVersion.V2026_07_28, params
            )
        except (KeyError, ValidationError) as exc:
            details = (
                f"call {self._tool_use_id}: mcp/message notification "
                f"{message.method!r} of request {message.request_id} expected an "
                f"MCP server notification, got {repr(params)[:200]}: {exc}"
            )
            raise RequestError.invalid_params(
                {ErrorDataKey.DETAILS.value: details}
            ) from exc

        if not isinstance(notification, mt.ProgressNotification):
            logger.info(
                "call %s: mcp/message notification %s is not progress; dropped",
                self._tool_use_id,
                message.method,
            )
            return

        progress = notification.params
        message_text = progress.message
        if message_text is None:
            message_text = ""

        meta: dict[str, Any] = {}
        if progress.meta is not None:
            meta = progress.meta

        await self._sink.emit(
            ToolProgressEvent(
                tool_use_id=self._tool_use_id,
                message=message_text,
                progress=progress.progress,
                total=progress.total,
                elapsed_sec=time.monotonic() - self._started,
                meta=meta,
            )
        )


class McpRequestError(Exception):
    """Запрос MCP к серверу регистрации через mcp/message не дал результата:
    ошибка ACP, ошибка MCP, срок или ответ не по схеме."""


class McpUnreachableError(McpRequestError):
    """Запрос не дошёл до сервера: клиент ответил ошибкой ACP, связь
    оборвалась или ответа нет в срок."""


class McpListing(StrEnum):
    """Ответы сервера, которые агент держит со сроком свежести по ttlMs
    утилиты caching: описание сервера и списки; ресурсы для модели — по
    обоим спискам ресурсов вместе."""

    DISCOVER = "server/discover"
    TOOLS = "tools/list"
    PROMPTS = "prompts/list"
    RESOURCES = "resources/list"


@dataclass(frozen=True)
class McpPages(Generic[PageT]):
    """Все страницы одного списка сервера и срок их общей свежести — самый
    ранний из сроков страниц по монотонным часам."""

    pages: Sequence[PageT]
    fresh_until: float


class ServerNotices:
    """Смены состояния серверов MCP сессии до начала следующего хода:
    сервер не открылся, перестал отвечать, ответил снова. Копят их
    SessionMcp и его серверы (AcpToolServer), забирает ход через
    SessionMcp.refreshed и пишет пометками в историю."""

    def __init__(self) -> None:
        self._templates = AgentTemplate()
        self._pending: list[ServerNotice] = []

    def unavailable(self, server: str, failure: BaseException) -> None:
        text = self._templates.read(AgentTemplateFile.MCP_UNAVAILABLE_NOTICE)
        self._add(NoticeLevel.WARNING, text.format(server=server), self._cause(failure))

    def lost(self, server: str, failure: BaseException) -> None:
        text = self._templates.read(AgentTemplateFile.MCP_LOST_NOTICE)
        self._add(NoticeLevel.WARNING, text.format(server=server), self._cause(failure))

    def back(self, server: str) -> None:
        text = self._templates.read(AgentTemplateFile.MCP_BACK_NOTICE)
        self._add(NoticeLevel.INFO, text.format(server=server), "")

    def taken(self) -> Sequence[ServerNotice]:
        """Накопленные смены; каждая отдаётся один раз."""
        taken = list(self._pending)
        self._pending.clear()

        return taken

    def _add(self, level: NoticeLevel, text: str, detail: str) -> None:
        self._pending.append(ServerNotice(level=level, text=text, detail=detail))

    @staticmethod
    def _cause(failure: BaseException) -> str:
        """Короткая причина для человека: `details` ответа клиента ACP об
        отказе сервера (статус HTTP, страница вместо MCP, нет соединения);
        без неё — пусто, полный текст сбоя остаётся в журнале агента."""
        cause: BaseException | None = failure
        while cause is not None:
            if isinstance(cause, RequestError):
                return ServerNotices._details(cause)

            cause = cause.__cause__

        return ""

    @staticmethod
    def _details(error: RequestError) -> str:
        data = error.data
        if not isinstance(data, Mapping):
            return ""

        details = data.get(ErrorDataKey.DETAILS.value)
        if not isinstance(details, str):
            return ""

        return details


class AcpToolServer(ToolServer):
    """Реализация ToolServer над одной регистрацией MCP-over-ACP: запись
    `type: "acp"` сессии.

    Создаёт AcpAgent на каждую запись сессии и отдаёт SessionMcp. open()
    согласует сервер (server/discover: возможности и инструкции), читает все
    страницы `tools/list`, промпты и ресурсы для модели; refreshed() в
    начале хода перечитывает то из них, чей срок свежести (ttlMs ответа)
    вышел. Сервер, до которого запрос не дошёл, отключается: его
    инструменты, промпты и инструкции уходят из сессии до первого удачного
    перечитывания. Если сервер обещал смену списков, агент держит поток
    subscriptions/listen: смена делает список устаревшим; кончившийся поток
    поднимает следующее удачное перечитывание. Вызов — `tools/call`
    запросом mcp/message через AgentSide; ответ сервера как пришёл ложится
    в ToolReply, текст для модели по нему собирает агент, ход вызова идёт
    в приёмник сессии через CallProgressRelay. ask() — любой другой запрос MCP (ресурсы,
    промпты). Каждый запрос несёт в _meta версию протокола, clientInfo
    агента и его clientCapabilities: формы (elicitation.form) агент
    объявляет, только если их объявил его клиент. Просьбы сервера о вводе
    (input_required) приёмник сессии задаёт пользователю формами
    `elicitation/create`, вызов повторяется с ответами.
    """

    MILLIS: ClassVar[int] = 1000

    def __init__(  # noqa: PLR0913 — сервер собирается всеми частями сессии
        self,
        link: ClientLink,
        server: schema.AcpMcpServer,
        relay: SessionRelay,
        limits: McpClientJson,
        mapping: AcpMapping,
        client: mt.Implementation,
        changes: ServerNotices,
    ) -> None:
        self._link = link
        self._server = server
        self._changes = changes
        self._sink: EventSink = relay
        self._relay = relay
        self._limits = limits
        self._mapping = mapping
        self._ids = IdMint()
        self._cards_of = ToolCards()
        self._replies = ToolReplies()
        self._names = McpToolNames()
        self._cards: list[ToolCard] = []
        self._prompts: list[mt.Prompt] = []
        self._for_model = False
        """Сервер отдаёт модели ресурсы или шаблоны ресурсов."""
        self._discovered: mt.DiscoverResult | None = None
        self._fresh: dict[McpListing, float] = {}
        """Срок свежести ответов по монотонным часам; без срока ответ устарел."""
        self._connected = False
        self._listening: asyncio.Task[None] | None = None
        capabilities: dict[str, Any] = {}
        if relay.elicits:
            capabilities = {
                McpCapability.ELICITATION.value: {McpCapability.FORM.value: {}}
            }

        self._meta: mt.RequestParamsMeta = {
            mt.PROTOCOL_VERSION_META_KEY: McpVersion.V2026_07_28.value,
            mt.CLIENT_INFO_META_KEY: client.model_dump(
                mode="json", by_alias=True, exclude_none=True
            ),
            mt.CLIENT_CAPABILITIES_META_KEY: capabilities,
        }
        self._label = f"MCP server '{server.name}' (serverId '{server.server_id}')"

    @property
    def name(self) -> str:
        """Имя сервера в сессии: часть полных имён его инструментов."""
        return self._server.name

    @property
    def connected(self) -> bool:
        """Последнее чтение у сервера удалось: его списки и инструкции в
        сессии."""
        return self._connected

    async def open(self) -> None:
        """Согласование, инструменты, промпты, ресурсы и поток смены списков.

        Ошибки:
        SessionOpenError — клиент ответил ошибкой ACP, ошибкой MCP, не уложился
            в срок или ответил не по схеме, либо сервер назвал инструмент,
            имя которого не годится для полного имени.
        """
        try:
            await self._fetched()
        except McpRequestError as exc:
            raise SessionOpenError(str(exc)) from exc

        self._connected = True
        self._follow()
        logger.info(
            "mcp: server %s (%s) offers %d tool(s), %d prompt(s)",
            self._server.name,
            self._server.server_id,
            len(self._cards),
            len(self._prompts),
        )

    async def refreshed(self) -> None:
        """Ответы с вышедшим сроком свежести — заново, свежие остаются.
        Сервер, до которого запрос не дошёл, отключается, и все его ответы
        устаревают; ошибка MCP или ответ не по схеме оставляют прежний ответ
        (caching: клиент вправе отдать устаревшее при сбое перечитывания).
        Удача подключает сервер и поднимает кончившийся поток смены списков."""
        try:
            await self._fetched()
        except McpUnreachableError as exc:
            self._disconnect(exc)
            return
        except McpRequestError as exc:
            logger.warning(
                "mcp: %s: re-reading its stale answers failed, the earlier "
                "answers stay: %s",
                self._label,
                exc,
            )
            return

        if not self._connected:
            logger.info("mcp: %s answers again, it is back in the session", self._label)
            self._changes.back(self._server.name)

        self._connected = True
        self._follow()

    async def close(self) -> None:
        """Поток смены списков закрывается."""
        listening = self._listening
        self._listening = None
        if listening is None:
            return

        listening.cancel()
        await asyncio.gather(listening, return_exceptions=True)

    def capabilities(self) -> mt.ServerCapabilities:
        return self._known().capabilities

    def instructions(self) -> str | None:
        """Инструкции сервера из server/discover; None — сервер их не дал."""
        return self._known().instructions

    def prompts(self) -> Sequence[mt.Prompt]:
        """Промпты подключённого сервера; отключённый — без промптов."""
        if not self._connected:
            return ()

        return self._prompts

    def for_model(self) -> bool:
        """Подключённый сервер отдаёт модели ресурсы или шаблоны ресурсов."""
        if not self._connected:
            return False

        return self._for_model

    async def model_resources(self) -> list[mt.Resource]:
        """Ресурсы сервера для модели по всем страницам resources/list:
        без audience либо с assistant в ней; сервер без ресурсов — пусто.

        Ошибки:
        McpRequestError — страница не получена либо не разобрана.
        """
        if self._known().capabilities.resources is None:
            return []

        listed = await self._paged(mt.ListResourcesRequest, mt.ListResourcesResult)

        return list(self._model_resources(listed.pages))

    async def model_templates(self) -> list[mt.ResourceTemplate]:
        """Шаблоны ресурсов сервера для модели по всем страницам
        resources/templates/list; сервер без ресурсов — пусто.

        Ошибки:
        McpRequestError — страница не получена либо не разобрана.
        """
        if self._known().capabilities.resources is None:
            return []

        listed = await self._paged(
            mt.ListResourceTemplatesRequest, mt.ListResourceTemplatesResult
        )

        return list(self._model_templates(listed.pages))

    def _model_resources(
        self, pages: Sequence[mt.ListResourcesResult]
    ) -> Iterator[mt.Resource]:
        for page in pages:
            for resource in page.resources:
                if self._for_assistant(resource.annotations):
                    yield resource

    def _model_templates(
        self, pages: Sequence[mt.ListResourceTemplatesResult]
    ) -> Iterator[mt.ResourceTemplate]:
        for page in pages:
            for template in page.resource_templates:
                if self._for_assistant(template.annotations):
                    yield template

    @staticmethod
    def _for_assistant(annotations: mt.Annotations | None) -> bool:
        """Ресурс адресован модели: audience не задана либо называет
        assistant; журнал вызова и иное «для человека» модели не идёт."""
        if annotations is None:
            return True

        return Audience.MODEL.reads(annotations.audience)

    def tools(self) -> Sequence[ToolCard]:
        """Инструменты подключённого сервера; отключённый — без инструментов."""
        if not self._connected:
            return ()

        return self._cards

    async def ask(
        self,
        request: mt.ClientRequest,
        result: type[ReplyT] | TypeAdapter[ReplyT],
    ) -> ReplyT:
        """Запрос MCP серверу через mcp/message со сроком списка и ответ
        моделью result.

        Ошибки:
        McpUnreachableError — клиент ответил ошибкой ACP или ответа нет в срок.
        McpRequestError — сервер ответил ошибкой MCP (код — в ошибке) либо
            ответ не по схеме.
        """
        params = request.params
        meta = self._meta.copy()
        if params is not None and params.meta is not None:
            meta.update(params.meta)

        sent = request.model_copy(update={"params": self._with_meta(params, meta)})
        message = McpMessage(
            server_id=self._server.server_id,
            request_id=self._ids.request_id(),
            **sent.model_dump(mode="json", by_alias=True, exclude_none=True),
        )
        what = f"{request.method} to {self._label}"
        limit = self._limits.list_timeout_sec
        try:
            async with asyncio.timeout(limit):
                outcome = await self._link.mcp_request(message, None)
        except TimeoutError as exc:
            msg = f"{what} expected an answer within {limit} s, got none"
            raise McpUnreachableError(msg) from exc
        except RequestError as exc:
            msg = (
                f"{what} expected a result, got ACP error {exc.code} {exc}: "
                f"{exc.data!r}"
            )
            raise McpUnreachableError(msg) from exc
        except AcpTransportError as exc:
            msg = f"{what} expected a result, got no answer: {exc}"
            raise McpUnreachableError(msg) from exc

        if isinstance(outcome, McpErrorOutcome):
            msg = (
                f"{what} expected a result, got MCP error {outcome.error.code}: "
                f"{outcome.error.message}"
            )
            raise McpRequestError(msg)

        try:
            if isinstance(result, TypeAdapter):
                return result.validate_python(outcome.result)

            return result.model_validate(outcome.result)
        except ValidationError as exc:
            shown = repr(outcome.result)[:200]
            msg = f"{what} expected a result by the schema, got {shown}: {exc}"
            raise McpRequestError(msg) from exc

    @staticmethod
    def _with_meta(
        params: mt.RequestParams | None, meta: mt.RequestParamsMeta
    ) -> mt.RequestParams:
        if params is None:
            return mt.RequestParams(_meta=meta)

        return params.model_copy(update={"meta": meta})

    def _known(self) -> mt.DiscoverResult:
        discovered = self._discovered
        if discovered is None:
            msg = f"{self._label}: it is asked before open(), it is not discovered"
            raise McpRequestError(msg)

        return discovered

    async def _fetched(self) -> None:
        """Устаревшие ответы — заново, по порядку: описание, затем списки по
        его возможностям.

        Ошибки:
        McpUnreachableError — запрос не дошёл до сервера.
        McpRequestError — сервер отказал ошибкой MCP, ответил не по схеме
            либо назвал инструмент не по форме полных имён.
        """
        if self._stale(McpListing.DISCOVER):
            discovered = await self.ask(
                mt.DiscoverRequest(params=mt.RequestParams()), mt.DiscoverResult
            )
            self._discovered = discovered
            self._fresh[McpListing.DISCOVER] = self._until(discovered)

        if self._stale(McpListing.TOOLS):
            await self._read_tools()

        if self._stale(McpListing.PROMPTS):
            await self._read_prompts()

        if self._stale(McpListing.RESOURCES):
            await self._read_resources()

    def _stale(self, listing: McpListing) -> bool:
        deadline = self._fresh.get(listing)
        if deadline is None:
            return True

        return time.monotonic() >= deadline

    def _until(self, answer: mt.CacheableResult) -> float:
        """Срок свежести ответа, полученного сейчас, по его ttlMs."""
        return time.monotonic() + answer.ttl_ms / self.MILLIS

    def _disconnect(self, cause: McpUnreachableError) -> None:
        """Сервер отключён до первого удачного чтения; все ответы устарели,
        человек получит пометку в начале хода."""
        if self._connected:
            logger.warning(
                "mcp: %s does not answer, its tools, prompts and instructions "
                "leave the session until it answers again: %s",
                self._label,
                cause,
            )
            self._changes.lost(self._server.name, cause)

        self._connected = False
        self._fresh.clear()

    async def _paged(
        self, request: type[ListRequest], result: type[PageT]
    ) -> McpPages[PageT]:
        """Все страницы списка и срок их общей свежести.

        Ошибки:
        McpUnreachableError, McpRequestError — от ask().
        """
        pages: list[PageT] = []
        fresh_until = float("inf")
        cursor: str | None = None
        while True:
            params = mt.PaginatedRequestParams(cursor=cursor)
            page = await self.ask(request(params=params), result)
            pages.append(page)
            fresh_until = min(fresh_until, self._until(page))
            cursor = page.next_cursor
            if cursor is None:
                return McpPages(pages=pages, fresh_until=fresh_until)

    async def _read_tools(self) -> None:
        """Карточки по всем страницам `tools/list`.

        Ошибки:
        McpUnreachableError, McpRequestError — страница не получена, не
            разобрана либо инструмент назван не по форме полных имён.
        """
        listed = await self._paged(mt.ListToolsRequest, mt.ListToolsResult)
        cards: list[ToolCard] = []
        for page in listed.pages:
            for tool in page.tools:
                try:
                    card = self._cards_of.card(self._server.name, tool)
                except McpToolNameError as exc:
                    msg = f"{self._label} lists a tool that is refused: {exc}"
                    raise McpRequestError(msg) from exc

                cards.append(card)

        self._cards = cards
        self._fresh[McpListing.TOOLS] = listed.fresh_until

    async def _read_prompts(self) -> None:
        """Промпты по всем страницам `prompts/list`; сервер без промптов —
        пусто без запроса.

        Ошибки:
        McpUnreachableError, McpRequestError — страница не получена либо не
            разобрана.
        """
        if self._known().capabilities.prompts is None:
            self._prompts = []
            return

        listed = await self._paged(mt.ListPromptsRequest, mt.ListPromptsResult)
        prompts: list[mt.Prompt] = []
        for page in listed.pages:
            prompts.extend(page.prompts)

        self._prompts = prompts
        self._fresh[McpListing.PROMPTS] = listed.fresh_until

    async def _read_resources(self) -> None:
        """Признак «сервер отдаёт модели ресурсы»: ресурсы, а без них —
        шаблоны ресурсов для модели; сервер без ресурсов — нет без запроса.

        Ошибки:
        McpUnreachableError, McpRequestError — страница не получена либо не
            разобрана.
        """
        if self._known().capabilities.resources is None:
            self._for_model = False
            return

        resources = await self._paged(mt.ListResourcesRequest, mt.ListResourcesResult)
        if any(self._model_resources(resources.pages)):
            self._for_model = True
            self._fresh[McpListing.RESOURCES] = resources.fresh_until
            return

        templates = await self._paged(
            mt.ListResourceTemplatesRequest, mt.ListResourceTemplatesResult
        )
        self._for_model = any(self._model_templates(templates.pages))
        fresh_until = min(resources.fresh_until, templates.fresh_until)
        self._fresh[McpListing.RESOURCES] = fresh_until

    def _follow(self) -> None:
        """Поток subscriptions/listen на смену списков, которую сервер обещал;
        поток уже идёт — ничего, кончился — поднимается заново, без обещаний
        потока нет."""
        listening = self._listening
        if listening is not None and not listening.done():
            return

        notifications = self._promised(self._known().capabilities)
        if notifications is None:
            return

        listen = mt.SubscriptionsListenRequest(
            params=mt.SubscriptionsListenRequestParams(
                notifications=notifications, _meta=self._meta
            )
        )
        message = McpMessage(
            server_id=self._server.server_id,
            request_id=self._ids.request_id(),
            **listen.model_dump(mode="json", by_alias=True, exclude_none=True),
        )
        name = self._server.name
        self._listening = asyncio.create_task(
            self._listen(message), name=f"mcp-listen-{name}"
        )

    @staticmethod
    def _promised(capabilities: mt.ServerCapabilities) -> mt.SubscriptionFilter | None:
        """Смены списков, которые сервер обещал; None — не обещал ни одной."""
        tools: bool | None = None
        if capabilities.tools is not None and capabilities.tools.list_changed:
            tools = True

        prompts: bool | None = None
        if capabilities.prompts is not None and capabilities.prompts.list_changed:
            prompts = True

        resources: bool | None = None
        if capabilities.resources is not None and capabilities.resources.list_changed:
            resources = True

        if tools is None and prompts is None and resources is None:
            return None

        return mt.SubscriptionFilter(
            tools_list_changed=tools,
            prompts_list_changed=prompts,
            resources_list_changed=resources,
        )

    async def _listen(self, message: McpMessage) -> None:
        """Поток живёт, пока его не закроет сервер или сессия. Конец потока —
        в лог, а списки устаревают: смену без потока увидит начало хода,
        которое поднимет и сам поток."""
        try:
            outcome = await self._link.mcp_request(message, self._heard)
        except (RequestError, AcpTransportError) as exc:
            self._expire_lists()
            logger.warning(
                "mcp: %s: the listen stream for list changes ended: %s",
                self._label,
                exc,
            )
            return

        self._expire_lists()
        if isinstance(outcome, McpErrorOutcome):
            logger.warning(
                "mcp: %s refused the listen stream: MCP error %s: %s",
                self._label,
                outcome.error.code,
                outcome.error.message,
            )
            return

        logger.info("mcp: %s closed the listen stream", self._label)

    def _expire_lists(self) -> None:
        for listing in (McpListing.TOOLS, McpListing.PROMPTS, McpListing.RESOURCES):
            self._fresh.pop(listing, None)

    async def _heard(self, message: McpMessage) -> None:
        """Уведомление потока подписки: смена списка делает его устаревшим.

        Ошибки:
        RequestError — уведомление не разбирается как уведомление сервера MCP
            (-32602).
        """
        params: Mapping[str, Any] = {}
        if message.params is not None:
            params = message.params

        try:
            notification = parse_server_notification(
                message.method, McpVersion.V2026_07_28, params
            )
        except (KeyError, ValidationError) as exc:
            details = (
                f"{self._label}: listen notification {message.method!r} expected "
                f"an MCP server notification, got {repr(params)[:200]}: {exc}"
            )
            raise RequestError.invalid_params(
                {ErrorDataKey.DETAILS.value: details}
            ) from exc

        match notification:
            case mt.SubscriptionsAcknowledgedNotification():
                logger.info(
                    "mcp: %s acknowledged list changes %s",
                    self._label,
                    notification.params.notifications,
                )
            case mt.ToolListChangedNotification():
                self._fresh.pop(McpListing.TOOLS, None)
            case mt.PromptListChangedNotification():
                self._fresh.pop(McpListing.PROMPTS, None)
            case mt.ResourceListChangedNotification():
                self._fresh.pop(McpListing.RESOURCES, None)
            case _:
                logger.info(
                    "mcp: %s sent %s on the listen stream; not followed",
                    self._label,
                    message.method,
                )

    async def submit(
        self, calls: Sequence[ToolUseBlock]
    ) -> Sequence[asyncio.Future[ToolReply]]:
        pending: list[asyncio.Future[ToolReply]] = []
        for call in calls:
            pending.append(asyncio.ensure_future(self._call(call)))

        return pending

    async def _call(self, call: ToolUseBlock) -> ToolReply:
        """Вызов `tools/call` с раундами ввода: просьбу сервера о вводе
        (input_required) агент задаёт пользователю формами и повторяет вызов
        с ответами и тем же requestState, но не больше `mcp.input_rounds`
        раз; раунды вопросов ложатся в `_meta` результата. Наружу идёт только
        отмена (контракт ToolServer)."""
        meta = mt.RequestParamsMeta(progress_token=call.id)
        meta.update(self._meta)
        params = mt.CallToolRequestParams(
            name=self._names.tool_of(call.name), arguments=dict(call.input), _meta=meta
        )
        rounds: list[ElicitationRound] = []
        asked = 0
        while True:
            reply = await self._requested(call, params)
            if isinstance(reply, ToolReply):
                return reply

            required = reply.parsed
            if isinstance(required, mt.CallToolResult):
                return self._finished(reply.raw, required, rounds)

            limit = self._limits.input_rounds
            if asked >= limit:
                return self._replies.refused(
                    f"{self._what(call)} asked for user input more than {limit} "
                    "times in one call (settings mcp.input_rounds); the call "
                    "is stopped, call the tool again with arguments that need "
                    "less input"
                )

            asked += 1
            answered = await self._answers(call, required, rounds)
            if isinstance(answered, ToolReply):
                return answered

            params = params.model_copy(
                update={
                    "input_responses": answered,
                    "request_state": required.request_state,
                }
            )

    def _what(self, call: ToolUseBlock) -> str:
        return (
            f"calling tool {call.name!r} (call {call.id}) on MCP server "
            f"'{self._server.name}' (serverId '{self._server.server_id}')"
        )

    async def _requested(
        self, call: ToolUseBlock, params: mt.CallToolRequestParams
    ) -> CallReply | ToolReply:
        """Один запрос `tools/call`: ответ сервера либо отказ, если ответа
        нет или он не по схеме."""
        request = mt.CallToolRequest(params=params)
        what = self._what(call)
        outcome = await self._sent(call, request)
        if isinstance(outcome, ToolReply):
            return outcome

        if isinstance(outcome, McpErrorOutcome):
            # отказ вызову может значить, что список инструментов сменился
            self._fresh.pop(McpListing.TOOLS, None)
            return self._replies.error(call.name, call.id, outcome.error)

        shown = repr(outcome.result)[:200]
        try:
            parsed = parse_server_result(
                request.method, McpVersion.V2026_07_28, outcome.result
            )
        except (ValidationError, KeyError, ValueError) as exc:
            return self._replies.refused(
                f"{what} expected a tools/call result, got {shown}: {exc}"
            )

        if isinstance(parsed, mt.CallToolResult | mt.InputRequiredResult):
            return CallReply(raw=outcome.result, parsed=parsed)

        return self._replies.refused(
            f"{what} expected a tools/call result, got {type(parsed).__name__}: {shown}"
        )

    async def _sent(
        self, call: ToolUseBlock, request: mt.CallToolRequest
    ) -> McpOutcome | ToolReply:
        """Запрос mcp/message с ходом вызова в срок `call_timeout_sec`; итог —
        исход MCP либо отказ, если ответа нет. Без ответа все ответы сервера
        устаревают: доступность проверит начало следующего хода."""
        message = McpMessage(
            server_id=self._server.server_id,
            request_id=self._ids.request_id(),
            **request.model_dump(mode="json", by_alias=True, exclude_none=True),
        )
        relay = CallProgressRelay(self._sink, call.id)
        limit = self._limits.call_timeout_sec
        what = self._what(call)
        try:
            async with asyncio.timeout(limit):
                return await self._link.mcp_request(message, relay.accept)
        except TimeoutError:
            self._fresh.clear()
            return self._replies.refused(
                f"{what} expected a tools/call result within {limit} s, got none"
            )
        except RequestError as exc:
            self._fresh.clear()
            return self._replies.refused(
                f"{what} expected a tools/call result, got ACP error {exc.code} "
                f"{exc}: {exc.data!r}"
            )
        except AcpTransportError as exc:
            self._fresh.clear()
            return self._replies.refused(
                f"{what} expected a tools/call result, got no answer: {exc}"
            )

    def _finished(
        self, raw: Any, result: mt.CallToolResult, rounds: Sequence[ElicitationRound]
    ) -> ToolReply:
        """Итог вызова по результату сервера: ответ как пришёл, с раундами
        вопросов в `_meta`."""
        if not rounds:
            return self._replies.received(raw, result)

        meta: dict[str, Any] = {}
        if result.meta is not None:
            meta.update(result.meta)

        dumped: list[dict[str, Any]] = []
        for asked in rounds:
            dumped.append(
                asked.model_dump(mode="json", by_alias=True, exclude_none=True)
            )

        meta[MetaKey.ELICITATIONS.value] = dumped
        shown = result.model_copy(update={"meta": meta})

        return self._replies.received(self._replies.dumped(shown), shown)

    async def _answers(
        self,
        call: ToolUseBlock,
        required: mt.InputRequiredResult,
        rounds: list[ElicitationRound],
    ) -> dict[str, mt.InputResponse] | ToolReply:
        """Ответы пользователя на просьбы сервера по их ключам: каждая форма
        уходит клиенту `elicitation/create` с именем сервера и инструмента в
        `_meta`. Выборку (sampling), корни (roots) и форму-ссылку агент не
        выполняет — вызов кончается отказом с причиной."""
        what = self._what(call)
        tool = self._names.tool_of(call.name)
        meta = {MetaKey.SERVER.value: self._server.name, MetaKey.TOOL.value: tool}
        requests: Mapping[str, mt.InputRequest] = {}
        if required.input_requests is not None:
            requests = required.input_requests

        responses: dict[str, mt.InputResponse] = {}
        for key, request in requests.items():
            if not isinstance(request, mt.ElicitRequest):
                return self._replies.refused(
                    f"{what} asked the client for {request.method} (input "
                    f"request {key!r}); this agent answers only elicitation "
                    "forms, sampling and roots are not supported"
                )

            form = request.params
            if not isinstance(form, mt.ElicitRequestFormParams):
                return self._replies.refused(
                    f"{what} asked the user to open a URL (input request "
                    f"{key!r}); this agent declared form elicitation only"
                )

            try:
                requested = self._mapping.server_form(form.requested_schema)
            except ValidationError as exc:
                return self._replies.refused(
                    f"{what} asked a form (input request {key!r}) that the "
                    f"client cannot show: {exc}"
                )

            answer = await self._relay.elicit(call.id, form.message, requested, meta)
            content: dict[str, FormValue] = {}
            if isinstance(answer, schema.AcceptElicitationResponse):
                if answer.content is not None:
                    content = dict(answer.content)

            result = self._mapping.server_answer(answer)
            rounds.append(
                ElicitationRound(
                    server=self._server.name,
                    tool=tool,
                    message=form.message,
                    requested=requested,
                    action=result.action,
                    content=content,
                )
            )
            responses[key] = result

        return responses


@dataclass(frozen=True)
class CallReply:
    """Ответ сервера на один запрос `tools/call`: как пришёл и разобранный."""

    raw: Any
    parsed: mt.CallToolResult | mt.InputRequiredResult


class ResourceToolName(StrEnum):
    """Собственные инструменты агента над ресурсами серверов MCP сессии."""

    LIST = "list_mcp_resources"
    READ = "read_mcp_resource"


class ResourceListArgs(BaseModel):
    """Аргументы list_mcp_resources: сервер; пусто — все серверы с
    ресурсами."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    server: str = Field(
        default="",
        description="Name of the MCP server; empty lists every server with resources.",
    )


class ResourceReadArgs(BaseModel):
    """Аргументы read_mcp_resource: сервер и uri ресурса или раскрытого
    шаблона."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    server: str = Field(min_length=1, description="Name of the MCP server.")
    uri: str = Field(
        min_length=1,
        description="URI of a listed resource or of an expanded resource template.",
    )


class McpResourceTools(ToolServer):
    """Реализация ToolServer: ресурсы серверов MCP сессии собственными
    инструментами агента для модели.

    Создаётся SessionMcp над живым списком её серверов; инструменты у неё есть,
    пока хоть один подключённый сервер отдаёт модели ресурсы (audience без
    assistant — для человека, как журнал вызова, модели не идёт): признак
    сервер обновляет в начале хода. list_mcp_resources отдаёт модели ресурсы и шаблоны
    ресурсов серверов (resources/list, resources/templates/list всеми
    страницами),
    read_mcp_resource читает ресурс по uri (resources/read): текст — как
    есть, двоичное содержимое — пометкой с типом и размером. Сервер,
    которого нет в сессии или у которого нет ресурсов, и ошибка сервера —
    отказ вызову с причиной для модели.
    """

    def __init__(self, servers: Sequence[AcpToolServer]) -> None:
        self._servers = servers
        self._replies = ToolReplies()
        self._errors = ValidationText()
        hints = ToolHints(read_only=True, destructive=False)
        self._cards = (
            ToolCard(
                name=ResourceToolName.LIST.value,
                description=(
                    "List the resources and resource templates of the MCP servers "
                    "of the session:\n"
                    "   - server — name of one server; empty lists all servers"
                ),
                parameters=ResourceListArgs.model_json_schema(),
                hints=hints,
            ),
            ToolCard(
                name=ResourceToolName.READ.value,
                description=(
                    "Read a resource of an MCP server of the session:\n"
                    "   - server — name of the server\n"
                    "   - uri — URI of a listed resource or of an expanded template"
                ),
                parameters=ResourceReadArgs.model_json_schema(),
                hints=hints,
            ),
        )

    def tools(self) -> Sequence[ToolCard]:
        """Инструменты ресурсов, если хоть один сервер отдаёт модели ресурсы."""
        for server in self._servers:
            if server.for_model():
                return self._cards

        return ()

    async def submit(
        self, calls: Sequence[ToolUseBlock]
    ) -> Sequence[asyncio.Future[ToolReply]]:
        pending: list[asyncio.Future[ToolReply]] = []
        for call in calls:
            pending.append(asyncio.ensure_future(self._call(call)))

        return pending

    async def _call(self, call: ToolUseBlock) -> ToolReply:
        """Один вызов; наружу идёт только отмена (контракт ToolServer)."""
        try:
            if call.name == ResourceToolName.LIST.value:
                listed = ResourceListArgs.model_validate(call.input)
                text = await self._listed(listed)
            else:
                read = ResourceReadArgs.model_validate(call.input)
                text = await self._read(read)
        except ValidationError as exc:
            return self._replies.refused(
                f"{call.name}: the arguments are invalid: {self._errors.of(exc)}"
            )
        except McpRequestError as exc:
            return self._replies.refused(f"{call.name}: {exc}")

        return self._replies.made(text, None, False)

    async def _listed(self, args: ResourceListArgs) -> str:
        """Ресурсы и шаблоны серверов для модели строками.

        Ошибки:
        McpRequestError — сервера нет в сессии, ресурсов для модели у него нет
            либо он не ответил.
        """
        servers = list(self._servers)
        if args.server:
            servers = [self._server(args.server)]

        lines: list[str] = []
        for server in servers:
            for resource in await server.model_resources():
                lines.append(self._line(server.name, resource))

            for template in await server.model_templates():
                lines.append(self._template_line(server.name, template))

        if not lines:
            return "The MCP servers of the session list no resources."

        return "\n".join(lines)

    async def _read(self, args: ResourceReadArgs) -> str:
        """Содержимое ресурса текстом для модели.

        Ошибки:
        McpRequestError — сервера нет в сессии, у него нет ресурсов, он
            ответил ошибкой либо попросил ввода.
        """
        server = self._server(args.server)
        params = mt.ReadResourceRequestParams(uri=args.uri)
        request = mt.ReadResourceRequest(params=params)
        read = await server.ask(request, InputReplies.READ)
        if isinstance(read, mt.InputRequiredResult):
            msg = (
                f"resources/read {args.uri} on server {args.server!r}: the server "
                "asked for more input, and this tool cannot answer such requests"
            )
            raise McpRequestError(msg)

        parts: list[str] = []
        for content in read.contents:
            if isinstance(content, mt.TextResourceContents):
                parts.append(content.text)
                continue

            parts.append(
                f"[blob {content.uri} {content.mime_type}, "
                f"{len(content.blob)} base64 characters]"
            )

        return "\n".join(parts)

    def _server(self, name: str) -> AcpToolServer:
        """Ошибки:
        McpRequestError — сервера нет в сессии либо ресурсов у него нет.
        """
        server = self._named(name)
        if server is None:
            names: list[str] = []
            for known in self._servers:
                names.append(known.name)

            msg = (
                f"server {name!r} is not an MCP server of the session; expected one "
                f"of {sorted(names)}"
            )
            raise McpRequestError(msg)

        if not server.for_model():
            msg = f"MCP server {name!r} offers no resources to the model"
            raise McpRequestError(msg)

        return server

    def _named(self, name: str) -> AcpToolServer | None:
        for server in self._servers:
            if server.name == name:
                return server

        return None

    @staticmethod
    def _line(server: str, resource: mt.Resource) -> str:
        words = [f"{server}: {resource.uri}", resource.name]
        if resource.mime_type:
            words.append(resource.mime_type)

        if resource.description:
            words.append(f"— {resource.description}")

        return " ".join(words)

    @staticmethod
    def _template_line(server: str, template: mt.ResourceTemplate) -> str:
        words = [f"{server}: template {template.uri_template}", template.name]
        if template.description:
            words.append(f"— {template.description}")

        return " ".join(words)


class InputReplies:
    """Модели ответов MCP с просьбой о вводе: агент принимает их в ask() и
    решает сам, что делать с input_required."""

    READ: ClassVar[TypeAdapter[mt.ReadResourceResult | mt.InputRequiredResult]] = (
        TypeAdapter(mt.ReadResourceResult | mt.InputRequiredResult)
    )
    PROMPT: ClassVar[TypeAdapter[mt.GetPromptResult | mt.InputRequiredResult]] = (
        TypeAdapter(mt.GetPromptResult | mt.InputRequiredResult)
    )


class SessionMcp(McpServerSet, ToolServer):
    """MCP-серверы одной сессии ACP: регистрации `type: "acp"` клиента;
    реализация McpServerSet и ToolServer ядра.

    Создаётся AcpAgent на `session/new|resume|fork` до сборки агента и живёт
    с сессией. Как ToolServer отдаёт модели инструменты серверов и
    McpResourceTools и ведёт вызов тому серверу, чей это инструмент; как
    McpServerSet — свежесть серверов в начале хода и подключённые серверы
    с их инструкциями: инструкции модель получает вложением хода, а не
    системным промптом. Вопрос `/<полное имя промпта> <аргументы>`
    раскрывается prompts/get в служебный контекст вопроса; объявляют
    промпты клиенту сами серверы, не агент. Сервер, который не открылся, в
    сессию не входит; смены состояния серверов (не открылся, пропал,
    вернулся) копит ServerNotices, и ход пишет их человеку пометками в
    историю. Закрытие сессии закрывает потоки подписки серверов.
    """

    def __init__(self, relay: SessionRelay) -> None:
        self._relay = relay
        self._servers: list[AcpToolServer] = []
        self._resources = McpResourceTools(self._servers)
        self._names = McpToolNames()
        self._replies = ToolReplies()
        self._lines = CommandLines()
        self._announcing: asyncio.Task[None] | None = None
        self.changes = ServerNotices()
        """Смены состояния серверов до следующего хода; их копят и серверы."""

    def add(self, server: AcpToolServer) -> None:
        self._servers.append(server)

    def unavailable(self, server: str, failure: BaseException) -> None:
        """Сервер server не открылся: сессия идёт без него, первый ход
        запишет человеку пометку."""
        logger.warning("mcp: server %s is left out of the session: %s", server, failure)
        self.changes.unavailable(server, failure)

    def tools(self) -> Sequence[ToolCard]:
        """Инструменты серверов и инструменты ресурсов; имена, которые
        предложены дважды, отвергает агент."""
        offered: list[ToolCard] = []
        for server in self._offering():
            offered.extend(server.tools())

        return offered

    async def submit(
        self, calls: Sequence[ToolUseBlock]
    ) -> Sequence[asyncio.Future[ToolReply]]:
        """Вызовы пакета к одному серверу уходят ему одним пакетом; вызов
        инструмента, которого нет ни у кого, получает отказ со списком."""
        routed: dict[int, list[int]] = {}
        pending: dict[int, asyncio.Future[ToolReply]] = {}
        servers = list(self._offering())
        for position, call in enumerate(calls):
            owner = self._owner(servers, call.name)
            if owner is None:
                pending[position] = self._settled(self._unknown(servers, call.name))
                continue

            routed.setdefault(owner, []).append(position)

        for owner, positions in routed.items():
            batch: list[ToolUseBlock] = []
            for position in positions:
                batch.append(calls[position])

            accepted = await servers[owner].submit(batch)
            for position, future in zip(positions, accepted, strict=True):
                pending[position] = future

        ordered: list[asyncio.Future[ToolReply]] = []
        for position in range(len(calls)):
            ordered.append(pending[position])

        return ordered

    def _offering(self) -> Iterator[ToolServer]:
        yield from self._servers
        yield self._resources

    @staticmethod
    def _owner(servers: Sequence[ToolServer], name: str) -> int | None:
        for index, server in enumerate(servers):
            for card in server.tools():
                if card.name == name:
                    return index

        return None

    def _unknown(self, servers: Sequence[ToolServer], name: str) -> ToolReply:
        known: list[str] = []
        for server in servers:
            for card in server.tools():
                known.append(card.name)

        return self._replies.refused(
            f"tool {name!r} does not exist; the available tools are {sorted(known)}"
        )

    @staticmethod
    def _settled(reply: ToolReply) -> asyncio.Future[ToolReply]:
        done: asyncio.Future[ToolReply] = asyncio.get_running_loop().create_future()
        done.set_result(reply)

        return done

    async def refreshed(self) -> Sequence[ServerNotice]:
        """Устаревшее у серверов — заново, у всех разом. Итог — смены
        состояния серверов с прошлого хода."""
        pending: list[Awaitable[None]] = []
        for server in self._servers:
            pending.append(server.refreshed())

        await asyncio.gather(*pending)

        return self.changes.taken()

    def connected(self) -> Sequence[ConnectedServer]:
        return list(self._connected())

    def _connected(self) -> Iterator[ConnectedServer]:
        for server in self._servers:
            if not server.connected:
                continue

            instructions = server.instructions()
            if instructions is None:
                instructions = ""

            yield ConnectedServer(name=server.name, instructions=instructions)

    def announce_later(self, commands: schema.AvailableCommandsUpdate) -> None:
        """Команды сессии агента клиенту отдельной задачей, после ответа на
        запрос сессии: о сессии, которую клиент ещё не знает, он уведомлений
        не ждёт."""
        self._announcing = asyncio.create_task(
            self._relay.notify(commands), name="session-commands"
        )

    async def expanded(self, asked: QueueEntry) -> QueueEntry:
        """Вопрос `/<команда> <аргументы>` промпта сервера — тот же вопрос с
        сообщениями промпта (prompts/get) в служебном контексте; прочий
        вопрос — как есть.

        Ошибки:
        RequestError — аргументов не хватает или они не разбираются
            (-32602), сервер не отдал промпт (-32603).
        """
        call = self._lines.parsed(asked.text)
        if call is None:
            return asked

        found = await self._prompt_named(call.name)
        if found is None:
            return asked

        server, prompt = found
        command = f"/{call.name}"
        try:
            arguments = self._lines.arguments(call, self._declared(prompt))
        except CommandArgumentsError as exc:
            raise RequestError.invalid_params(
                {ErrorDataKey.DETAILS.value: str(exc)}
            ) from exc

        params = mt.GetPromptRequestParams(name=prompt.name, arguments=arguments)
        request = mt.GetPromptRequest(params=params)
        try:
            reply = await server.ask(request, InputReplies.PROMPT)
        except McpRequestError as exc:
            details = f"command {command}: prompts/get failed: {exc}"
            raise RequestError.internal_error(
                {ErrorDataKey.DETAILS.value: details}
            ) from exc

        if isinstance(reply, mt.InputRequiredResult):
            details = (
                f"command {command}: the server asked for more input to give the "
                "prompt, and this agent cannot answer such requests"
            )
            raise RequestError.internal_error({ErrorDataKey.DETAILS.value: details})

        context = [*asked.context, self._replies.prompt(reply)]

        return asked.model_copy(update={"context": context})

    async def close(self) -> None:
        announcing = self._announcing
        self._announcing = None
        if announcing is not None:
            announcing.cancel()
            await asyncio.gather(announcing, return_exceptions=True)

        for server in self._servers:
            await server.close()

    async def _prompt_named(self, name: str) -> tuple[AcpToolServer, mt.Prompt] | None:
        """Промпт сервера по полному имени; None — такого нет. Имени нет в
        списках — устаревшие списки перечитываются: сервер мог сменить
        промпты после начала хода."""
        found = self._prompt_of(name)
        if found is not None:
            return found

        if self._names.parse(name) is None:
            return None

        await self.refreshed()

        return self._prompt_of(name)

    def _prompt_of(self, name: str) -> tuple[AcpToolServer, mt.Prompt] | None:
        for server in self._servers:
            for prompt in server.prompts():
                if self._names.full(server.name, prompt.name) == name:
                    return server, prompt

        return None

    @staticmethod
    def _declared(prompt: mt.Prompt) -> Sequence[CommandArgument]:
        """Аргументы промпта по порядку; промпт без аргументов — пусто."""
        declared = prompt.arguments
        if declared is None:
            return ()

        arguments: list[CommandArgument] = []
        for argument in declared:
            required = bool(argument.required)
            arguments.append(CommandArgument(name=argument.name, required=required))

        return arguments


class SessionRelay(EventSink):
    """Реализация EventSink сессии ACP: события ядра → уведомления клиенту.

    Создаёт вход на каждую сессию до сборки агента и отдаёт ядру
    приёмником; агент привязывается после открытия (`attach`), потому что
    пометки инструментов, заполненность контекста и ответы на вопросы живут
    у него. Первое событие хода шлёт `state_update` `running`, итог хода —
    `usage_update`, после первого вопроса — `session_info_update` и затем
    `idle`; каждый сброс записей в историю — `session_info_update` с
    `updatedAt`, в ходе и вне его. Вопрос `can_use_tool` уходит клиенту
    отдельной задачей, которую снимает отмена хода: разрешение —
    `session/request_permission`, вопрос AskUserQuestion — формой
    `elicitation/create` с toolCallId вызова, если клиент объявил формы
    (elicits); без них вопрос остаётся без ответа.
    На время вопроса сессия в `requires_action`. После ухода клиента
    приёмник отвязывается и дальше молчит.
    """

    def __init__(
        self, session_id: str, link: ClientLink, mapping: AcpMapping, elicits: bool
    ) -> None:
        self._session_id = session_id
        self._link = link
        self._mapping = mapping
        self._elicits = elicits
        self._titles = SessionTitles()
        self._agent: Agent | None = None
        self._asks: dict[str, asyncio.Task[None]] = {}
        self._detached = False
        self._titled = False
        self._running = False
        self._inserting: dict[str, asyncio.Future[None]] = {}
        """Вопрос `session/prompt` в очереди → ожидание его записи `user`."""
        self._streamed: str | None = None
        """messageId ответа, который сейчас стримится и ещё не записан."""

    def attach(self, agent: Agent) -> None:
        self._agent = agent

    def insertion(self, message_id: str) -> asyncio.Future[None]:
        """Ожидание вставки вопроса message_id: его решает запись `user` с
        этим uuid, отказ — refuse."""
        inserted: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._inserting[message_id] = inserted

        return inserted

    def refuse(self, message_id: str, error: RequestError) -> None:
        """Вопрос message_id снят до вставки: его запрос получит error."""
        inserted = self._inserting.pop(message_id, None)
        if inserted is None:
            return

        if inserted.done():
            return

        inserted.set_exception(error)

    def refuse_all(self, error: RequestError) -> None:
        for message_id in list(self._inserting):
            self.refuse(message_id, error)

    @property
    def elicits(self) -> bool:
        """Клиент объявил формы `elicitation.form`."""
        return self._elicits

    async def notify(self, update: Update) -> None:
        """Уведомление вне хода: смена настроек сессии."""
        await self._send(update)

    async def announce_loaded(self) -> None:
        """После воспроизведения сессии: заголовок и заполненность
        контекста, как после хода."""
        await self._send_all(self._turn_summary())

    async def failed(self) -> None:
        """Обслуживание сессии сорвалось: пометка и `idle` отказом; причину
        пишет в журнал тот, кто заметил сбой."""
        self._running = False
        await self._send_all(self._mapping.serving_stopped())

    async def emit(self, event: AgentEvent) -> None:
        if isinstance(event, ResultEvent):
            await self._send_all(self._turn_summary())
            self._running = False
            ending = list(self._withdrawn())
            ending.extend(self._mapping.turn_ended(event.turn_end))
            await self._send_all(ending)
            return

        if isinstance(event, ErrorEvent):
            logger.warning("session %s: %s", self._session_id, event.message)
            await self._send(self._mapping.notice(NoticeLevel.ERROR, event.message, ""))
            return

        if isinstance(event, HistoryWrittenEvent):
            await self._send(self._mapping.activity_update(event))
            return

        updates = self._updates(event)
        if updates is None:
            self._control(event)
            return

        if not updates:
            return

        if not self._running:
            self._running = True
            await self._send(schema.RunningSessionStateUpdate())

        await self._send_all(updates)

    def _turn_summary(self) -> Sequence[Update]:
        """Уведомления по итогу хода: заполненность и, однажды, заголовок."""
        updates: list[Update] = []
        usage = self._usage()
        if usage is not None:
            updates.append(usage)

        title = self._title()
        if title is not None:
            updates.append(title)

        return updates

    def _usage(self) -> schema.UsageUpdate | None:
        if self._agent is None:
            return None

        return self._mapping.usage_update(self._agent.context_usage())

    def _title(self) -> schema.SessionInfoUpdate | None:
        """Заголовок из первого вопроса человека; уходит один раз."""
        if self._titled or self._agent is None:
            return None

        first = self._first_prompt(self._agent.session.conversation())
        title = self._titles.title(first)
        if title is None:
            return None

        self._titled = True

        return self._mapping.title_update(title)

    def _first_prompt(self, conversation: Sequence[ChainRecord]) -> str | None:
        for record in conversation:
            if not isinstance(record, UserRecord):
                continue

            text = self._mapping.prompt_text(record)
            if text is not None:
                return text

        return None

    def _updates(self, event: AgentEvent) -> Sequence[Update] | None:
        """Уведомления хода по событию; None — событие не про ленту."""
        if isinstance(event, StreamEvent):
            self._streamed = event.message_id
            return self._mapping.chunks(event)

        if isinstance(event, RecordEvent):
            return self._record_updates(event.record)

        if isinstance(event, ToolStartedEvent):
            return [self._mapping.tool_started(event)]

        if isinstance(event, ToolProgressEvent):
            return [self._mapping.tool_progress(event)]

        return None

    def _control(self, event: AgentEvent) -> None:
        if isinstance(event, ControlRequestEvent):
            self._ask(event)
            return

        if isinstance(event, ControlCancelEvent):
            self.withdraw(event.request_id)
            return

        if isinstance(event, InitEvent):
            logger.info(
                "session %s: opened with model %s and %d tool(s)",
                event.session_id,
                event.model,
                len(event.tools),
            )

    def _record_updates(self, record: Record) -> Sequence[Update]:
        if not isinstance(record, ChainRecord):
            return ()

        updates: list[Update] = []
        if self._failed_attempt(record):
            updates.extend(self._withdrawn())

        updates.extend(self._mapping.notices(record))
        if isinstance(record, AssistantRecord):
            self._streamed = None
            for block in record.message.tool_uses():
                updates.append(self._mapping.tool_call(block, self._hints(block.name)))

            return updates

        if isinstance(record, CompactionRecord):
            return [self._mapping.compaction(record)]

        if isinstance(record, CompactSummaryRecord):
            return self._compacted(record)

        if isinstance(record, UserRecord):
            self._inserted(record.uuid)
            updates.extend(self._mapping.user_updates(record))
            return updates

        return updates

    @staticmethod
    def _failed_attempt(record: ChainRecord) -> bool:
        """Запись о неудачной попытке запроса к модели перед повтором."""
        return isinstance(record, ApiErrorRecord)

    def _withdrawn(self) -> Iterator[Update]:
        """Неудачная попытка запроса к модели: её недописанный ответ и
        рассуждение заменяются пустыми — в журнале их нет, и лента клиента
        совпадёт с воспроизведением."""
        streamed = self._streamed
        if streamed is None:
            return

        self._streamed = None
        yield schema.AgentThoughtUpdate(message_id=streamed, content=[])
        yield schema.AgentMessageUpdate(message_id=streamed, content=[])

    def _compacted(self, summary: CompactSummaryRecord) -> Sequence[Update]:
        """Состоявшееся сжатие и заполненность после него."""
        updates: list[Update] = [self._mapping.compacted(summary)]
        usage = self._usage()
        if usage is not None:
            updates.append(usage)

        return updates

    def _hints(self, name: str) -> ToolHints | None:
        if self._agent is None:
            return None

        return self._agent.tool_hints(name)

    async def _send_all(self, updates: Sequence[Update]) -> None:
        for update in updates:
            await self._send(update)

    async def _send(self, update: Update) -> None:
        if self._detached:
            return

        try:
            await self._link.session_update(self._session_id, update)
        except (ConnectionError, AcpTransportError) as exc:
            self._detached = True
            logger.warning(
                "session %s: the client connection is closed (%s); further "
                "updates of this session are dropped",
                self._session_id,
                exc,
            )

    def _ask(self, event: ControlRequestEvent) -> None:
        name = f"acp-ask-{event.request_id}"
        if event.request.tool_name == AskUserQuestionTool.NAME:
            task = asyncio.create_task(self._question(event), name=name)
        else:
            task = asyncio.create_task(self._permission(event), name=name)

        self._asks[event.request_id] = task

    async def _question(self, event: ControlRequestEvent) -> None:
        """Вопрос AskUserQuestion пользователю формой; ответ формы — ответ
        ядру."""
        agent = self._agent
        if agent is None:
            msg = "A question arrived before the agent was attached."
            raise RuntimeError(msg)

        try:
            answer = await self._answered(event)
        finally:
            self._asks.pop(event.request_id, None)

        taken = agent.answer(event.request_id, answer)
        if not taken:
            logger.info(
                "session %s: the answer to question %s came after the turn moved on",
                self._session_id,
                event.request_id,
            )

    async def _answered(self, event: ControlRequestEvent) -> PermissionAnswer:
        """Ответ формы на вопрос; ввод не по схеме AskUserQuestion — вопрос
        без ответа с записью в журнал."""
        request = event.request
        try:
            form = self._mapping.question_form(request)
        except ValidationError as exc:
            logger.error(
                "session %s: question %s of call %s is not an AskUserQuestion input "
                "and is left unanswered: %s",
                self._session_id,
                event.request_id,
                request.tool_use_id,
                exc,
            )
            return AllowAnswer()

        response = await self.elicit(
            request.tool_use_id, form.message, form.requested, {}
        )

        return self._mapping.question_answer(response, form)

    async def elicit(
        self,
        tool_call_id: str,
        message: str,
        requested: schema.ElicitationSchema,
        meta: Mapping[str, Any],
    ) -> ElicitationAnswer:
        """Форма `elicitation/create` клиенту в рамках вызова tool_call_id:
        сессия на время вопроса в `requires_action`. Клиент без форм, ушедший
        клиент и ответ ошибкой — отмена (`cancel`) с записью в журнал; снятие
        вопроса отменой хода снимает и запрос."""
        if not self._elicits:
            logger.info(
                "session %s: call %s asks the user, but the client declared no "
                "form elicitation; the question is cancelled",
                self._session_id,
                tool_call_id,
            )
            return schema.CancelElicitationResponse()

        await self._send(schema.RequiresActionSessionStateUpdate())
        try:
            response = await self._link.create_elicitation(
                message, self._session_id, tool_call_id, requested, meta
            )
        except ConnectionError as exc:
            logger.warning(
                "session %s: the question of call %s cannot be asked, the client "
                "is gone (%s); it is cancelled",
                self._session_id,
                tool_call_id,
                exc,
            )
            return schema.CancelElicitationResponse()
        except RequestError as exc:
            logger.warning(
                "session %s: the client answered the question of call %s with an "
                "error (%s); it is cancelled",
                self._session_id,
                tool_call_id,
                exc,
            )
            return schema.CancelElicitationResponse()

        await self._send(schema.RunningSessionStateUpdate())

        return response

    async def _permission(self, event: ControlRequestEvent) -> None:
        agent = self._agent
        if agent is None:
            msg = "A permission question arrived before the agent was attached."
            raise RuntimeError(msg)

        request = event.request
        ask = self._mapping.permission_request(request, self._hints(request.tool_name))
        try:
            response = await self._link.request_permission(self._session_id, ask)
            answer = self._mapping.permission_answer(response, request)
        except ConnectionError as exc:
            logger.warning(
                "session %s: permission for %s cannot be asked, the client is gone "
                "(%s); the call is rejected",
                self._session_id,
                request.tool_name,
                exc,
            )
            answer = DenyAnswer()
        except RequestError as exc:
            logger.warning(
                "session %s: the client answered the permission request for %s "
                "with an error (%s); the call is rejected",
                self._session_id,
                request.tool_name,
                exc,
            )
            answer = DenyAnswer()
        finally:
            self._asks.pop(event.request_id, None)

        taken = agent.answer(event.request_id, answer)
        if not taken:
            logger.info(
                "session %s: the answer to question %s came after the turn moved on",
                self._session_id,
                event.request_id,
            )

    def withdraw(self, request_id: str) -> None:
        """Вопрос снят ядром: ответ клиента больше не нужен."""
        task = self._asks.pop(request_id, None)
        if task is not None:
            task.cancel()

    def withdraw_all(self) -> None:
        for request_id in list(self._asks):
            self.withdraw(request_id)

    def _inserted(self, message_id: str) -> None:
        inserted = self._inserting.pop(message_id, None)
        if inserted is None:
            return

        if inserted.done():
            return

        inserted.set_result(None)


class OpenedSession(Protocol):
    """Открытая сессия сборки: агент ядра, действующий профиль модели и всё,
    что надо закрыть вместе с ними."""

    @property
    @abstractmethod
    def agent(self) -> Agent: ...

    @property
    @abstractmethod
    def model(self) -> str:
        """Имя действующего профиля модели (`models.<имя>` настроек)."""

    @abstractmethod
    async def set_model(self, name: str) -> None:
        """Переключает профиль модели агента для следующих ходов.

        Ошибки:
        SessionRefusedError — профиля с таким именем нет.
        SessionBusyError — ход идёт.
        SessionOpenError — профиль или его провайдер не годятся.
        """

    @abstractmethod
    async def close(self) -> None:
        """Гасит ход и сбрасывает журнал."""


class ObservedSession(ToolHintSource, Protocol):
    """Сессия, открытая без замка: её держит другой процесс. Записи в
    хранилище есть, инструменты подключены; `claim` открывает сессию
    по-настоящему, когда замок свободен."""

    @abstractmethod
    async def claim(self) -> OpenedSession:
        """Ошибки:
        SessionHeldError — замок всё ещё у другого процесса.
        SessionNotFoundError — записи сессии исчезли из хранилища.
        SessionOpenError — сессию открыть не удалось.
        """


class SessionAssembly(Protocol):
    """Сборка сессии из настроек процесса и инструментов
    `session/new|resume|fork`, список и удаление сессий хранилища.

    Реализует вход процесса: он знает профиль модели и хранилище истории;
    протокольный слой знает только события, ответы и инструменты.
    """

    @abstractmethod
    async def open(  # noqa: PLR0913 — сессия собирается всеми частями запроса
        self,
        session_id: SessionId,
        cwd: Path,
        tools: ToolServer | None,
        servers: McpServerSet,
        sink: EventSink,
        resume: bool,
        questions: bool,
    ) -> OpenedSession:
        """servers — серверы MCP сессии: свежесть их списков и инструкции на
        каждый ход; questions — клиент объявил формы, и модели предлагается
        собственный инструмент «вопрос пользователю».

        Ошибки:
        SessionOpenError — сессию открыть не удалось.
        SessionHeldError — при возобновлении: сессию держит другой процесс.
        SessionNotFoundError — при возобновлении: записей сессии нет.
        """

    @abstractmethod
    async def observe(  # noqa: PLR0913 — наблюдатель собирается всеми частями сессии
        self,
        session_id: SessionId,
        cwd: Path,
        tools: ToolServer | None,
        servers: McpServerSet,
        sink: EventSink,
        questions: bool,
    ) -> ObservedSession:
        """Ошибки:
        SessionNotFoundError — записей сессии нет.
        HistoryError — хранилище отказало при чтении.
        """

    @abstractmethod
    def history(self, session_id: SessionId) -> AsyncGenerator[ChainRecord, None]:
        """Записи цепочки сессии из хранилища потоком, по одной: из них
        воспроизводится беседа.

        Ошибки:
        HistoryError — хранилище отказало.
        """

    @abstractmethod
    def models(self) -> Sequence[str]:
        """Имена профилей `models.<имя>` в порядке файла настроек."""

    @abstractmethod
    async def sessions(self, cursor: str | None) -> SessionPage:
        """Ошибки:
        HistoryError — хранилище отказало.
        """

    @abstractmethod
    async def delete(self, session_id: SessionId) -> None:
        """Ошибки:
        SessionHeldError — сессию держит работающий процесс.
        HistoryError — хранилище отказало.
        """

    @abstractmethod
    async def copy(
        self, source: SessionId, target: SessionId, up_to: str | None
    ) -> int:
        """Записи `source` до записи `up_to` (не включая; None — все) под
        идентификатором `target`; возвращает число скопированных записей.

        Ошибки:
        SessionNotFoundError — у источника нет записей.
        RecordNotFoundError — записи `up_to` в источнике нет.
        HistoryError — хранилище отказало.
        """


class AcpSession:
    """Одна сессия ACP: открытый агент, его обслуживание очереди, приёмник и
    серверы MCP сессии (SessionMcp): вопрос-команда промпта сервера
    раскрывается ими, команда сессии (SessionCommands) становится своим
    элементом очереди, закрытие сессии закрывает потоки серверов."""

    def __init__(
        self,
        session_id: str,
        opened: OpenedSession,
        relay: SessionRelay,
        mapping: AcpMapping,
        mcp: SessionMcp,
    ) -> None:
        self._session_id = session_id
        self._opened = opened
        self._relay = relay
        self._mapping = mapping
        self._mcp = mcp
        self._commands = SessionCommands()
        self._reports: list[asyncio.Task[None]] = []
        self._serving = asyncio.create_task(
            opened.agent.serve(), name=f"acp-serve-{session_id}"
        )
        self._serving.add_done_callback(self._served)

    @property
    def agent(self) -> Agent:
        return self._opened.agent

    @property
    def relay(self) -> SessionRelay:
        return self._relay

    @property
    def model(self) -> str:
        return self._opened.model

    @property
    def hints(self) -> ToolHintSource:
        """Пометки инструментов сессии: их знает агент ядра."""
        return self.agent

    def set_mode(self, mode: PermissionMode) -> None:
        """Режим разрешений записью ядра; действует со следующей проверки.

        Ошибки:
        RequestError — режим недоступен при параметрах запуска (-32602).
        """
        try:
            self.agent.set_permission_mode(mode)
        except PermissionModeError as exc:
            raise RequestError.invalid_params(
                {ErrorDataKey.DETAILS.value: str(exc)}
            ) from exc

    async def set_model(self, name: str) -> None:
        """Профиль модели для следующих ходов; при идущем ходе или непустой
        очереди — отказ.

        Ошибки:
        RequestError — ход идёт (-32603), профиля нет (-32602), профиль не
            годится (-32603).
        """
        if self.busy():
            details = (
                f"session {self._session_id} is busy: a turn is in progress; switch "
                "the model while the session is idle"
            )
            raise RequestError.internal_error({ErrorDataKey.DETAILS.value: details})

        try:
            await self._opened.set_model(name)
        except SessionRefusedError as exc:
            raise RequestError.invalid_params(
                {ErrorDataKey.DETAILS.value: str(exc)}
            ) from exc
        except (SessionBusyError, SessionOpenError) as exc:
            raise RequestError.internal_error(
                {ErrorDataKey.DETAILS.value: str(exc)}
            ) from exc

    def busy(self) -> bool:
        """Ход идёт или ждёт в очереди."""
        if self.agent.session.state is not SessionState.IDLE:
            return True

        return len(self.agent.queue) > 0

    def _served(self, task: asyncio.Task[None]) -> None:
        """Обслуживание остановилось сбоем: идущий ход кончается пометкой с
        причиной и `idle` отказом."""
        if task.cancelled():
            return

        failure = task.exception()
        if failure is None:
            return

        logger.error(
            "session %s: the agent stopped serving: %s", self._session_id, failure
        )
        details = (
            f"session {self._session_id} stopped serving prompts before this one "
            f"was inserted: {failure}"
        )
        self._relay.refuse_all(
            RequestError.internal_error({ErrorDataKey.DETAILS.value: details})
        )
        self._reports.append(asyncio.create_task(self._relay.failed()))

    async def prompt(self, asked: QueueEntry) -> schema.PromptResponse:
        """Ставит вопрос в очередь ядра и отвечает, когда его ход вставил
        запись `user`: `messageId` ответа — её uuid. Вопрос идёт своим ходом,
        без склейки с соседями. Вопрос-команду промпта сервера раскрывает
        SessionMcp. Команда сессии `/compact` — элемент очереди compact: её
        вызов агент пишет в историю записью с uuid элемента, ответ приходит
        с этой записью, как у вопроса.

        Ошибки:
        RequestError — очередь не приняла элемент или аргументы команды не
            годятся (-32602), обслуживание сессии остановилось либо сервер не
            отдал промпт (-32603), вопрос снят до вставки отменой хода или
            закрытием сессии (-32800).
        """
        command = self._commands.queued(asked)
        if command is not None:
            asked = command

        if command is None:
            asked = await self._mcp.expanded(asked)
        if self._serving.done():
            details = (
                f"session {self._session_id} no longer serves prompts: its agent "
                "stopped; open a new session"
            )
            raise RequestError.internal_error({ErrorDataKey.DETAILS.value: details})

        agent = self.agent
        priority: QueuePriority | None = None
        if agent.session.state is not SessionState.IDLE:
            priority = QueuePriority.LATER

        if len(agent.queue) > 0:
            priority = QueuePriority.LATER

        entry = asked.model_copy(update={"priority": priority, "alone": True})
        try:
            item = agent.queue.enqueue(entry)
        except QueueRefusedError as exc:
            raise RequestError.invalid_params(
                {ErrorDataKey.DETAILS.value: str(exc)}
            ) from exc

        inserted = self._relay.insertion(item.id)
        if priority is QueuePriority.LATER:
            logger.info(
                "session %s: prompt %s waits in the queue behind the running turn",
                self._session_id,
                item.id,
            )

        await inserted

        return schema.PromptResponse(message_id=item.id)

    async def flush(self) -> None:
        """Сбрасывает несброшенные записи сессии в хранилище.

        Ошибки:
        HistoryError — хранилище отказало.
        """
        await self._opened.agent.writer.flush(strict=True)

    def cancel(self, reason: CoreStopReason) -> None:
        """Прерывает ход и очищает очередь: вопросы из неё не вставлены, их
        запросы получают -32800."""
        for item in self.agent.queue.items():
            details = (
                f"the prompt {item.id} of session {self._session_id} was removed "
                "from the queue by session/cancel before it was inserted"
            )
            self._relay.refuse(
                item.id,
                RequestError.request_cancelled({ErrorDataKey.DETAILS.value: details}),
            )

        self.agent.interrupt(reason, cancel_queued=True)

    async def close(self) -> None:
        details = (
            f"session {self._session_id} was closed before the prompt was inserted"
        )
        self._relay.refuse_all(
            RequestError.request_cancelled({ErrorDataKey.DETAILS.value: details})
        )
        self._relay.withdraw_all()
        await self._mcp.close()
        await self._opened.close()
        try:
            await self._serving
        except Exception as exc:
            logger.error(
                "session %s: serving ended with an error: %s", self._session_id, exc
            )

        await asyncio.gather(*self._reports)


class ObserverSession:
    """Сессия-наблюдатель: открыта без замка, беседу отдала, хода не ведёт.

    Создаёт вход на `session/resume`, когда сессию держит другой процесс;
    первый `prompt` зовёт `claim`, и при свободном замке наблюдатель уступает
    место обычной AcpSession с тем же приёмником.
    """

    def __init__(
        self,
        session_id: str,
        observed: ObservedSession,
        relay: SessionRelay,
        mapping: AcpMapping,
        mcp: SessionMcp,
    ) -> None:
        self._session_id = session_id
        self._observed = observed
        self._relay = relay
        self._mapping = mapping
        self._mcp = mcp

    @property
    def relay(self) -> SessionRelay:
        return self._relay

    @property
    def hints(self) -> ToolHintSource:
        return self._observed

    async def claim(self) -> AcpSession:
        """Ошибки:
        SessionHeldError — замок всё ещё у другого процесса.
        SessionNotFoundError — записи сессии исчезли из хранилища.
        SessionOpenError — сессию открыть не удалось.
        """
        opened = await self._observed.claim()
        self._relay.attach(opened.agent)

        return AcpSession(
            self._session_id, opened, self._relay, self._mapping, self._mcp
        )

    def busy(self) -> bool:
        """Наблюдатель хода не ведёт."""
        return False

    async def close(self) -> None:
        """Наблюдатель держит только серверы MCP сессии."""
        await self._mcp.close()


HeldSession: TypeAlias = AcpSession | ObserverSession
AuthMethod: TypeAlias = (
    schema.TerminalAuthMethod | schema.AgentAuthMethod | schema.OtherAuthMethod
)


@dataclass
class HostedSession:
    """Открытая сессия процесса в своём потоке: поток и сама сессия;
    наблюдатель, взявший замок, сменяется настоящей сессией на месте."""

    thread: LoopThread
    held: HeldSession


class AcpAgent(AcpAgentProtocol):
    """Реализация Agent SDK v2 над сессиями ядра.

    Создаёт вход процесса со сборкой сессий; AgentSide зовёт `attach` и
    получает агента для роутера SDK. Один объект обслуживает все сессии
    соединения: каждая — свой агент ядра над инструментами серверов `acp` из
    `session/new|resume|fork`, либо наблюдатель, пока сессию держит другой
    процесс.
    """

    # SDK считает метод протокола неподдержанным
    list_providers = AcpAgentProtocol.list_providers
    set_provider = AcpAgentProtocol.set_provider
    disable_provider = AcpAgentProtocol.disable_provider
    mcp_message = AcpAgentProtocol.mcp_message
    notify_mcp = AcpAgentProtocol.notify_mcp
    start_nes = AcpAgentProtocol.start_nes
    suggest_nes = AcpAgentProtocol.suggest_nes
    accept_nes = AcpAgentProtocol.accept_nes
    reject_nes = AcpAgentProtocol.reject_nes
    close_nes = AcpAgentProtocol.close_nes
    did_open = AcpAgentProtocol.did_open
    did_change = AcpAgentProtocol.did_change
    did_close = AcpAgentProtocol.did_close
    did_save = AcpAgentProtocol.did_save
    did_focus = AcpAgentProtocol.did_focus

    def __init__(
        self,
        assembly: SessionAssembly,
        info: schema.Implementation,
        limits: McpClientJson,
        threads: SessionThreads,
        auth_methods: Sequence[schema.AgentAuthMethod],
    ) -> None:
        self._assembly = assembly
        self._info = info
        self._limits = limits
        self._threads = threads
        self._auth_methods = list(auth_methods)
        """Способы входа, которые вход объявляет в initialize: у `boba serve`
        — токен службы, проверенный транспортом до подключения; у stdio их
        нет."""
        self._link: ClientLink | None = None
        self._elicits = False
        """Клиент объявил формы `elicitation.form` в initialize."""
        self._sessions: dict[str, HostedSession] = {}
        self._mapping = AcpMapping()
        self._options = SessionOptions()
        self._commands = SessionCommands()
        self._prompts = PromptText()
        self._specs = McpServerSpecs()
        self._titles = SessionTitles()
        self._ids = IdMint()

    def attach(self, side: AgentSide, connection: AgentSideConnection) -> AcpAgent:
        """Фабрика агента для AgentSide: связь с клиентом в главном цикле
        процесса и сам агент."""
        home = LoopCall(asyncio.get_running_loop())
        self._link = ClientLink(side, connection, home)

        return self

    def _linked(self) -> ClientLink:
        if self._link is None:
            msg = "The ACP agent has no client connection yet; attach() comes first."
            raise RuntimeError(msg)

        return self._link

    async def initialize(
        self,
        protocol_version: int,
        info: schema.Implementation,
        capabilities: schema.ClientCapabilities | None = None,
        **kwargs: Any,
    ) -> schema.InitializeResponse:
        logger.info(
            "initialize: %s %s speaks protocol version %d",
            info.name,
            info.version,
            protocol_version,
        )
        elicitation = None
        if capabilities is not None:
            elicitation = capabilities.elicitation

        if elicitation is not None:
            self._elicits = elicitation.form is not None
        session = schema.SessionCapabilities(
            prompt=schema.PromptCapabilities(),
            mcp=schema.McpCapabilities(acp=schema.McpAcpCapabilities()),
            delete=schema.SessionDeleteCapabilities(),
            fork=schema.SessionForkCapabilities(
                field_meta={MetaKey.UP_TO_MESSAGE_ID.value: True}
            ),
        )

        methods: list[AuthMethod] | None = None
        if self._auth_methods:
            methods = list(self._auth_methods)

        return schema.InitializeResponse(
            protocol_version=AcpVersion.V2,
            info=self._info,
            capabilities=schema.AgentCapabilities(session=session),
            auth_methods=methods,
        )

    async def login(self, method_id: str, **kwargs: Any) -> schema.LoginAuthResponse:
        """Вход объявленным способом: подключение уже вошло токеном, который
        проверил транспорт, — вход принимается сразу.

        Ошибки:
        RequestError — вход не объявлял способов (-32601), такого способа
            вход не объявлял (-32602).
        """
        if not self._auth_methods:
            raise RequestError.method_not_found(AGENT_METHODS["auth_login"])

        for method in self._auth_methods:
            if method.method_id == method_id:
                return schema.LoginAuthResponse()

        offered: list[str] = []
        for method in self._auth_methods:
            offered.append(method.method_id)

        details = f"auth method {method_id!r} is not offered; offered: {offered}"
        raise RequestError.invalid_params({ErrorDataKey.DETAILS.value: details})

    async def logout(self, **kwargs: Any) -> schema.LogoutAuthResponse:
        """Выход: сессии подключения закрываются.

        Ошибки:
        RequestError — вход не объявлял способов (-32601).
        """
        if not self._auth_methods:
            raise RequestError.method_not_found(AGENT_METHODS["auth_logout"])

        await self.close_all()

        return schema.LogoutAuthResponse()

    async def new_session(
        self,
        cwd: str,
        additional_directories: list[str] | None = None,
        mcp_servers: list[McpServerBlock] | None = None,
        **kwargs: Any,
    ) -> schema.NewSessionResponse:
        """Сессия открывается в своём потоке.

        Ошибки:
        RequestError — `cwd` не абсолютный или серверы MCP не годятся
            (-32602); инструменты или сессию открыть не удалось, процесс
            держит предельное число сессий (-32603).
        """
        directory = self._directory(cwd)
        servers = self._specs.servers(self._blocks(mcp_servers))
        session_id = self._ids.uuid()
        thread = self._thread_for(session_id)
        try:
            session = await thread.run(self._new(session_id, directory, servers))
        except BaseException:
            await self._threads.stop(thread)
            raise

        self._sessions[session_id] = HostedSession(thread=thread, held=session)
        logger.info(
            "session %s: new, cwd %s, %d MCP server(s), thread %s",
            session_id,
            cwd,
            len(servers),
            thread.name,
        )

        return schema.NewSessionResponse(
            session_id=session_id,
            config_options=self._options.config_options(self._settings(session)),
        )

    async def _new(
        self,
        session_id: str,
        directory: Path,
        servers: Sequence[schema.AcpMcpServer],
    ) -> AcpSession:
        """Сборка новой сессии в цикле её потока.

        Ошибки:
        RequestError — сборка отвергла параметры (-32602) или не открыла
            сессию (-32603).
        """
        relay = SessionRelay(session_id, self._linked(), self._mapping, self._elicits)
        async with AsyncExitStack() as owned:
            mcp = await self._mcp(servers, relay)
            owned.push_async_callback(mcp.close)
            try:
                opened = await self._assembly.open(
                    SessionId(value=session_id),
                    directory,
                    mcp,
                    mcp,
                    relay,
                    resume=False,
                    questions=self._elicits,
                )
            except SessionRefusedError as exc:
                raise RequestError.invalid_params(
                    {ErrorDataKey.DETAILS.value: str(exc)}
                ) from exc
            except (SessionOpenError, HistoryError) as exc:
                raise RequestError.internal_error(
                    {ErrorDataKey.DETAILS.value: str(exc)}
                ) from exc

            owned.pop_all()

        relay.attach(opened.agent)
        session = AcpSession(session_id, opened, relay, self._mapping, mcp)
        mcp.announce_later(self._commands.update())

        return session

    def _thread_for(self, session_id: str) -> LoopThread:
        """Ошибки:
        RequestError — процесс держит предельное число сессий (-32603).
        """
        try:
            return self._threads.open(session_id)
        except SessionLimitError as exc:
            raise RequestError.internal_error(
                {ErrorDataKey.DETAILS.value: str(exc)}
            ) from exc

    async def _mcp(
        self, servers: Sequence[schema.AcpMcpServer], relay: SessionRelay
    ) -> SessionMcp:
        """Серверы MCP сессии: каждый согласован и отдал списки до ответа на
        запрос. Сервер, который не открылся, сессия не берёт: ход идёт без
        его инструментов, клиент получает пометку (SessionMcp.unavailable).
        """
        link = self._linked()
        client = mt.Implementation(name=self._info.name, version=self._info.version)
        mcp = SessionMcp(relay)
        async with AsyncExitStack() as owned:
            owned.push_async_callback(mcp.close)
            for server in servers:
                tool_server = AcpToolServer(
                    link,
                    server,
                    relay,
                    self._limits,
                    self._mapping,
                    client,
                    mcp.changes,
                )
                try:
                    await tool_server.open()
                except SessionOpenError as exc:
                    await tool_server.close()
                    mcp.unavailable(server.name, exc)
                    continue

                mcp.add(tool_server)

            owned.pop_all()

        return mcp

    def _settings(self, session: AcpSession) -> SessionSettings:
        agent = session.agent

        return SessionSettings(
            mode=agent.permission_mode,
            modes=agent.available_modes(),
            model=session.model,
            models=self._assembly.models(),
        )

    async def _announce_settings(self, session: AcpSession) -> None:
        """Полное состояние настроек клиенту: `config_option_update`."""
        settings = self._settings(session)
        await session.relay.notify(self._options.options_update(settings))

    async def prompt(
        self, session_id: str, prompt: list[ContentBlock], **kwargs: Any
    ) -> schema.PromptResponse:
        """Ошибки:
        RequestError — сессия неизвестна (-32002), блоки не годятся (-32602),
            сессию держит другой процесс (-32603).
        """
        hosted = self._session(session_id)
        asked = self._prompts.render(prompt)
        session = await self._claimed(session_id, hosted)

        return await hosted.thread.run(session.prompt(asked))

    async def _claimed(self, session_id: str, hosted: HostedSession) -> AcpSession:
        """Сессия, которая ведёт ходы: наблюдатель берёт замок и открывает
        сессию по-настоящему на своём месте.

        Ошибки:
        RequestError — сессию держит другой процесс (-32603), записи
            исчезли (-32002), сессию открыть не удалось (-32603).
        """
        held = hosted.held
        if isinstance(held, AcpSession):
            return held

        try:
            session = await hosted.thread.run(held.claim())
        except SessionHeldError as exc:
            raise self._held_by_other(session_id, str(exc)) from exc
        except SessionNotFoundError as exc:
            await self._closed(session_id, hosted)
            raise self._unknown(session_id) from exc
        except (SessionOpenError, HistoryError) as exc:
            raise RequestError.internal_error(
                {ErrorDataKey.DETAILS.value: str(exc)}
            ) from exc

        hosted.held = session
        logger.info("session %s: the observer took the lock", session_id)
        await hosted.thread.run(self._announce_settings(session))

        return session

    async def cancel_session(self, session_id: str, **kwargs: Any) -> None:
        hosted = self._sessions.get(session_id)
        if hosted is None:
            logger.info("cancel: session %s is unknown; nothing to cancel", session_id)
            return

        held = hosted.held
        if isinstance(held, ObserverSession):
            logger.info("cancel: session %s is observed, no turn to cancel", session_id)
            return

        hosted.thread.soon(partial(held.cancel, CoreStopReason.USER_STOP))

    def _session(self, session_id: str) -> HostedSession:
        """Ошибки:
        RequestError — сессия этому процессу неизвестна (-32002).
        """
        hosted = self._sessions.get(session_id)
        if hosted is None:
            raise self._unknown(session_id)

        return hosted

    @staticmethod
    def _unknown(session_id: str) -> RequestError:
        return RequestError(
            AcpErrorCode.RESOURCE_NOT_FOUND,
            f"session {session_id} is unknown to this agent process",
            {ErrorDataKey.SESSION_ID.value: session_id},
        )

    @staticmethod
    def _held_by_other(session_id: str, details: str) -> RequestError:
        return RequestError(
            AcpErrorCode.INTERNAL_ERROR,
            f"session {session_id} is held by another process",
            {
                ErrorDataKey.SESSION_ID.value: session_id,
                ErrorDataKey.DETAILS.value: details,
            },
        )

    @staticmethod
    def _directory(cwd: str) -> Path:
        """Ошибки:
        RequestError — `cwd` не абсолютный (-32602).
        """
        directory = Path(cwd)
        if not directory.is_absolute():
            details = f"cwd must be an absolute path, got '{cwd}'"
            raise RequestError.invalid_params({ErrorDataKey.DETAILS.value: details})

        return directory

    @staticmethod
    def _blocks(mcp_servers: list[McpServerBlock] | None) -> list[McpServerBlock]:
        if mcp_servers is None:
            return []

        return mcp_servers

    @staticmethod
    def _stored_id(session_id: str) -> SessionId:
        """Ошибки:
        RequestError — строка не может быть идентификатором сессии (-32002).
        """
        try:
            return SessionId(value=session_id)
        except ValidationError as exc:
            raise AcpAgent._unknown(session_id) from exc

    def shutdown(self, reason: CoreStopReason) -> None:
        """Прерывает ходы всех сессий: процесс останавливается."""
        for hosted in self._sessions.values():
            held = hosted.held
            if isinstance(held, AcpSession):
                hosted.thread.soon(partial(held.cancel, reason))

    def busy(self) -> bool:
        """Идёт ли ход хотя бы в одной сессии входа."""
        states = self._session_busy()

        return any(states)

    def _session_busy(self) -> Iterator[bool]:
        for hosted in self._sessions.values():
            yield hosted.held.busy()

    async def close_all(self) -> None:
        for session_id, hosted in list(self._sessions.items()):
            await self._closed(session_id, hosted)

    async def _closed(self, session_id: str, hosted: HostedSession) -> None:
        """Сессия закрыта в своём потоке, поток остановлен."""
        self._sessions.pop(session_id, None)
        try:
            await hosted.thread.run(hosted.held.close())
        finally:
            await self._threads.stop(hosted.thread)

        logger.info("session %s: closed, its thread is stopped", session_id)

    async def resume_session(
        self,
        session_id: str,
        cwd: str,
        additional_directories: list[str] | None = None,
        mcp_servers: list[McpServerBlock] | None = None,
        replay_from: schema.ReplayFromStartVariant
        | schema.OtherReplayFrom
        | None = None,
        **kwargs: Any,
    ) -> schema.ResumeSessionResponse:
        """Открывает сессию из хранилища в её потоке; с `replayFrom: start`
        до ответа воспроизводит всю беседу уведомлениями, за ней — заголовок
        и заполненность. Ответ несёт настройки сессии; у наблюдателя агента
        нет, его ответ без них.

        Ошибки:
        RequestError — сессия неизвестна (-32002), `cwd`, серверы или
            `replayFrom` не годятся (-32602), сессию открыть не удалось
            (-32603).
        """
        if isinstance(replay_from, schema.OtherReplayFrom):
            details = (
                f"replayFrom of type '{replay_from.type}' is not supported; "
                "expected type 'start' or no replayFrom"
            )
            raise RequestError.invalid_params({ErrorDataKey.DETAILS.value: details})

        hosted = await self._open_stored(session_id, cwd, mcp_servers)
        held = hosted.held
        replay = isinstance(replay_from, schema.ReplayFromStartVariant)
        if replay:
            await hosted.thread.run(self._replayed(session_id, held))

        logger.info("session %s: resumed", session_id)
        if isinstance(held, ObserverSession):
            return schema.ResumeSessionResponse()

        if replay:
            await hosted.thread.run(held.relay.announce_loaded())

        return schema.ResumeSessionResponse(
            config_options=self._options.config_options(self._settings(held))
        )

    async def _replayed(self, session_id: str, held: HeldSession) -> None:
        """Беседа уведомлениями по порядку записей — в цикле потока сессии,
        потоком: запись из хранилища → её уведомления → отправка, без
        накопления. За сохранёнными записями у открытой сессии идут её ещё
        не сохранённые (починка оборванного хода).

        Ошибки:
        RequestError — хранилище отказало (-32603).
        """
        stored = SessionId(value=session_id)
        hints: ToolHintSource = held.hints
        opened = OpenCompactions()
        replayed = 0
        last = 0
        try:
            async with aclosing(self._assembly.history(stored)) as records:
                async for record in records:
                    await self._replay_record(held, record, hints, opened)
                    replayed += 1
                    last = record.seq
        except HistoryError as exc:
            raise RequestError.internal_error(
                {ErrorDataKey.DETAILS.value: str(exc)}
            ) from exc

        live = False
        if isinstance(held, AcpSession):
            for record in held.agent.session.since(last):
                if not isinstance(record, ChainRecord):
                    continue

                await self._replay_record(held, record, hints, opened)
                replayed += 1

            live = held.agent.session.state is SessionState.COMPACTING

        for compaction_id in opened.unfinished(live):
            await held.relay.notify(self._mapping.interrupted(compaction_id))

        logger.info("session %s: %d record(s) replayed", session_id, replayed)

    async def _replay_record(
        self,
        held: HeldSession,
        record: ChainRecord,
        hints: ToolHintSource,
        opened: OpenCompactions,
    ) -> None:
        """Уведомления записи; вопрос человека сперва закрывает сжатия, у
        которых в истории нет конца."""
        for compaction_id in opened.before(record):
            await held.relay.notify(self._mapping.interrupted(compaction_id))

        for update in self._mapping.replayed(record, hints):
            await held.relay.notify(update)

        opened.after(record)

    async def _open_stored(
        self,
        session_id: str,
        cwd: str,
        mcp_servers: list[McpServerBlock] | None,
    ) -> HostedSession:
        """Сессия хранилища: уже открытая в этом процессе — как есть; иначе
        открывается в новом потоке с замком, а при занятом замке —
        наблюдателем.

        Ошибки:
        RequestError — сессия неизвестна (-32002), параметры не годятся
            (-32602), инструменты или сессию открыть не удалось, процесс
            держит предельное число сессий (-32603).
        """
        directory = self._directory(cwd)
        servers = self._specs.servers(self._blocks(mcp_servers))
        known = self._sessions.get(session_id)
        if known is not None:
            return known

        stored = self._stored_id(session_id)
        thread = self._thread_for(session_id)
        try:
            held = await thread.run(self._stored(stored, directory, servers))
        except BaseException:
            await self._threads.stop(thread)
            raise

        hosted = HostedSession(thread=thread, held=held)
        self._sessions[session_id] = hosted

        return hosted

    async def _stored(
        self,
        stored: SessionId,
        directory: Path,
        servers: Sequence[schema.AcpMcpServer],
    ) -> HeldSession:
        """Сборка сохранённой сессии в цикле её потока.

        Ошибки:
        RequestError — сессия неизвестна (-32002), параметры не годятся
            (-32602), инструменты или сессию открыть не удалось (-32603).
        """
        relay = SessionRelay(stored.value, self._linked(), self._mapping, self._elicits)
        async with AsyncExitStack() as owned:
            mcp = await self._mcp(servers, relay)
            owned.push_async_callback(mcp.close)
            try:
                held = await self._held(stored, directory, mcp, relay)
            except SessionNotFoundError as exc:
                raise self._unknown(stored.value) from exc
            except SessionRefusedError as exc:
                raise RequestError.invalid_params(
                    {ErrorDataKey.DETAILS.value: str(exc)}
                ) from exc
            except (SessionOpenError, HistoryError) as exc:
                raise RequestError.internal_error(
                    {ErrorDataKey.DETAILS.value: str(exc)}
                ) from exc

            owned.pop_all()

        mcp.announce_later(self._commands.update())

        return held

    async def _held(
        self,
        stored: SessionId,
        directory: Path,
        mcp: SessionMcp,
        relay: SessionRelay,
    ) -> HeldSession:
        """Ошибки:
        SessionNotFoundError, SessionRefusedError, SessionOpenError,
        HistoryError — от сборки.
        """
        try:
            opened = await self._assembly.open(
                stored,
                directory,
                mcp,
                mcp,
                relay,
                resume=True,
                questions=self._elicits,
            )
        except SessionHeldError as exc:
            logger.info(
                "session %s: held by another process, opened as an observer (%s)",
                stored.value,
                exc,
            )
            observed = await self._assembly.observe(
                stored, directory, mcp, mcp, relay, self._elicits
            )

            return ObserverSession(stored.value, observed, relay, self._mapping, mcp)

        relay.attach(opened.agent)

        return AcpSession(stored.value, opened, relay, self._mapping, mcp)

    async def list_sessions(
        self, cwd: str | None = None, cursor: str | None = None, **kwargs: Any
    ) -> schema.ListSessionsResponse:
        """Сессии хранилища страницей: курсор — идентификатор последней
        сессии предыдущей страницы; `cwd` оставляет сессии этого каталога.

        Ошибки:
        RequestError — `cwd` не абсолютный (-32602), хранилище отказало
            (-32603).
        """
        wanted: Path | None = None
        if cwd is not None:
            wanted = self._directory(cwd)

        try:
            page = await self._listed(wanted, cursor)
        except HistoryError as exc:
            raise RequestError.internal_error(
                {ErrorDataKey.DETAILS.value: str(exc)}
            ) from exc

        sessions: list[schema.SessionInfo] = []
        for summary in page.sessions:
            updated_at = datetime.fromisoformat(summary.updated_at)
            sessions.append(
                schema.SessionInfo(
                    session_id=summary.session_id,
                    cwd=summary.cwd,
                    title=self._titles.title(summary.first_prompt),
                    updated_at=updated_at,
                )
            )

        return schema.ListSessionsResponse(
            sessions=sessions, next_cursor=page.next_cursor
        )

    async def _listed(self, wanted: Path | None, cursor: str | None) -> SessionPage:
        """Страница сессий: без каталога — страница хранилища как есть; с
        каталогом страницы хранилища читаются подряд, пока не наберётся
        полная страница сессий этого каталога или сессии не кончатся.
        Курсор ответа — идентификатор последней отданной сессии, так что
        следующая страница продолжает с него.

        Ошибки:
        HistoryError — хранилище отказало.
        """
        page = await self._assembly.sessions(cursor)
        if wanted is None:
            return page

        picked: list[SessionSummary] = []
        while True:
            for summary in page.sessions:
                if Path(summary.cwd) == wanted:
                    picked.append(summary)

            if page.next_cursor is None:
                return SessionPage(sessions=picked, next_cursor=None)

            size = len(page.sessions)
            if len(picked) >= size:
                full = picked[:size]
                return SessionPage(sessions=full, next_cursor=full[-1].session_id)

            page = await self._assembly.sessions(page.next_cursor)

    async def delete_session(
        self, session_id: str, **kwargs: Any
    ) -> schema.DeleteSessionResponse:
        """Сессия, которую ведёт этот процесс, закрывается до удаления (она
        держит замок); наблюдатель закрывается после, когда хранилище
        удаление приняло. Повторное удаление и сессия, которой не было, —
        успех.

        Ошибки:
        RequestError — сессию держит другой процесс или хранилище отказало
            (-32603).
        """
        hosted = self._sessions.get(session_id)
        observed: HostedSession | None = None
        if hosted is not None:
            if isinstance(hosted.held, AcpSession):
                await self._closed(session_id, hosted)
            else:
                observed = hosted

        try:
            stored = SessionId(value=session_id)
        except ValidationError:
            logger.info("session %s: cannot exist, nothing to delete", session_id)
            return schema.DeleteSessionResponse()

        await self._delete_stored(session_id, stored)
        if observed is not None:
            await self._closed(session_id, observed)

        logger.info("session %s: deleted", session_id)

        return schema.DeleteSessionResponse()

    async def _delete_stored(self, session_id: str, stored: SessionId) -> None:
        """Ошибки:
        RequestError — сессию держит другой процесс или хранилище отказало
            (-32603).
        """
        try:
            await self._assembly.delete(stored)
        except SessionHeldError as exc:
            raise self._held_by_other(session_id, str(exc)) from exc
        except HistoryError as exc:
            raise RequestError.internal_error(
                {ErrorDataKey.DETAILS.value: str(exc)}
            ) from exc

    async def set_config_option(
        self,
        config_id: str,
        session_id: str,
        value: Any,
        *,
        type: str | None = None,  # noqa: A002 — имя поля протокола
        **kwargs: Any,
    ) -> schema.SetSessionConfigOptionResponse:
        """`mode` — режим разрешений ядра, `model` — профиль модели для
        следующих ходов; ответ — полное состояние настроек, оно же уходит
        `config_option_update`. Смена идёт в цикле потока сессии.

        Ошибки:
        RequestError — сессия неизвестна (-32002), настройки или значения
            нет, значение не строка (-32602), ход идёт или профиль не
            годится, сессию держит другой процесс (-32603).
        """
        hosted, session = self._settable(session_id)
        option = self._option_id(config_id)
        if not isinstance(value, str):
            details = (
                f"configOption '{config_id}' is a select: value must be a string, "
                f"got {value!r}"
            )
            raise RequestError.invalid_params({ErrorDataKey.DETAILS.value: details})

        return await hosted.thread.run(self._configured(session, option, value))

    async def _configured(
        self, session: AcpSession, option: ConfigOptionId, value: str
    ) -> schema.SetSessionConfigOptionResponse:
        if option is ConfigOptionId.MODE:
            await self._set_mode(session, value)
        else:
            name = self._options.parse_model(value, self._settings(session))
            await session.set_model(name)
            logger.info(
                "session %s: model profile switched to %s",
                session.agent.session.id.value,
                name,
            )

        await self._announce_settings(session)

        return schema.SetSessionConfigOptionResponse(
            config_options=self._options.config_options(self._settings(session))
        )

    async def _set_mode(self, session: AcpSession, value: str) -> None:
        """Ошибки:
        RequestError — режима нет или он недоступен (-32602).
        """
        mode = self._options.parse_mode(value, self._settings(session))
        session.set_mode(mode)
        logger.info(
            "session %s: permission mode switched to %s",
            session.agent.session.id.value,
            mode.value,
        )

    def _settable(self, session_id: str) -> tuple[HostedSession, AcpSession]:
        """Сессия, настройки которой можно менять: у наблюдателя агента нет.

        Ошибки:
        RequestError — сессия неизвестна (-32002), её держит другой
            процесс (-32603).
        """
        hosted = self._session(session_id)
        held = hosted.held
        if isinstance(held, ObserverSession):
            details = "its settings can be changed only by the process that holds it"
            raise self._held_by_other(session_id, details)

        return hosted, held

    @staticmethod
    def _option_id(config_id: str) -> ConfigOptionId:
        """Ошибки:
        RequestError — такой настройки у агента нет (-32602).
        """
        try:
            return ConfigOptionId(config_id)
        except ValueError as exc:
            known = [option.value for option in ConfigOptionId]
            details = f"configOption '{config_id}' is unknown; available: {known}"
            raise RequestError.invalid_params(
                {ErrorDataKey.DETAILS.value: details}
            ) from exc

    async def fork_session(
        self,
        session_id: str,
        cwd: str,
        additional_directories: list[str] | None = None,
        mcp_servers: list[McpServerBlock] | None = None,
        **kwargs: Any,
    ) -> schema.ForkSessionResponse:
        """Ветвит сессию хранилища: её записи копируются под новый
        идентификатор (его чеканит агент), и новая сессия открывается как
        обычная в своём потоке — с замком, инструментами серверов запроса и
        починкой оборванного хода загрузчиком. Исходная сессия не меняется,
        её замок не нужен; сессию, которую ведёт этот процесс, перед копией
        сбрасывает в хранилище. Точка среза — расширение
        `_meta["boba/upToMessageId"]` (ForkMeta): ветка от `messageId`
        вопроса — беседа до этого вопроса; срез на первом вопросе даёт
        пустую сессию. Ответ — `sessionId` новой сессии с её настройками, как
        у `session/new`.

        Ошибки:
        RequestError — сессия неизвестна (-32002); `cwd`, серверы или
            `_meta` не годятся, записи среза в сессии нет (-32602);
            хранилище, инструменты или сборка отказали, процесс держит
            предельное число сессий (-32603).
        """
        directory = self._directory(cwd)
        servers = self._specs.servers(self._blocks(mcp_servers))
        meta = self._fork_meta(kwargs)
        source = self._stored_id(session_id)
        target = SessionId(value=self._ids.uuid())
        up_to = meta.up_to_message_id
        await self._flushed(session_id)
        copied = await self._copied(session_id, source, target, up_to)
        thread = self._thread_for(target.value)
        try:
            session = await thread.run(
                self._forked(target, directory, servers, copied > 0)
            )
        except BaseException:
            await self._threads.stop(thread)
            raise

        self._sessions[target.value] = HostedSession(thread=thread, held=session)
        logger.info(
            "session %s: forked from %s, %d record(s) copied, cut at %s",
            target.value,
            session_id,
            copied,
            up_to,
        )

        return schema.ForkSessionResponse(
            session_id=target.value,
            config_options=self._options.config_options(self._settings(session)),
        )

    @staticmethod
    def _fork_meta(meta: Mapping[str, Any]) -> ForkMeta:
        """Ошибки:
        RequestError — `boba/upToMessageId` не непустая строка (-32602).
        """
        try:
            return ForkMeta.model_validate(meta)
        except ValidationError as exc:
            details = f"_meta of session/fork is not valid: {exc}"
            raise RequestError.invalid_params(
                {ErrorDataKey.DETAILS.value: details}
            ) from exc

    async def _flushed(self, session_id: str) -> None:
        """Сессия, которую ведёт этот процесс, сбрасывается в хранилище в
        своём потоке, чтобы копия видела все её записи.

        Ошибки:
        RequestError — хранилище отказало (-32603).
        """
        hosted = self._sessions.get(session_id)
        if hosted is None:
            return

        held = hosted.held
        if not isinstance(held, AcpSession):
            return

        try:
            await hosted.thread.run(held.flush())
        except HistoryError as exc:
            raise RequestError.internal_error(
                {ErrorDataKey.DETAILS.value: str(exc)}
            ) from exc

    async def _copied(
        self, session_id: str, source: SessionId, target: SessionId, up_to: str | None
    ) -> int:
        """Ошибки:
        RequestError — сессия неизвестна (-32002), записи `up_to` нет
            (-32602), хранилище отказало (-32603).
        """
        try:
            return await self._assembly.copy(source, target, up_to)
        except SessionNotFoundError as exc:
            raise self._unknown(session_id) from exc
        except RecordNotFoundError as exc:
            raise RequestError.invalid_params(
                {
                    ErrorDataKey.SESSION_ID.value: session_id,
                    ErrorDataKey.UP_TO_MESSAGE_ID.value: up_to,
                    ErrorDataKey.DETAILS.value: str(exc),
                }
            ) from exc
        except HistoryError as exc:
            raise RequestError.internal_error(
                {ErrorDataKey.DETAILS.value: str(exc)}
            ) from exc

    async def _forked(
        self,
        target: SessionId,
        directory: Path,
        servers: Sequence[schema.AcpMcpServer],
        resume: bool,
    ) -> AcpSession:
        """Новая сессия над скопированными записями в цикле её потока; если
        открыть её не удалось, копия удаляется из хранилища, чтобы ветка не
        осталась сиротой.

        Ошибки:
        RequestError — параметры не годятся (-32602), сессию открыть не
            удалось (-32603).
        """
        relay = SessionRelay(target.value, self._linked(), self._mapping, self._elicits)
        async with AsyncExitStack() as owned:
            mcp = await self._mcp(servers, relay)
            owned.push_async_callback(mcp.close)
            try:
                opened = await self._assembly.open(
                    target,
                    directory,
                    mcp,
                    mcp,
                    relay,
                    resume=resume,
                    questions=self._elicits,
                )
            except SessionRefusedError as exc:
                await self._dropped_fork(target, exc)
                raise RequestError.invalid_params(
                    {ErrorDataKey.DETAILS.value: str(exc)}
                ) from exc
            except (SessionOpenError, HistoryError) as exc:
                await self._dropped_fork(target, exc)
                raise RequestError.internal_error(
                    {ErrorDataKey.DETAILS.value: str(exc)}
                ) from exc

            owned.pop_all()

        relay.attach(opened.agent)
        session = AcpSession(target.value, opened, relay, self._mapping, mcp)
        mcp.announce_later(self._commands.update())

        return session

    async def _dropped_fork(self, target: SessionId, cause: Exception) -> None:
        """Ошибки:
        RequestError — копию не удалось ни открыть, ни удалить (-32603).
        """
        try:
            await self._assembly.delete(target)
        except HistoryError as exc:
            details = (
                f"the forked session {target.value} could not be opened ({cause}) "
                f"and its copy could not be removed: {exc}"
            )
            raise RequestError.internal_error(
                {ErrorDataKey.DETAILS.value: details}
            ) from exc

    async def close_session(
        self, session_id: str, **kwargs: Any
    ) -> schema.CloseSessionResponse:
        """Гасит ход сессии, отпускает замок и останавливает её поток.

        Ошибки:
        RequestError — сессия неизвестна (-32002).
        """
        hosted = self._session(session_id)
        await self._closed(session_id, hosted)
        logger.info("session %s: closed", session_id)

        return schema.CloseSessionResponse()
