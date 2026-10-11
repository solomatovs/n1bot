"""Вход ACP: агент `boba acp` над ядром boba-agent на SDK agent-client-protocol.

Сервер JSON-RPC по stdio: `initialize`, `session/new`, `session/prompt` с
потоком `session/update`, `session/cancel`, `$/cancel_request`, запрос
`session/request_permission` к клиенту; история — `session/load` с
воспроизведением всей беседы уведомлениями до ответа, `session/resume` без
воспроизведения, `session/list` страницами с курсором, `session/close`,
`session/delete`. Сессию открывает сборка (SessionAssembly): агент ядра с
серверами MCP из запроса; события ядра приёмник SessionRelay переводит в
уведомления по таблице AcpMapping, а ответ на `session/prompt` уходит после
них, когда ядро отдало итог хода. Беседа при загрузке воспроизводится той
же таблицей из записей сессии. Второй `prompt` в идущем ходе встаёт в
очередь ввода ядра и получает ответ по завершении своего хода;
`session/cancel` прерывает ход и очищает очередь.

Сессию, которую держит другой процесс, `load`/`resume` открывают
наблюдателем (ObserverSession): историю он отдаёт, а `prompt` отвечает
-32603 «held by another process», пока замок занят; когда держатель вышел,
следующий `prompt` открывает сессию по-настоящему и проходит. Роли
наблюдателя в ACP нет — это расширение агента, видимое клиенту только
текстом ошибки.

Настройки сессии (SessionOptions): ответы `session/new|load|resume` несут
`modes` по режимам разрешений ядра под потолком запуска и `configOptions`
`mode` (те же режимы) и `model` (профили `models.<имя>` файла настроек);
`session/set_mode` и `session/set_config_option` меняют режим записью ядра
или переключают профиль модели для следующих ходов (при идущем ходе —
-32603), после смены уходят `current_mode_update` и `config_option_update`.
После каждого хода и после сжатия уходит `usage_update` с заполненностью
контекста; после первого хода и при `load` — `session_info_update` с
заголовком из первого вопроса. Уведомлений `compaction_update`,
`compaction_summary_chunk` и `notice` в SDK 0.12.1 нет: ошибки вне хода
остаются в логе. `session/fork` (черновик RFD) копирует записи сессии из
хранилища под новый идентификатор и открывает новую сессию как обычную, с
теми же `modes` и `configOptions` в ответе; точка среза — расширение
`_meta["boba/upToUuid"]`. Клиент вправе дать вопросу uuid записи `user`
расширением `_meta["boba/messageId"]` запроса `session/prompt`: по нему
воспроизведение и ветвление называют вопрос так же, как клиент. `authenticate`
объявлен и отвечает «метод не найден» — точка расширения. Всё своё в протоколе
лежит в `_meta` под ключами с префиксом `boba/`.

Ошибки:
RequestError — ошибка JSON-RPC клиенту: параметры запроса (-32602),
    неизвестная сессия (-32002), нереализованный метод (-32601), отказ
    модели, сборки или хранилища сессии, сессия у другого процесса (-32603).
SessionRefusedError — сборка сессии отвергла параметры запроса; поднимает
    реализация SessionAssembly, вход переводит в -32602.
SessionOpenError — сессию открыть не удалось: хранилище, сервер MCP,
    профиль; поднимает реализация SessionAssembly, вход переводит в -32603.
SessionBusyError — модель меняют при идущем ходе; поднимает реализация
    OpenedSession, вход переводит в -32603.
"""

from __future__ import annotations

import asyncio
import json
import logging
from abc import abstractmethod
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, ClassVar, Protocol, TypeAlias
from uuid import UUID

from acp import Agent as AcpAgentProtocol
from acp import Client as AcpClientProtocol
from acp.agent.router import build_agent_router
from acp.connection import Connection, StreamDirection
from acp.connection import StreamEvent as FrameEvent
from acp.exceptions import RequestError
from acp.helpers import (
    start_tool_call,
    text_block,
    tool_content,
    update_agent_message,
    update_agent_thought,
    update_current_mode,
    update_tool_call,
    update_user_message,
)
from acp.meta import PROTOCOL_VERSION
from acp.router import Route
from acp.schema import (
    AcpMcpServer,
    AgentCapabilities,
    AgentMessageChunk,
    AgentThoughtChunk,
    AudioContentBlock,
    AuthenticateResponse,
    CancelRequestNotification,
    ClientCapabilities,
    CloseSessionResponse,
    ConfigOptionUpdate,
    CurrentModeUpdate,
    DeleteSessionRequest,
    DeleteSessionResponse,
    EmbeddedResourceContentBlock,
    ForkSessionResponse,
    HttpMcpServer,
    ImageContentBlock,
    Implementation,
    InitializeResponse,
    ListSessionsResponse,
    LoadSessionResponse,
    McpCapabilities,
    McpServerHttp,
    McpServerStdio,
    NewSessionResponse,
    PermissionOption,
    PermissionOptionKind,
    PromptCapabilities,
    PromptResponse,
    RequestPermissionRequest,
    RequestPermissionResponse,
    ResourceContentBlock,
    ResumeSessionResponse,
    SelectedPermissionOutcome,
    SessionCapabilities,
    SessionCloseCapabilities,
    SessionConfigOptionBoolean,
    SessionConfigOptionSelect,
    SessionDeleteCapabilities,
    SessionForkCapabilities,
    SessionInfo,
    SessionInfoUpdate,
    SessionListCapabilities,
    SessionMode,
    SessionModeState,
    SessionNotification,
    SessionResumeCapabilities,
    SetSessionConfigOptionResponse,
    SetSessionModeResponse,
    SseMcpServer,
    StopReason,
    TextContentBlock,
    ToolCallProgress,
    ToolCallStart,
    ToolCallStatus,
    ToolCallUpdate,
    ToolKind,
    UsageUpdate,
    UserMessageChunk,
)
from acp.utils import notify_model, request_model
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from boba.agent.agent import Agent
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
    InitEvent,
    RecordEvent,
    ResultEvent,
    StreamEvent,
    ToolProgressEvent,
    ToolStartedEvent,
    TurnOutcome,
)
from boba.agent.history import (
    HistoryError,
    RecordNotFoundError,
    SessionHeldError,
    SessionId,
    SessionNotFoundError,
)
from boba.agent.ids import IdMint
from boba.agent.permissions import PermissionModeError
from boba.agent.queue import QueueEntry, QueueKind, QueuePriority, QueueRefusedError
from boba.agent.records import (
    AssistantRecord,
    ChainRecord,
    CompactBoundaryRecord,
    PermissionMode,
    Record,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserRecord,
)
from boba.agent.session import SessionState
from boba.agent.turn import ContextUsage
from boba.cancellation import StopReason as CoreStopReason
from boba.cli.settings import McpServerJson, McpTransportJson
from boba.toolkit.dag import ToolHints

logger = logging.getLogger(__name__)

__all__ = [
    "AcpAgent",
    "AcpMapping",
    "AcpMethod",
    "AcpServer",
    "ClientLink",
    "ConfigOptionId",
    "ForkMeta",
    "McpServerSpecs",
    "MetaKey",
    "ObservedSession",
    "OpenedSession",
    "PermissionChoice",
    "PromptMeta",
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
    "StopReasonName",
    "ToolHintSource",
    "ToolKindName",
    "ToolStatusName",
]

PromptBlock: TypeAlias = (
    TextContentBlock
    | ImageContentBlock
    | AudioContentBlock
    | ResourceContentBlock
    | EmbeddedResourceContentBlock
)
McpServerBlock: TypeAlias = HttpMcpServer | SseMcpServer | AcpMcpServer | McpServerStdio
ConfigOption: TypeAlias = SessionConfigOptionSelect | SessionConfigOptionBoolean
Update: TypeAlias = (
    UserMessageChunk
    | AgentMessageChunk
    | AgentThoughtChunk
    | ToolCallStart
    | ToolCallProgress
    | UsageUpdate
    | CurrentModeUpdate
    | ConfigOptionUpdate
    | SessionInfoUpdate
)


class SessionRefusedError(Exception):
    """Сборка сессии отвергла параметры запроса."""


class SessionOpenError(Exception):
    """Сессию открыть не удалось: хранилище, сервер MCP, профиль."""


class SessionBusyError(Exception):
    """Ход идёт: модель сессии менять поздно."""


class AcpMethod(StrEnum):
    """Методы JSON-RPC, которые вход называет сам: кроме маршрутов SDK."""

    SESSION_PROMPT = "session/prompt"
    SESSION_UPDATE = "session/update"
    REQUEST_PERMISSION = "session/request_permission"
    CANCEL_REQUEST = "$/cancel_request"
    SESSION_LOAD = "session/load"
    SESSION_RESUME = "session/resume"
    SESSION_LIST = "session/list"
    SESSION_CLOSE = "session/close"
    SESSION_DELETE = "session/delete"
    SESSION_FORK = "session/fork"
    SESSION_SET_MODE = "session/set_mode"
    SESSION_SET_CONFIG_OPTION = "session/set_config_option"
    AUTHENTICATE = "authenticate"


class MetaKey(StrEnum):
    """Ключи `_meta` расширений этого агента; префикс `boba/` — по правилу
    расширяемости ACP."""

    PROGRESS = "boba/progress"
    UP_TO_UUID = "boba/upToUuid"
    MESSAGE_ID = "boba/messageId"
    TOOL_NAME = "boba/toolName"


class SessionUpdateName(StrEnum):
    """Виды `session/update`, которые вход строит без помощников SDK."""

    USAGE = "usage_update"
    CONFIG_OPTION = "config_option_update"
    SESSION_INFO = "session_info_update"


class ConfigOptionId(StrEnum):
    """Настройки сессии (`configOptions`): идентификатор совпадает с
    категорией ACP того же имени."""

    MODE = "mode"
    MODEL = "model"


class StopReasonName(StrEnum):
    """Причины конца хода ACP (`stopReason`)."""

    END_TURN = "end_turn"
    MAX_TOKENS = "max_tokens"
    MAX_TURN_REQUESTS = "max_turn_requests"
    REFUSAL = "refusal"
    CANCELLED = "cancelled"


class ToolKindName(StrEnum):
    """Виды инструментов ACP (`kind`), которые агент выводит из пометок."""

    READ = "read"
    EDIT = "edit"
    EXECUTE = "execute"
    OTHER = "other"


class ToolStatusName(StrEnum):
    """Состояния вызова ACP (`status`)."""

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"


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


class ErrorDataKey(StrEnum):
    """Ключи `data` ошибки JSON-RPC, которые пишет агент."""

    KIND = "kind"
    MESSAGE = "message"
    DETAILS = "details"
    SESSION_ID = "sessionId"
    UP_TO_UUID = "upToUuid"


class PromptMeta(BaseModel):
    """Расширение `boba/messageId` в `_meta` запроса `session/prompt`: UUID,
    который клиент даёт вопросу; он становится uuid записи `user` этого
    вопроса, поэтому `session/load` воспроизводит вопрос с тем же `messageId`,
    а `session/fork` принимает его точкой среза. Без ключа uuid чеканит
    агент. Чужие ключи `_meta` не разбираются."""

    model_config = ConfigDict(extra="ignore")

    message_id: UUID | None = Field(default=None, alias=MetaKey.MESSAGE_ID.value)

    def record_uuid(self) -> str | None:
        if self.message_id is None:
            return None

        return str(self.message_id)


class ForkMeta(BaseModel):
    """Расширение `boba/upToUuid` в `_meta` запроса `session/fork`: uuid
    записи цепочки, на которой ветка кончается — в ветку идут записи до
    неё, сама она и всё после неё — нет. Без ключа ветка — копия всей
    сессии. Чужие ключи `_meta` не разбираются."""

    model_config = ConfigDict(extra="ignore")

    up_to_uuid: str | None = Field(
        default=None, alias=MetaKey.UP_TO_UUID.value, min_length=1
    )


class ProgressMeta(BaseModel):
    """Расширение `boba/progress` в `_meta` уведомления `tool_call_update`:
    отчёт сервера MCP о ходе вызова, которому в ACP поля нет."""

    model_config = ConfigDict(frozen=True)

    message: str
    progress: float
    total: float | None = None
    elapsed_sec: float


class Frame(BaseModel):
    """Кадр JSON-RPC глазами наблюдателя соединения: только поля адресации."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    id: int | str | None = None
    method: str | None = None
    params: Mapping[str, Any] | None = None


class SessionParams(BaseModel):
    """Параметры запроса, у которого есть сессия."""

    model_config = ConfigDict(frozen=True, extra="ignore", populate_by_name=True)

    session_id: str = Field(alias="sessionId")


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


class SessionOptions:
    """Режимы и настройки сессии в форме ACP: `modes` для клиентов старого
    API и `configOptions` (`mode`, `model`) для клиентов настроек, оба из
    одного состояния SessionSettings, чтобы они не расходились; те же
    состояния — уведомлениями `current_mode_update` и
    `config_option_update`. Создаёт вход; ответы `session/new|load|resume|fork`
    собирают через него."""

    SELECT: ClassVar[str] = "select"
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

    def modes(self, settings: SessionSettings) -> SessionModeState:
        available: list[SessionMode] = []
        for mode in settings.modes:
            card = self.CARDS[mode]
            available.append(
                SessionMode(id=mode.value, name=card.name, description=card.description)
            )

        return SessionModeState(
            current_mode_id=settings.mode.value, available_modes=available
        )

    def config_options(self, settings: SessionSettings) -> list[ConfigOption]:
        return [self._mode_option(settings), self._model_option(settings)]

    def mode_update(self, settings: SessionSettings) -> CurrentModeUpdate:
        return update_current_mode(settings.mode.value)

    def options_update(self, settings: SessionSettings) -> ConfigOptionUpdate:
        return ConfigOptionUpdate.model_validate(
            {
                "sessionUpdate": SessionUpdateName.CONFIG_OPTION.value,
                "configOptions": self._dumped(self.config_options(settings)),
            }
        )

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

    def _mode_option(self, settings: SessionSettings) -> SessionConfigOptionSelect:
        options: list[dict[str, str]] = []
        for mode in settings.modes:
            card = self.CARDS[mode]
            options.append(
                {
                    "value": mode.value,
                    "name": card.name,
                    "description": card.description,
                }
            )

        return self._select(
            ConfigOptionId.MODE,
            "Permission mode",
            "How the agent treats tool calls that change something",
            settings.mode.value,
            options,
        )

    def _model_option(self, settings: SessionSettings) -> SessionConfigOptionSelect:
        options: list[dict[str, str]] = []
        for name in settings.models:
            options.append({"value": name, "name": name})

        return self._select(
            ConfigOptionId.MODEL,
            "Model",
            "Model profile from the agent settings used for the next turns",
            settings.model,
            options,
        )

    def _select(
        self,
        option_id: ConfigOptionId,
        name: str,
        description: str,
        current: str,
        options: Sequence[Mapping[str, str]],
    ) -> SessionConfigOptionSelect:
        return SessionConfigOptionSelect.model_validate(
            {
                "id": option_id.value,
                "name": name,
                "description": description,
                "category": option_id.value,
                "type": self.SELECT,
                "currentValue": current,
                "options": list(options),
            }
        )

    @staticmethod
    def _dumped(options: Sequence[ConfigOption]) -> list[dict[str, Any]]:
        dumped: list[dict[str, Any]] = []
        for option in options:
            dumped.append(
                option.model_dump(mode="json", by_alias=True, exclude_none=True)
            )

        return dumped

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


class WireLiterals:
    """Строгие литералы SDK из перечислений входа: значение проходит адаптер
    pydantic на границе, а не приводится типом."""

    def __init__(self) -> None:
        self._stop_reasons = TypeAdapter(StopReason)
        self._kinds = TypeAdapter(ToolKind)
        self._statuses = TypeAdapter(ToolCallStatus)
        self._option_kinds = TypeAdapter(PermissionOptionKind)

    def stop_reason(self, name: StopReasonName) -> StopReason:
        return self._stop_reasons.validate_python(name.value)

    def kind(self, name: ToolKindName) -> ToolKind:
        return self._kinds.validate_python(name.value)

    def status(self, name: ToolStatusName) -> ToolCallStatus:
        return self._statuses.validate_python(name.value)

    def option_kind(self, choice: PermissionChoice) -> PermissionOptionKind:
        return self._option_kinds.validate_python(choice.value)


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
    """Запрос разрешения клиенту: вызов и варианты ответа."""

    model_config = ConfigDict(frozen=True)

    tool_call: ToolCallUpdate
    options: Sequence[PermissionOption]


class AcpMapping:
    """Соответствие ядра протоколу: исходы хода → `stopReason`, события →
    уведомления `session/update`, пометки инструмента → `kind`, вопрос
    `can_use_tool` → запрос разрешения и ответ на него.

    Единственное место, где ядро встречается со схемой ACP; создаёт вход и
    отдаёт приёмникам событий сессий.
    """

    STOP_REASONS: ClassVar[Mapping[TurnOutcome, StopReasonName]] = {
        TurnOutcome.COMPLETED: StopReasonName.END_TURN,
        TurnOutcome.MAX_TURNS: StopReasonName.MAX_TURN_REQUESTS,
        TurnOutcome.OUTPUT_LIMIT: StopReasonName.MAX_TOKENS,
        TurnOutcome.ABORTED_STREAMING: StopReasonName.CANCELLED,
        TurnOutcome.ABORTED_TOOLS: StopReasonName.CANCELLED,
        TurnOutcome.SHUTDOWN: StopReasonName.CANCELLED,
        TurnOutcome.BLOCKING_LIMIT: StopReasonName.REFUSAL,
        TurnOutcome.COMPACT_FAILURE_BREAKER: StopReasonName.REFUSAL,
        TurnOutcome.RAPID_REFILL_BREAKER: StopReasonName.REFUSAL,
    }

    def __init__(self) -> None:
        self._wire = WireLiterals()
        self._titles = ToolTitles()

    def prompt_response(self, result: ResultEvent) -> PromptResponse:
        """Ответ на `session/prompt` по итогу хода.

        Ошибки:
        RequestError — модель отказала: ход кончился без ответа (-32603).
        """
        found = self.STOP_REASONS.get(result.outcome)
        if found is None:
            data = {
                ErrorDataKey.KIND.value: result.outcome.value,
                ErrorDataKey.MESSAGE.value: result.error,
            }
            raise RequestError.internal_error(data)

        return PromptResponse(stop_reason=self._wire.stop_reason(found))

    def cancelled_response(self) -> PromptResponse:
        return PromptResponse(
            stop_reason=self._wire.stop_reason(StopReasonName.CANCELLED)
        )

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
            thought = update_agent_thought(text_block(event.reasoning))
            thought.message_id = event.message_id
            updates.append(thought)

        if event.content:
            message = update_agent_message(text_block(event.content))
            message.message_id = event.message_id
            updates.append(message)

        return updates

    def tool_call(self, block: ToolUseBlock, hints: ToolHints | None) -> ToolCallStart:
        """Имя инструмента отдельно от заголовка: в схеме SDK поля `name`
        нет, клиент берёт его из `_meta` (расширение)."""
        start = start_tool_call(
            block.id,
            self._titles.title(block.name, block.input),
            kind=self._wire.kind(self.tool_kind(hints)),
            status=self._wire.status(ToolStatusName.PENDING),
            raw_input=dict(block.input),
        )
        start.field_meta = {MetaKey.TOOL_NAME.value: block.name}

        return start

    def tool_results(self, record: UserRecord) -> Sequence[ToolCallProgress]:
        updates: list[ToolCallProgress] = []
        for block in record.message.tool_results():
            updates.append(self._tool_result(block, record.tool_use_result))

        return updates

    def user_chunk(self, text: str, message_id: str) -> UserMessageChunk:
        chunk = update_user_message(text_block(text))
        chunk.message_id = message_id

        return chunk

    def replay(
        self, conversation: Sequence[ChainRecord], hints: ToolHintSource
    ) -> Sequence[Update]:
        """Беседа из записей → те же уведомления, что шлёт живой ход, по
        порядку записей: вопрос человека — `user_message_chunk` с uuid записи
        как `messageId`; ответ модели — по блоку: рассуждение, текст, вызов
        инструмента с `messageId` сообщения; результат инструмента —
        `tool_call_update`. Синтетические записи агента (заметки об ошибках,
        заглушки) не воспроизводятся, оборванный ответ — воспроизводится;
        служебные сообщения, вложения и границы сжатия в ленту не идут."""
        updates: list[Update] = []
        for record in conversation:
            updates.extend(self._replayed(record, hints))

        return updates

    def _replayed(self, record: ChainRecord, hints: ToolHintSource) -> Iterator[Update]:
        if isinstance(record, UserRecord):
            yield from self._replayed_user(record)
            return

        if isinstance(record, AssistantRecord):
            yield from self._replayed_assistant(record, hints)

    def _replayed_user(self, record: UserRecord) -> Iterator[Update]:
        results = self.tool_results(record)
        if results:
            yield from results
            return

        text = record.human_text()
        if text is None:
            return

        yield self.user_chunk(text, record.uuid)

    def _replayed_assistant(
        self, record: AssistantRecord, hints: ToolHintSource
    ) -> Iterator[Update]:
        if record.is_synthetic() and not record.is_aborted_mid_stream:
            return

        message_id = record.message.id
        for block in record.message.content:
            if isinstance(block, ThinkingBlock):
                thought = update_agent_thought(text_block(block.thinking))
                thought.message_id = message_id
                yield thought
                continue

            if isinstance(block, TextBlock):
                message = update_agent_message(text_block(block.text))
                message.message_id = message_id
                yield message
                continue

            yield self.tool_call(block, hints.tool_hints(block.name))

    def _tool_result(self, block: ToolResultBlock, artifact: Any) -> ToolCallProgress:
        status = ToolStatusName.COMPLETED
        if block.is_error:
            status = ToolStatusName.FAILED

        return update_tool_call(
            block.tool_use_id,
            status=self._wire.status(status),
            content=[tool_content(text_block(block.text()))],
            raw_output=artifact,
        )

    def tool_started(self, event: ToolStartedEvent) -> ToolCallProgress:
        return update_tool_call(
            event.tool_use_id, status=self._wire.status(ToolStatusName.IN_PROGRESS)
        )

    def usage_update(self, usage: ContextUsage) -> UsageUpdate:
        """Заполненность контекста: `used` — оценка разговора, `size` —
        действующее окно профиля."""
        return UsageUpdate.model_validate(
            {
                "sessionUpdate": SessionUpdateName.USAGE.value,
                "used": usage.used,
                "size": usage.size,
            }
        )

    def title_update(self, title: str) -> SessionInfoUpdate:
        return SessionInfoUpdate.model_validate(
            {"sessionUpdate": SessionUpdateName.SESSION_INFO.value, "title": title}
        )

    def tool_progress(self, event: ToolProgressEvent) -> ToolCallProgress:
        """Отчёт о ходе вызова: статус стандартный, поля отчёта — в `_meta`
        под ключом `boba/progress` (расширение)."""
        meta = ProgressMeta(
            message=event.message,
            progress=event.progress,
            total=event.total,
            elapsed_sec=event.elapsed_sec,
        )
        update = update_tool_call(
            event.tool_use_id, status=self._wire.status(ToolStatusName.IN_PROGRESS)
        )
        update.field_meta = {
            MetaKey.PROGRESS.value: meta.model_dump(mode="json", exclude_none=True)
        }

        return update

    def permission_request(
        self, request: CanUseToolRequest, hints: ToolHints | None
    ) -> PermissionAsk:
        tool_call = ToolCallUpdate(
            tool_call_id=request.tool_use_id,
            title=self._titles.title(request.tool_name, request.input),
            kind=self._wire.kind(self.tool_kind(hints)),
            status=self._wire.status(ToolStatusName.PENDING),
            raw_input=dict(request.input),
        )
        options: list[PermissionOption] = []
        for choice in PermissionChoice:
            options.append(
                PermissionOption(
                    option_id=choice.value,
                    name=choice.label,
                    kind=self._wire.option_kind(choice),
                )
            )

        return PermissionAsk(tool_call=tool_call, options=options)

    def permission_answer(
        self, response: RequestPermissionResponse, request: CanUseToolRequest
    ) -> PermissionAnswer:
        """Ответ клиента → ответ ядру; отмена и неизвестный вариант — отказ."""
        outcome = response.outcome
        if not isinstance(outcome, SelectedPermissionOutcome):
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


class PromptText:
    """Текст вопроса из блоков `session/prompt`: текст как есть, ссылка на
    ресурс — ссылкой markdown; прочие блоки агент не объявлял."""

    LINK: ClassVar[str] = "[@{name}]({uri})"

    def render(self, blocks: Sequence[PromptBlock]) -> str:
        """Ошибки:
        RequestError — блок неподдержанного вида или пустой вопрос (-32602).
        """
        parts: list[str] = []
        for block in blocks:
            parts.append(self._part(block))

        text = "".join(parts)
        if not text.strip():
            data = {ErrorDataKey.DETAILS.value: "the prompt has no text to send"}
            raise RequestError.invalid_params(data)

        return text

    def _part(self, block: PromptBlock) -> str:
        if isinstance(block, TextContentBlock):
            return block.text

        if isinstance(block, ResourceContentBlock):
            return self.LINK.format(name=block.name, uri=block.uri)

        details = (
            f"prompt blocks of type '{block.type}' are not supported: this agent "
            "declared no image, audio or embedded context capability; send text "
            "and resource_link blocks"
        )
        raise RequestError.invalid_params({ErrorDataKey.DETAILS.value: details})


class McpServerSpecs:
    """Серверы MCP из `session/new` → записи формата `.mcp.json` настроек,
    из которых сборка делает конфиги клиента."""

    def specs(self, servers: Sequence[McpServerBlock]) -> dict[str, McpServerJson]:
        """Ошибки:
        RequestError — сервер неподдержанного вида или повтор имени (-32602).
        """
        specs: dict[str, McpServerJson] = {}
        for server in servers:
            if server.name in specs:
                details = (
                    f"mcpServers names '{server.name}' twice; names must be unique"
                )
                raise RequestError.invalid_params({ErrorDataKey.DETAILS.value: details})

            specs[server.name] = self._spec(server)

        return specs

    def _spec(self, server: McpServerBlock) -> McpServerJson:
        if isinstance(server, McpServerStdio):
            env: dict[str, str] = {}
            for variable in server.env:
                env[variable.name] = variable.value

            return McpServerJson(
                type=McpTransportJson.STDIO,
                command=server.command,
                args=list(server.args),
                env=env,
            )

        if isinstance(server, McpServerHttp):
            headers: dict[str, str] = {}
            for header in server.headers:
                headers[header.name] = header.value

            return McpServerJson(
                type=McpTransportJson.HTTP, url=server.url, headers=headers
            )

        details = (
            f"mcpServers entry '{server.name}' has type '{server.type}', which this "
            "agent does not support: it declared mcpCapabilities http only (stdio "
            "is always accepted)"
        )
        raise RequestError.invalid_params({ErrorDataKey.DETAILS.value: details})


class ClientLink:
    """Вызовы к клиенту по соединению SDK: уведомление `session/update` и
    запрос `session/request_permission`. Создаёт сервер и отдаёт агенту."""

    def __init__(self, connection: Connection) -> None:
        self._connection = connection

    async def session_update(self, session_id: str, update: Update) -> None:
        await notify_model(
            self._connection,
            AcpMethod.SESSION_UPDATE.value,
            SessionNotification(session_id=session_id, update=update),
        )

    async def request_permission(
        self, session_id: str, ask: PermissionAsk
    ) -> RequestPermissionResponse:
        return await request_model(
            self._connection,
            AcpMethod.REQUEST_PERMISSION.value,
            RequestPermissionRequest(
                session_id=session_id,
                tool_call=ask.tool_call,
                options=list(ask.options),
            ),
            RequestPermissionResponse,
        )


class PromptWaiters:
    """Ожидания ответов на `session/prompt` по идентификаторам элементов
    очереди: итог хода закрывает ожидания всех элементов, которые он
    обслужил; очистка очереди отвечает им отменой; падение обслуживания —
    ошибкой."""

    def __init__(self, mapping: AcpMapping) -> None:
        self._mapping = mapping
        self._pending: dict[str, asyncio.Future[PromptResponse]] = {}

    def register(self, item_id: str) -> asyncio.Future[PromptResponse]:
        future: asyncio.Future[PromptResponse] = (
            asyncio.get_running_loop().create_future()
        )
        self._pending[item_id] = future

        return future

    def settle(self, result: ResultEvent) -> None:
        for item_id in result.prompt_ids:
            future = self._pending.pop(item_id, None)
            if future is None or future.done():
                continue

            try:
                future.set_result(self._mapping.prompt_response(result))
            except RequestError as exc:
                future.set_exception(exc)

    def cancel(self, item_ids: Sequence[str]) -> None:
        for item_id in item_ids:
            future = self._pending.pop(item_id, None)
            if future is None or future.done():
                continue

            future.set_result(self._mapping.cancelled_response())

    def fail(self, error: RequestError) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(error)

        self._pending.clear()


class SessionRelay(EventSink):
    """Реализация EventSink сессии ACP: события ядра → уведомления клиенту.

    Создаёт вход на каждую сессию до сборки агента и отдаёт ядру
    приёмником; агент привязывается после открытия (`attach`), потому что
    пометки инструментов, заполненность контекста и ответы на вопросы живут
    у него. Уведомления ждут отправки в порядке событий, так что итог хода
    приходит после них; по итогу хода уходят `usage_update` и, после первого
    вопроса человека, `session_info_update` с заголовком, затем закрываются
    ожидания `prompt`; граница сжатия тоже даёт `usage_update`. Вопрос
    `can_use_tool` уходит клиенту отдельной задачей, которую снимает отмена
    хода. После ухода клиента уведомления некому слать: приёмник
    отвязывается и дальше молчит.
    """

    def __init__(
        self,
        session_id: str,
        link: ClientLink,
        mapping: AcpMapping,
        waiters: PromptWaiters,
    ) -> None:
        self._session_id = session_id
        self._link = link
        self._mapping = mapping
        self._waiters = waiters
        self._titles = SessionTitles()
        self._agent: Agent | None = None
        self._asks: dict[str, asyncio.Task[None]] = {}
        self._detached = False
        self._titled = False

    def attach(self, agent: Agent) -> None:
        self._agent = agent

    async def replay(self, updates: Sequence[Update]) -> None:
        """Воспроизведение беседы: уведомления уходят по порядку тем же
        путём, что живые."""
        await self._send_all(updates)

    async def notify(self, update: Update) -> None:
        """Уведомление вне хода: смена настроек сессии."""
        await self._send(update)

    async def announce_loaded(self) -> None:
        """После воспроизведения загруженной сессии: заголовок и
        заполненность контекста, как после хода."""
        await self._send_all(self._turn_summary())

    async def emit(self, event: AgentEvent) -> None:
        updates = self._updates(event)
        if updates is not None:
            await self._send_all(updates)
            return

        if isinstance(event, ResultEvent):
            await self._send_all(self._turn_summary())
            self._waiters.settle(event)
            return

        self._control(event)

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

    def _usage(self) -> UsageUpdate | None:
        if self._agent is None:
            return None

        return self._mapping.usage_update(self._agent.context_usage())

    def _title(self) -> SessionInfoUpdate | None:
        """Заголовок из первого вопроса человека; уходит один раз."""
        if self._titled or self._agent is None:
            return None

        first = self._first_prompt(self._agent.session.conversation())
        title = self._titles.title(first)
        if title is None:
            return None

        self._titled = True

        return self._mapping.title_update(title)

    @staticmethod
    def _first_prompt(conversation: Sequence[ChainRecord]) -> str | None:
        for record in conversation:
            if not isinstance(record, UserRecord):
                continue

            text = record.human_text()
            if text is not None:
                return text

        return None

    def _updates(self, event: AgentEvent) -> Sequence[Update] | None:
        """Уведомления по событию; None — событие не про ленту."""
        if isinstance(event, StreamEvent):
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

        if isinstance(event, ErrorEvent):
            logger.warning("session %s: %s", self._session_id, event.message)
            return

        if isinstance(event, InitEvent):
            logger.info(
                "session %s: opened with model %s and %d tool(s)",
                event.session_id,
                event.model,
                len(event.tools),
            )

    def _record_updates(self, record: Record) -> Sequence[Update]:
        if isinstance(record, AssistantRecord):
            updates: list[Update] = []
            for block in record.message.tool_uses():
                updates.append(self._mapping.tool_call(block, self._hints(block.name)))

            return updates

        if isinstance(record, UserRecord):
            return self._mapping.tool_results(record)

        if isinstance(record, CompactBoundaryRecord):
            usage = self._usage()
            if usage is None:
                return ()

            return [usage]

        return ()

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
        except ConnectionError as exc:
            self._detached = True
            logger.warning(
                "session %s: the client connection is closed (%s); further "
                "updates of this session are dropped",
                self._session_id,
                exc,
            )

    def _ask(self, event: ControlRequestEvent) -> None:
        task = asyncio.create_task(
            self._permission(event), name=f"acp-permission-{event.request_id}"
        )
        self._asks[event.request_id] = task

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
        """Гасит ход, сбрасывает журнал, закрывает серверы MCP сессии."""


class ObservedSession(ToolHintSource, Protocol):
    """Сессия, открытая без замка: её держит другой процесс. Беседа
    прочитана из хранилища, серверы MCP подключены; `claim` открывает
    сессию по-настоящему, когда замок свободен."""

    @abstractmethod
    def conversation(self) -> Sequence[ChainRecord]: ...

    @abstractmethod
    async def claim(self) -> OpenedSession:
        """Ошибки:
        SessionHeldError — замок всё ещё у другого процесса.
        SessionNotFoundError — записи сессии исчезли из хранилища.
        SessionOpenError — сессию открыть не удалось.
        """

    @abstractmethod
    async def close(self) -> None:
        """Закрывает серверы MCP наблюдателя."""


class SessionAssembly(Protocol):
    """Сборка сессии из настроек процесса и параметров `session/new|load|resume`,
    список и удаление сессий хранилища.

    Реализует вход процесса: он знает профиль модели, хранилище истории и
    клиент MCP; протокольный слой знает только события и ответы.
    """

    @abstractmethod
    async def open(
        self,
        session_id: SessionId,
        cwd: Path,
        servers: Mapping[str, McpServerJson],
        sink: EventSink,
        resume: bool,
    ) -> OpenedSession:
        """Ошибки:
        SessionRefusedError — параметры сессии не годятся (сервер MCP с
            неподдержанными заголовками).
        SessionOpenError — сессию открыть не удалось.
        SessionHeldError — при возобновлении: сессию держит другой процесс.
        SessionNotFoundError — при возобновлении: записей сессии нет.
        """

    @abstractmethod
    async def observe(
        self,
        session_id: SessionId,
        cwd: Path,
        servers: Mapping[str, McpServerJson],
        sink: EventSink,
    ) -> ObservedSession:
        """Ошибки:
        SessionRefusedError — параметры сессии не годятся.
        SessionOpenError — серверы MCP не поднялись.
        SessionNotFoundError — записей сессии нет.
        HistoryError — хранилище отказало при чтении.
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
    ожидания ответов на `prompt`."""

    def __init__(
        self,
        session_id: str,
        opened: OpenedSession,
        relay: SessionRelay,
        waiters: PromptWaiters,
    ) -> None:
        self._session_id = session_id
        self._opened = opened
        self._relay = relay
        self._waiters = waiters
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

    def conversation(self) -> Sequence[ChainRecord]:
        return self.agent.session.conversation()

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
        if self._busy():
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

    def _busy(self) -> bool:
        if self.agent.session.state is not SessionState.IDLE:
            return True

        return len(self.agent.queue) > 0

    def _served(self, task: asyncio.Task[None]) -> None:
        if task.cancelled():
            return

        failure = task.exception()
        if failure is None:
            return

        logger.error(
            "session %s: the agent stopped serving: %s", self._session_id, failure
        )
        data = {
            ErrorDataKey.KIND.value: type(failure).__name__,
            ErrorDataKey.MESSAGE.value: str(failure),
        }
        self._waiters.fail(RequestError.internal_error(data))

    async def prompt(self, text: str, message_id: str | None = None) -> PromptResponse:
        """Ставит вопрос в очередь ядра и ждёт итога хода, который его обслужит;
        message_id — uuid записи `user` вопроса от клиента (`boba/messageId`).

        Ошибки:
        RequestError — очередь не приняла элемент или uuid вопроса уже есть в
            сессии (-32602), обслуживание сессии остановилось (-32603),
            модель отказала (-32603).
        """
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

        if message_id is not None and agent.session.find(message_id) is not None:
            details = (
                f"{MetaKey.MESSAGE_ID.value} {message_id} already names a record of "
                f"session {self._session_id}; every question needs its own UUID"
            )
            raise RequestError.invalid_params({ErrorDataKey.DETAILS.value: details})

        entry = QueueEntry(
            kind=QueueKind.PROMPT, text=text, priority=priority, id=message_id
        )
        try:
            item = agent.queue.enqueue(entry)
        except QueueRefusedError as exc:
            raise RequestError.invalid_params(
                {ErrorDataKey.DETAILS.value: str(exc)}
            ) from exc

        return await self._waiters.register(item.id)

    async def flush(self) -> None:
        """Сбрасывает несброшенные записи сессии в хранилище.

        Ошибки:
        HistoryError — хранилище отказало.
        """
        await self._opened.agent.writer.flush(strict=True)

    def cancel(self, reason: CoreStopReason) -> None:
        """Прерывает ход и очищает очередь; ожидающие получают `cancelled`."""
        queued: list[str] = []
        for item in self.agent.queue.items():
            queued.append(item.id)

        self.agent.interrupt(reason, cancel_queued=True)
        self._waiters.cancel(queued)

    async def close(self) -> None:
        self._relay.withdraw_all()
        await self._opened.close()
        try:
            await self._serving
        except Exception as exc:
            logger.error(
                "session %s: serving ended with an error: %s", self._session_id, exc
            )


class ObserverSession:
    """Сессия-наблюдатель: открыта без замка, беседу отдала, хода не ведёт.

    Создаёт вход на `session/load|resume`, когда сессию держит другой
    процесс; первый `prompt` зовёт `claim`, и при свободном замке
    наблюдатель уступает место обычной AcpSession с тем же приёмником и
    ожиданиями.
    """

    def __init__(
        self,
        session_id: str,
        observed: ObservedSession,
        relay: SessionRelay,
        waiters: PromptWaiters,
    ) -> None:
        self._session_id = session_id
        self._observed = observed
        self._relay = relay
        self._waiters = waiters

    @property
    def relay(self) -> SessionRelay:
        return self._relay

    def conversation(self) -> Sequence[ChainRecord]:
        return self._observed.conversation()

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

        return AcpSession(self._session_id, opened, self._relay, self._waiters)

    async def close(self) -> None:
        await self._observed.close()


HeldSession: TypeAlias = AcpSession | ObserverSession


class PromptRequests:
    """Наблюдатель кадров соединения: под каким `id` идёт какой запрос
    `session/prompt`, чтобы `$/cancel_request` нашёл сессию. Регистрируется
    у соединения SDK; ответ агента снимает запись."""

    def __init__(self) -> None:
        self._sessions: dict[str, str] = {}

    def __call__(self, event: FrameEvent) -> None:
        try:
            frame = Frame.model_validate(event.message)
        except ValidationError as exc:
            logger.debug("frame observer: unreadable frame skipped: %s", exc)
            return

        if frame.id is None:
            return

        key = str(frame.id)
        if event.direction is StreamDirection.OUTGOING:
            if frame.method is None:
                self._sessions.pop(key, None)

            return

        if frame.method != AcpMethod.SESSION_PROMPT.value:
            return

        if frame.params is None:
            return

        try:
            params = SessionParams.model_validate(frame.params)
        except ValidationError:
            return

        self._sessions[key] = params.session_id

    def session_of(self, request_id: int | str) -> str | None:
        return self._sessions.get(str(request_id))


class AcpAgent(AcpAgentProtocol):
    """Реализация Agent SDK над сессиями ядра.

    Создаёт вход процесса со сборкой сессий; сервер (AcpServer) привязывает
    канал к клиенту. Один объект обслуживает все сессии соединения: каждая
    — свой агент ядра с серверами MCP из `session/new|load|resume`, либо
    наблюдатель, пока сессию держит другой процесс.
    """

    NAME: ClassVar[str] = "boba"

    def __init__(self, assembly: SessionAssembly, version: str) -> None:
        self._assembly = assembly
        self._version = version
        self._link: ClientLink | None = None
        self._sessions: dict[str, HeldSession] = {}
        self._mapping = AcpMapping()
        self._options = SessionOptions()
        self._prompts = PromptText()
        self._specs = McpServerSpecs()
        self._titles = SessionTitles()
        self._ids = IdMint()
        self._requests = PromptRequests()
        self._client: ClientCapabilities | None = None

    @property
    def requests(self) -> PromptRequests:
        return self._requests

    @property
    def client_capabilities(self) -> ClientCapabilities | None:
        """Возможности клиента из `initialize`; None — рукопожатия не было."""
        return self._client

    def attach(self, link: ClientLink) -> None:
        self._link = link

    def _linked(self) -> ClientLink:
        if self._link is None:
            msg = "The ACP agent has no client connection yet; attach() comes first."
            raise RuntimeError(msg)

        return self._link

    async def initialize(
        self,
        protocol_version: int,
        client_capabilities: ClientCapabilities | None = None,
        client_info: Implementation | None = None,
        **kwargs: Any,
    ) -> InitializeResponse:
        self._client = client_capabilities
        name = "unknown client"
        if client_info is not None:
            name = f"{client_info.name} {client_info.version}"

        logger.info("initialize: %s speaks protocol version %d", name, protocol_version)

        return InitializeResponse(
            protocol_version=PROTOCOL_VERSION,
            agent_capabilities=AgentCapabilities(
                load_session=True,
                prompt_capabilities=PromptCapabilities(
                    image=False, audio=False, embedded_context=False
                ),
                mcp_capabilities=McpCapabilities(http=True, sse=False),
                session_capabilities=SessionCapabilities(
                    list=SessionListCapabilities(),
                    resume=SessionResumeCapabilities(),
                    close=SessionCloseCapabilities(),
                    delete=SessionDeleteCapabilities(),
                    fork=SessionForkCapabilities(),
                ),
            ),
            agent_info=Implementation(name=self.NAME, version=self._version),
            auth_methods=[],
        )

    async def new_session(
        self,
        cwd: str,
        additional_directories: list[str] | None = None,
        mcp_servers: list[McpServerBlock] | None = None,
        **kwargs: Any,
    ) -> NewSessionResponse:
        """Ошибки:
        RequestError — `cwd` не абсолютный или серверы MCP не годятся
            (-32602); сессию открыть не удалось (-32603).
        """
        directory = self._directory(cwd)
        specs = self._specs.specs(self._blocks(mcp_servers))
        session_id = self._ids.uuid()
        waiters = PromptWaiters(self._mapping)
        relay = SessionRelay(session_id, self._linked(), self._mapping, waiters)
        try:
            opened = await self._assembly.open(
                SessionId(value=session_id), directory, specs, relay, resume=False
            )
        except SessionRefusedError as exc:
            raise RequestError.invalid_params(
                {ErrorDataKey.DETAILS.value: str(exc)}
            ) from exc
        except (SessionOpenError, HistoryError) as exc:
            raise RequestError.internal_error(
                {ErrorDataKey.DETAILS.value: str(exc)}
            ) from exc

        relay.attach(opened.agent)
        session = AcpSession(session_id, opened, relay, waiters)
        self._sessions[session_id] = session
        logger.info(
            "session %s: new, cwd %s, %d MCP server(s)", session_id, cwd, len(specs)
        )
        settings = self._settings(session)

        return NewSessionResponse(
            session_id=session_id,
            modes=self._options.modes(settings),
            config_options=self._options.config_options(settings),
        )

    def _settings(self, session: AcpSession) -> SessionSettings:
        agent = session.agent

        return SessionSettings(
            mode=agent.permission_mode,
            modes=agent.available_modes(),
            model=session.model,
            models=self._assembly.models(),
        )

    async def _announce_settings(self, session: AcpSession, mode_changed: bool) -> None:
        """Полное состояние настроек клиенту: `current_mode_update` при смене
        режима, `config_option_update` всегда."""
        settings = self._settings(session)
        if mode_changed:
            await session.relay.notify(self._options.mode_update(settings))

        await session.relay.notify(self._options.options_update(settings))

    async def prompt(
        self, session_id: str, prompt: list[PromptBlock], **kwargs: Any
    ) -> PromptResponse:
        """Ошибки:
        RequestError — сессия неизвестна (-32002), блоки или `boba/messageId`
            не годятся (-32602), сессию держит другой процесс или модель
            отказала (-32603).
        """
        held = self._session(session_id)
        text = self._prompts.render(prompt)
        meta = self._prompt_meta(kwargs)
        if isinstance(held, ObserverSession):
            held = await self._claim(session_id, held)

        return await held.prompt(text, meta.record_uuid())

    @staticmethod
    def _prompt_meta(meta: Mapping[str, Any]) -> PromptMeta:
        """Ошибки:
        RequestError — `boba/messageId` не UUID (-32602).
        """
        try:
            return PromptMeta.model_validate(meta)
        except ValidationError as exc:
            details = f"_meta of session/prompt is not valid: {exc}"
            raise RequestError.invalid_params(
                {ErrorDataKey.DETAILS.value: details}
            ) from exc

    async def _claim(self, session_id: str, observer: ObserverSession) -> AcpSession:
        """Наблюдатель берёт замок: сессия открывается по-настоящему и
        занимает его место.

        Ошибки:
        RequestError — сессию держит другой процесс (-32603), записи
            исчезли (-32002), сессию открыть не удалось (-32603).
        """
        try:
            session = await observer.claim()
        except SessionHeldError as exc:
            raise self._held_by_other(session_id, str(exc)) from exc
        except SessionNotFoundError as exc:
            self._sessions.pop(session_id, None)
            await observer.close()
            raise self._unknown(session_id) from exc
        except (SessionOpenError, HistoryError) as exc:
            raise RequestError.internal_error(
                {ErrorDataKey.DETAILS.value: str(exc)}
            ) from exc

        self._sessions[session_id] = session
        logger.info("session %s: the observer took the lock", session_id)
        await self._announce_settings(session, mode_changed=True)

        return session

    async def cancel(self, session_id: str, **kwargs: Any) -> None:
        session = self._sessions.get(session_id)
        if session is None:
            logger.info("cancel: session %s is unknown; nothing to cancel", session_id)
            return

        if isinstance(session, ObserverSession):
            logger.info("cancel: session %s is observed, no turn to cancel", session_id)
            return

        session.cancel(CoreStopReason.USER_STOP)

    async def cancel_request(self, params: Any) -> None:
        """Маршрут `$/cancel_request`: отмена запроса `session/prompt` по его
        `requestId` — как `session/cancel` его сессии; прочие запросы
        исполняются сразу, их отменять нечего."""
        try:
            notification = CancelRequestNotification.model_validate(params)
        except ValidationError as exc:
            logger.warning("$/cancel_request with unreadable params ignored: %s", exc)
            return

        if notification.request_id is None:
            return

        session_id = self._requests.session_of(notification.request_id)
        if session_id is None:
            logger.info(
                "$/cancel_request %s matches no running session/prompt; ignored",
                notification.request_id,
            )
            return

        await self.cancel(session_id)

    def _session(self, session_id: str) -> HeldSession:
        """Ошибки:
        RequestError — сессия этому процессу неизвестна (-32002).
        """
        session = self._sessions.get(session_id)
        if session is None:
            raise self._unknown(session_id)

        return session

    @staticmethod
    def _unknown(session_id: str) -> RequestError:
        return RequestError(
            RequestError.resource_not_found().code,
            f"session {session_id} is unknown to this agent process",
            {ErrorDataKey.SESSION_ID.value: session_id},
        )

    @staticmethod
    def _held_by_other(session_id: str, details: str) -> RequestError:
        return RequestError(
            RequestError.internal_error().code,
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
        for session in self._sessions.values():
            if isinstance(session, AcpSession):
                session.cancel(reason)

    async def close_all(self) -> None:
        for session_id, session in list(self._sessions.items()):
            await session.close()
            self._sessions.pop(session_id, None)

    async def load_session(
        self,
        cwd: str,
        session_id: str,
        mcp_servers: list[McpServerBlock] | None = None,
        additional_directories: list[str] | None = None,
        **kwargs: Any,
    ) -> LoadSessionResponse | None:
        """Открывает сессию из хранилища и до ответа воспроизводит всю беседу
        уведомлениями `session/update`, за ней — заголовок и заполненность;
        ответ несёт режимы и настройки сессии. У наблюдателя агента нет:
        его ответ без них, состояние придёт уведомлениями при захвате.

        Ошибки:
        RequestError — сессия неизвестна (-32002), `cwd` или серверы не
            годятся (-32602), сессию открыть не удалось (-32603).
        """
        held = await self._open_stored(session_id, cwd, mcp_servers)
        conversation = held.conversation()
        if isinstance(held, ObserverSession):
            hints: ToolHintSource = held.hints
        else:
            hints = held.agent

        await held.relay.replay(self._mapping.replay(conversation, hints))
        logger.info(
            "session %s: loaded, %d record(s) replayed", session_id, len(conversation)
        )
        if isinstance(held, ObserverSession):
            return LoadSessionResponse()

        await held.relay.announce_loaded()
        settings = self._settings(held)

        return LoadSessionResponse(
            modes=self._options.modes(settings),
            config_options=self._options.config_options(settings),
        )

    async def resume_session(
        self,
        session_id: str,
        cwd: str,
        additional_directories: list[str] | None = None,
        mcp_servers: list[McpServerBlock] | None = None,
        **kwargs: Any,
    ) -> ResumeSessionResponse:
        """Открывает сессию из хранилища без воспроизведения беседы; ответ
        несёт режимы и настройки сессии (у наблюдателя — без них).

        Ошибки:
        RequestError — как у `session/load`.
        """
        held = await self._open_stored(session_id, cwd, mcp_servers)
        logger.info("session %s: resumed", session_id)
        if isinstance(held, ObserverSession):
            return ResumeSessionResponse()

        settings = self._settings(held)

        return ResumeSessionResponse(
            modes=self._options.modes(settings),
            config_options=self._options.config_options(settings),
        )

    async def _open_stored(
        self,
        session_id: str,
        cwd: str,
        mcp_servers: list[McpServerBlock] | None,
    ) -> HeldSession:
        """Сессия хранилища: уже открытая в этом процессе — как есть; иначе
        открывается с замком, а при занятом замке — наблюдателем.

        Ошибки:
        RequestError — сессия неизвестна (-32002), параметры не годятся
            (-32602), сессию открыть не удалось (-32603).
        """
        directory = self._directory(cwd)
        specs = self._specs.specs(self._blocks(mcp_servers))
        known = self._sessions.get(session_id)
        if known is not None:
            return known

        stored = self._stored_id(session_id)
        waiters = PromptWaiters(self._mapping)
        relay = SessionRelay(session_id, self._linked(), self._mapping, waiters)
        try:
            held = await self._held(stored, directory, specs, relay, waiters)
        except SessionNotFoundError as exc:
            raise self._unknown(session_id) from exc
        except SessionRefusedError as exc:
            raise RequestError.invalid_params(
                {ErrorDataKey.DETAILS.value: str(exc)}
            ) from exc
        except (SessionOpenError, HistoryError) as exc:
            raise RequestError.internal_error(
                {ErrorDataKey.DETAILS.value: str(exc)}
            ) from exc

        self._sessions[session_id] = held

        return held

    async def _held(
        self,
        stored: SessionId,
        directory: Path,
        specs: Mapping[str, McpServerJson],
        relay: SessionRelay,
        waiters: PromptWaiters,
    ) -> HeldSession:
        """Ошибки:
        SessionNotFoundError, SessionRefusedError, SessionOpenError,
        HistoryError — от сборки.
        """
        try:
            opened = await self._assembly.open(
                stored, directory, specs, relay, resume=True
            )
        except SessionHeldError as exc:
            logger.info(
                "session %s: held by another process, opened as an observer (%s)",
                stored.value,
                exc,
            )
            observed = await self._assembly.observe(stored, directory, specs, relay)

            return ObserverSession(stored.value, observed, relay, waiters)

        relay.attach(opened.agent)

        return AcpSession(stored.value, opened, relay, waiters)

    async def list_sessions(
        self, cwd: str | None = None, cursor: str | None = None, **kwargs: Any
    ) -> ListSessionsResponse:
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

        sessions: list[SessionInfo] = []
        for summary in page.sessions:
            sessions.append(
                SessionInfo(
                    session_id=summary.session_id,
                    cwd=summary.cwd,
                    title=self._titles.title(summary.first_prompt),
                    updated_at=summary.updated_at,
                )
            )

        return ListSessionsResponse(sessions=sessions, next_cursor=page.next_cursor)

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

    async def delete_session(self, params: Any) -> DeleteSessionResponse:
        """Маршрут `session/delete`: сессия, которую ведёт этот процесс,
        закрывается до удаления (она держит замок); наблюдатель закрывается
        после, когда хранилище удаление приняло. Повторное удаление и
        сессия, которой не было, — успех.

        Ошибки:
        RequestError — сессию держит другой процесс или хранилище отказало
            (-32603).
        """
        request = DeleteSessionRequest.model_validate(params)
        session_id = request.session_id
        held = self._sessions.get(session_id)
        if isinstance(held, AcpSession):
            self._sessions.pop(session_id, None)
            await held.close()

        try:
            stored = SessionId(value=session_id)
        except ValidationError:
            logger.info("session %s: cannot exist, nothing to delete", session_id)
            return DeleteSessionResponse()

        await self._delete_stored(session_id, stored)
        if isinstance(held, ObserverSession):
            self._sessions.pop(session_id, None)
            await held.close()

        logger.info("session %s: deleted", session_id)

        return DeleteSessionResponse()

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

    async def set_session_mode(
        self, session_id: str, mode_id: str, **kwargs: Any
    ) -> SetSessionModeResponse | None:
        """Режим разрешений — то же действие, что `set_config_option` с
        `configId: mode`; после смены уходят `current_mode_update` и
        `config_option_update`.

        Ошибки:
        RequestError — сессия неизвестна (-32002), режима нет или он
            недоступен (-32602), сессию держит другой процесс (-32603).
        """
        session = self._settable(session_id)
        await self._set_mode(session, mode_id)

        return SetSessionModeResponse()

    async def set_config_option(
        self, config_id: str, session_id: str, value: str | bool, **kwargs: Any
    ) -> SetSessionConfigOptionResponse | None:
        """`mode` — режим разрешений ядра, `model` — профиль модели для
        следующих ходов; ответ — полное состояние настроек.

        Ошибки:
        RequestError — сессия неизвестна (-32002), настройки или значения
            нет, значение не строка (-32602), ход идёт или профиль не
            годится, сессию держит другой процесс (-32603).
        """
        session = self._settable(session_id)
        option = self._option_id(config_id)
        if not isinstance(value, str):
            details = (
                f"configOption '{config_id}' is a select: value must be a string, "
                f"got {value!r}"
            )
            raise RequestError.invalid_params({ErrorDataKey.DETAILS.value: details})

        if option is ConfigOptionId.MODE:
            await self._set_mode(session, value)
        else:
            name = self._options.parse_model(value, self._settings(session))
            await session.set_model(name)
            logger.info("session %s: model profile switched to %s", session_id, name)
            await self._announce_settings(session, mode_changed=False)

        return SetSessionConfigOptionResponse(
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
        await self._announce_settings(session, mode_changed=True)

    def _settable(self, session_id: str) -> AcpSession:
        """Сессия, настройки которой можно менять: у наблюдателя агента нет.

        Ошибки:
        RequestError — сессия неизвестна (-32002), её держит другой
            процесс (-32603).
        """
        held = self._session(session_id)
        if isinstance(held, ObserverSession):
            details = "its settings can be changed only by the process that holds it"
            raise self._held_by_other(session_id, details)

        return held

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

    async def authenticate(
        self, method_id: str, **kwargs: Any
    ) -> AuthenticateResponse | None:
        raise RequestError.method_not_found(AcpMethod.AUTHENTICATE.value)

    async def fork_session(
        self,
        session_id: str,
        cwd: str,
        additional_directories: list[str] | None = None,
        mcp_servers: list[McpServerBlock] | None = None,
        **kwargs: Any,
    ) -> ForkSessionResponse:
        """Ветвит сессию хранилища: её записи копируются под новый
        идентификатор (его чеканит агент), и новая сессия открывается как
        обычная — с замком, серверами MCP запроса и починкой оборванного
        хода загрузчиком. Исходная сессия не меняется, её замок не нужен;
        сессию, которую ведёт этот процесс, перед копией сбрасывает в
        хранилище. Точка среза — расширение `_meta["boba/upToUuid"]`
        (ForkMeta): ветка от uuid вопроса — беседа до этого вопроса, и
        правка вопроса — новый `prompt` в новой сессии; срез на первой
        записи даёт пустую сессию. Ответ — `sessionId` новой сессии с её
        режимами и настройками, как у `session/new`.

        Ошибки:
        RequestError — сессия неизвестна (-32002); `cwd`, серверы или
            `_meta` не годятся, записи `upToUuid` в сессии нет (-32602);
            хранилище или сборка отказали (-32603).
        """
        directory = self._directory(cwd)
        specs = self._specs.specs(self._blocks(mcp_servers))
        meta = self._fork_meta(kwargs)
        source = self._stored_id(session_id)

        await self._flushed(session_id)
        target = SessionId(value=self._ids.uuid())
        copied = await self._copied(session_id, source, target, meta.up_to_uuid)
        session = await self._opened_fork(target, directory, specs, copied > 0)
        self._sessions[target.value] = session
        logger.info(
            "session %s: forked from %s, %d record(s) copied, cut at %s",
            target.value,
            session_id,
            copied,
            meta.up_to_uuid,
        )
        settings = self._settings(session)

        return ForkSessionResponse(
            session_id=target.value,
            modes=self._options.modes(settings),
            config_options=self._options.config_options(settings),
        )

    @staticmethod
    def _fork_meta(meta: Mapping[str, Any]) -> ForkMeta:
        """Ошибки:
        RequestError — `boba/upToUuid` не непустая строка (-32602).
        """
        try:
            return ForkMeta.model_validate(meta)
        except ValidationError as exc:
            details = f"_meta of session/fork is not valid: {exc}"
            raise RequestError.invalid_params(
                {ErrorDataKey.DETAILS.value: details}
            ) from exc

    async def _flushed(self, session_id: str) -> None:
        """Сессия, которую ведёт этот процесс, сбрасывается в хранилище,
        чтобы копия видела все её записи.

        Ошибки:
        RequestError — хранилище отказало (-32603).
        """
        held = self._sessions.get(session_id)
        if not isinstance(held, AcpSession):
            return

        try:
            await held.flush()
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
                    ErrorDataKey.UP_TO_UUID.value: up_to,
                    ErrorDataKey.DETAILS.value: str(exc),
                }
            ) from exc
        except HistoryError as exc:
            raise RequestError.internal_error(
                {ErrorDataKey.DETAILS.value: str(exc)}
            ) from exc

    async def _opened_fork(
        self,
        target: SessionId,
        directory: Path,
        specs: Mapping[str, McpServerJson],
        resume: bool,
    ) -> AcpSession:
        """Новая сессия над скопированными записями; если открыть её не
        удалось, копия удаляется из хранилища, чтобы ветка не осталась
        сиротой.

        Ошибки:
        RequestError — серверы не годятся (-32602), сессию открыть не
            удалось (-32603).
        """
        waiters = PromptWaiters(self._mapping)
        relay = SessionRelay(target.value, self._linked(), self._mapping, waiters)
        try:
            opened = await self._assembly.open(
                target, directory, specs, relay, resume=resume
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

        relay.attach(opened.agent)

        return AcpSession(target.value, opened, relay, waiters)

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
    ) -> CloseSessionResponse | None:
        """Гасит ход сессии, отпускает замок, закрывает серверы MCP.

        Ошибки:
        RequestError — сессия неизвестна (-32002).
        """
        held = self._session(session_id)
        self._sessions.pop(session_id, None)
        await held.close()
        logger.info("session %s: closed", session_id)

        return CloseSessionResponse()

    async def ext_method(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        raise RequestError.method_not_found(f"_{method}")

    def on_connect(self, conn: AcpClientProtocol) -> None:
        """Соединение SDK здесь не используется: канал к клиенту даёт
        AcpServer через `attach`."""

    async def ext_notification(self, method: str, params: dict[str, Any]) -> None:
        logger.info("extension notification _%s is not supported; ignored", method)


class AcpServer:
    """Соединение ACP по потокам: маршрутизатор SDK с включёнными
    нестабильными методами плюс маршруты `$/cancel_request` и
    `session/delete`, которых у маршрутизатора нет. Создаёт вход процесса;
    `serve` живёт до конца ввода или до `stop`, затем закрывает сессии."""

    def __init__(
        self,
        agent: AcpAgent,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        self._agent = agent
        router = build_agent_router(agent, use_unstable_protocol=True)
        router.add_route(
            Route(
                method=AcpMethod.CANCEL_REQUEST.value,
                func=agent.cancel_request,
                kind="notification",
            )
        )
        router.add_route(
            Route(
                method=AcpMethod.SESSION_DELETE.value,
                func=agent.delete_session,
                kind="request",
            )
        )
        self._connection = Connection(
            router, writer, reader, observers=[agent.requests], listening=False
        )
        agent.attach(ClientLink(self._connection))
        self._stopped = asyncio.Event()

    def stop(self) -> None:
        """Остановка снаружи (сигнал): обслуживание кончается, сессии закрываются."""
        self._stopped.set()

    async def serve(self) -> None:
        receiving = asyncio.create_task(
            self._connection.main_loop(), name="acp-receive"
        )
        stopping = asyncio.create_task(self._stopped.wait(), name="acp-stop")
        try:
            await asyncio.wait(
                {receiving, stopping}, return_when=asyncio.FIRST_COMPLETED
            )
        finally:
            stopping.cancel()
            receiving.cancel()
            await asyncio.gather(receiving, stopping, return_exceptions=True)
            await self._agent.close_all()
            await self._connection.close()
