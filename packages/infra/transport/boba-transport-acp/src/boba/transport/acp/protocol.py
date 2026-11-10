"""Провод ACP v2 (alpha.8) агента и клиента поверх SDK acp.experimental.v2.

Расширение команд сессии (BobaExtension): клиент пишет в историю сессии
команду, которую исполнил сам, и объявление своих команд запросом
`_boba/session/record`, агент отдаёт итог команды уведомлением
`session/update` вида `_boba/command_outcome`, а все команды сессии из
истории целиком (вид, владелец, аргументы, приложение) — видом
`_boba/commands`. Итог команды агент не разбирает: он идёт словарём в форме
клиента, поэтому новый вид итога (новое приложение чата) появляется в
клиенте без правки агента.
Расширение источника вызова: агент спрашивает клиента запросом
`_boba/tool_source`, что сохранить в истории для показа вызова инструмента
сервера клиента, и отдаёт ответ содержимым вызова вида `_boba/tool_source`.

Ошибки:
RequestError (acp.exceptions) — собеседник ответил ошибкой ACP (коды AcpErrorCode)
    или агент выбрал версию, которой клиент не знает.
AcpTransportError — соединение закрыто, ответ на mcp/message пришёл не по конверту
    или строка ввода длиннее лимита потока.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from enum import IntEnum, StrEnum
from types import ModuleType
from typing import Annotated, Any, ClassVar, Literal, TypeAlias, assert_never

import mcp_types
from mcp_types.jsonrpc import (
    INVALID_REQUEST,
    JSONRPC_VERSION,
    PARSE_ERROR,
    ErrorData,
    JSONRPCError,
)
from pydantic import BaseModel as StrictModel
from pydantic import ConfigDict, Field, TypeAdapter, ValidationError

from acp._transport import Transport
from acp.connection import Connection, JsonValue
from acp.exceptions import RequestError
from acp.experimental import v2
from acp.experimental.v2 import (
    AgentSideConnection,
    Client,
    ClientSideConnection,
    schema,
)
from acp.experimental.v2._connection import open_connection
from acp.experimental.v2._initialization import InitializationState
from acp.experimental.v2._params import build_request
from acp.experimental.v2._schema_base import BaseModel
from acp.experimental.v2.client import _ClientRouter
from acp.experimental.v2.meta import AGENT_METHODS, CLIENT_METHODS
from acp.utils import param_model

logger = logging.getLogger(__name__)

__all__ = [
    "AcpErrorCode",
    "AcpTransportError",
    "AcpVersion",
    "AgentSide",
    "BobaExtension",
    "ClientConnection",
    "CommandApp",
    "CommandElement",
    "CommandFailure",
    "CommandKind",
    "CommandOutcome",
    "CommandOutcomeDict",
    "CommandOutcomeUpdate",
    "CommandOwner",
    "CommandText",
    "CommandType",
    "CommandsUpdate",
    "DeclaredAgentCommand",
    "DeclaredApp",
    "DeclaredArgument",
    "DeclaredCommand",
    "DeclaredHarnessCommand",
    "DeclaredPrompt",
    "ElicitationRound",
    "ErrorDataKey",
    "HistoryNotice",
    "HistoryTurnEnd",
    "HistoryUpdate",
    "McpErrorOutcome",
    "McpMessage",
    "McpOutcome",
    "McpResultOutcome",
    "McpVersion",
    "MetaKey",
    "RecordedKind",
    "SessionCommandsRequest",
    "SessionFailureData",
    "SessionFailureKind",
    "SessionRecordParams",
    "SessionRecordRequest",
    "StdioTransport",
    "StopReason",
    "ToolSourceContent",
    "ToolSourceReply",
    "ToolSourceRequest",
    "ToolStatus",
]


class AcpTransportError(Exception):
    """Соединение ACP закрыто или ответ на mcp/message пришёл не по конверту."""


class AcpVersion(IntEnum):
    """Версии ACP провода — реестр «версия → проводной модуль SDK» (wire())."""

    V2 = v2.PROTOCOL_VERSION

    def wire(self) -> ModuleType:
        """Модуль SDK с моделями и сторонами соединения этой версии."""
        match self:
            case AcpVersion.V2:
                return v2

        assert_never(self)


class McpVersion(StrEnum):
    """Версии MCP в конверте mcp/message — реестр «версия → модуль mcp_types»."""

    V2026_07_28 = "2026-07-28"

    def wire(self) -> ModuleType:
        """Модуль mcp_types с моделями этой версии."""
        match self:
            case McpVersion.V2026_07_28:
                return mcp_types

        assert_never(self)


class AcpErrorCode(IntEnum):
    """Коды ошибок ACP (JSON-RPC и MCP-over-ACP), которые различают стороны."""

    INVALID_PARAMS = -32602
    METHOD_NOT_FOUND = -32601
    INTERNAL_ERROR = -32603
    REQUEST_CANCELLED = -32800
    RESOURCE_NOT_FOUND = -32002
    UNSUPPORTED_PROTOCOL_VERSION = -32022
    UNKNOWN_SERVER = -33001
    BACKEND_FAILED = -33002


class StopReason(StrEnum):
    """Причины конца хода (`stopReason` в idle); причину отказа агент шлёт
    пометкой `notice` до idle."""

    END_TURN = "end_turn"
    MAX_TOKENS = "max_tokens"
    MAX_TURN_REQUESTS = "max_turn_requests"
    REFUSAL = "refusal"
    CANCELLED = "cancelled"


class ToolStatus(StrEnum):
    """Состояния вызова инструмента в `tool_call_update` (`status`)."""

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def terminal(self) -> bool:
        """Вызов кончился: удачей, сбоем или отменой."""
        return self in (ToolStatus.COMPLETED, ToolStatus.FAILED, ToolStatus.CANCELLED)


class ErrorDataKey(StrEnum):
    """Ключи `data` ошибки JSON-RPC, которые пишет агент и читает клиент."""

    KIND = "kind"
    DETAILS = "details"
    SESSION_ID = "sessionId"
    UP_TO_MESSAGE_ID = "upToMessageId"


class SessionFailureKind(StrEnum):
    """Публичные причины отказа открытия чата, общие для агента и клиента."""

    UNAVAILABLE = "session-unavailable"
    TOOLS_UNAVAILABLE = "tools-unavailable"


class SessionFailureData(StrictModel):
    """Публичные данные отказа ACP без диагностики и параметров подключения."""

    kind: SessionFailureKind = SessionFailureKind.UNAVAILABLE


class AgentRequestErrors:
    """Граница входа ACP: внутренние ошибки уходят в журнал, наружу — код."""

    def data(self, error: RequestError) -> SessionFailureData:
        try:
            return SessionFailureData.model_validate(error.data)
        except ValidationError:
            return SessionFailureData()

    def internal(self, error: Exception) -> RequestError:
        data = SessionFailureData()
        if isinstance(error, RequestError):
            data = self.data(error)

        return RequestError.internal_error(data.model_dump(mode="json"))


class MetaKey(StrEnum):
    """Ключи boba в `_meta` ACP и MCP.

    Срез ветки `session/fork` по messageId — и в `_meta` запроса
    `session/fork` (id вопроса), и в `capabilities.session.fork._meta` ответа
    initialize (true — агент режет ветку по нему). Форма `elicitation/create`
    своего вопроса агента: у поля вопроса — ключ парного поля «Other» со
    своим текстом, у варианта — предпросмотр; уведомление tool_call этого
    вызова несёт в `_meta` признак вопроса пользователю (true), по нему
    клиент отличает вопрос от вызова инструмента и вживую, и в истории.
    Вопрос сервера MCP: имя сервера и инструмента в `_meta` запроса, раунды
    вопросов вызова — в `_meta` результата tools/call.
    """

    UP_TO_MESSAGE_ID = "boba/upToMessageId"
    QUESTION = "boba/question"
    OTHER = "boba/other"
    PREVIEW = "boba/preview"
    SERVER = "boba/server"
    TOOL = "boba/tool"
    ELICITATIONS = "boba/elicitations"


class ElicitationRound(BaseModel):
    """Раунд вопроса сервера MCP в вызове инструмента: сервер и инструмент,
    форма, какой её увидел пользователь (сообщение сервера и поля схемой
    ACP), и ответ. Агент кладёт раунды вызова списком в `_meta` результата
    tools/call под MetaKey.ELICITATIONS, лента рисует по ним запись вопроса
    одинаково вживую и в истории."""

    server: str
    tool: str
    message: str
    requested: schema.ElicitationSchema
    action: Literal["accept", "decline", "cancel"]
    content: dict[str, str | int | float | bool | list[str]] = Field(
        default_factory=dict
    )


class BobaExtension(StrEnum):
    """Расширения ACP boba: имена методов, виды уведомления `session/update` и
    вид содержимого вызова.

    Команды сессии: агент объявляет их ключами SESSION_RECORD и COMMANDS
    (true) в `capabilities._meta` ответа initialize; клиент объявляет приём
    уведомлений COMMANDS ключом COMMANDS (true) в `clientCapabilities._meta`
    запроса initialize. Без объявления агента клиент не предлагает команд,
    которые исполняет сам, а без объявления клиента агент шлёт только
    стандартный `available_commands_update`; итог команды агент отдаёт
    уведомлением COMMAND_OUTCOME.

    Источник вызова: клиент объявляет его ключом TOOL_SOURCE (true) в
    `clientCapabilities._meta` запроса initialize; агент перед записью
    вызова инструмента сервера, который клиент дал записью `type: "acp"`,
    спрашивает клиента запросом TOOL_SOURCE и отдаёт его ответ в первом
    уведомлении вызова содержимым вида TOOL_SOURCE."""

    SESSION_RECORD = "_boba/session/record"
    COMMAND_OUTCOME = "_boba/command_outcome"
    COMMANDS = "_boba/commands"
    TOOL_SOURCE = "_boba/tool_source"
    HISTORY = "_boba/history"


class HistoryTurnEnd(BaseModel):
    """Исход прошлого хода для ленты; текущее состояние сессии не меняет."""

    kind: Literal["turn_end"] = "turn_end"
    stop_reason: Annotated[str, Field(alias="stopReason")]


class HistoryNotice(BaseModel):
    """Сохранённая пометка для ленты, отдельно от живого уведомления notice."""

    kind: Literal["notice"] = "notice"
    notice: schema.Notice


class HistoryUpdate(BaseModel):
    """Данные истории для клиента, объявившего `_boba/history` в capabilities.

    Агент передаёт исходы и пометки прошлых ходов при replay; клиент рисует
    их без обработки как живых переходов состояния или новых предупреждений.
    """

    session_update: Annotated[
        Literal[BobaExtension.HISTORY], Field(alias="sessionUpdate")
    ] = BobaExtension.HISTORY
    event: Annotated[HistoryTurnEnd | HistoryNotice, Field(discriminator="kind")]

    def wire(self) -> schema.OtherSessionUpdate:
        dumped = self.model_dump(mode="json", by_alias=True, exclude_none=True)

        return schema.OtherSessionUpdate.model_validate(dumped)


class CommandKind(StrEnum):
    """Виды итога команды клиента. Вид называет клиент: агент итог не
    разбирает, а хранит и отдаёт как пришёл, поэтому новый вид итога
    появляется в клиенте без правки агента."""

    TEXT = "text"
    APP = "app"
    ELEMENT = "element"
    FAILED = "failed"


class CommandText(BaseModel):
    """Итог команды клиента текстом markdown, каким его увидел пользователь."""

    kind: Literal[CommandKind.TEXT] = CommandKind.TEXT
    text: str


class CommandApp(BaseModel):
    """Приложение MCP Apps, которое открыла команда клиента, целиком на
    момент вызова: сервер, карточка инструмента, аргументы и итог tools/call,
    ресурс приложения (resources/read со всеми contents и их _meta). По нему
    приложение рисуется снова без обращения к серверу."""

    kind: Literal[CommandKind.APP] = CommandKind.APP
    server: str = Field(min_length=1)
    tool: mcp_types.Tool
    arguments: dict[str, Any]
    result: mcp_types.CallToolResult
    resource: mcp_types.ReadResourceResult


class CommandFailure(BaseModel):
    """Команда клиента не исполнилась: причина для пользователя."""

    kind: Literal[CommandKind.FAILED] = CommandKind.FAILED
    error: str = Field(min_length=1)


class CommandElement(BaseModel):
    """Приложение чата, которое открыла команда клиента: имя его элемента
    ленты и props на момент вызова. Элемент живёт во вкладке и работает
    действиями чата; серверу MCP лента ничего не шлёт."""

    kind: Literal[CommandKind.ELEMENT] = CommandKind.ELEMENT
    element: str = Field(min_length=1)
    props: dict[str, Any] = Field(default_factory=dict)


CommandOutcome: TypeAlias = Annotated[
    CommandText | CommandApp | CommandElement | CommandFailure,
    Field(discriminator="kind"),
]
"""Итог команды клиента по виду kind. Форма итога принадлежит клиенту: на
проводе он идёт словарём (CommandOutcomeDict), а разбирает его только клиент,
который итог и построил."""


class RecordedKind(StrEnum):
    """Что клиент пишет в историю запросом `_boba/session/record`."""

    COMMAND = "command"
    COMMANDS = "commands"


CommandOutcomeDict: TypeAlias = dict[str, Any]
"""Итог команды клиента так, как он идёт по проводу и лежит в истории
сессии: словарь в форме клиента. Агент его не разбирает — ни на записи, ни
при отдаче уведомлением, — поэтому новый вид итога не требует правки агента."""


class SessionRecordRequest(BaseModel):
    """Параметры `_boba/session/record` с командой, которую клиент исполнил
    сам, — messageId записи (его даёт клиент), имя команды, набранный текст
    `/<имя> <аргументы>` и итог. Агент отвечает, когда запись легла в
    историю сессии и её уведомления ушли клиенту."""

    session_id: Annotated[str, Field(alias="sessionId", min_length=1)]
    kind: Literal[RecordedKind.COMMAND] = RecordedKind.COMMAND
    message_id: Annotated[str, Field(alias="messageId", min_length=1)]
    command: Annotated[str, Field(min_length=1)]
    text: str
    outcome: CommandOutcomeDict


class CommandType(StrEnum):
    """Вид команды сессии: PROMPT — промпт сервера MCP, его раскрывает агент
    для модели; APP — интерфейс MCP Apps, клиент сам зовёт tools/call и
    рисует приложение; AGENT — команда сессии агента; HARNESS — команда
    клиента, её исполняет клиент."""

    PROMPT = "prompt"
    APP = "app"
    AGENT = "agent"
    HARNESS = "harness"


class CommandOwner(StrEnum):
    """Владелец команды, который не сервер MCP."""

    AGENT = "agent"
    HARNESS = "harness"


class DeclaredArgument(BaseModel):
    """Аргумент команды: имя, описание, обязательность и значения, которые
    подсказал владелец."""

    name: Annotated[str, Field(min_length=1)]
    description: str = ""
    required: bool
    values: list[str] = Field(default_factory=list)


class DeclaredPrompt(BaseModel):
    """Промпт prompt сервера owner; name — полное имя команды."""

    type: Literal[CommandType.PROMPT] = CommandType.PROMPT
    name: Annotated[str, Field(min_length=1)]
    description: str
    owner: Annotated[str, Field(min_length=1)]
    prompt: Annotated[str, Field(min_length=1)]
    arguments: list[DeclaredArgument] = Field(default_factory=list)


class DeclaredApp(BaseModel):
    """Интерфейс MCP Apps сервера owner: промпт prompt ведёт к инструменту
    tool (карточка целиком) с приложением resource_uri; аргументы команды —
    аргументы вызова."""

    type: Literal[CommandType.APP] = CommandType.APP
    name: Annotated[str, Field(min_length=1)]
    description: str
    owner: Annotated[str, Field(min_length=1)]
    prompt: Annotated[str, Field(min_length=1)]
    tool: mcp_types.Tool
    resource_uri: Annotated[str, Field(alias="resourceUri", min_length=1)]
    arguments: list[DeclaredArgument] = Field(default_factory=list)


class DeclaredAgentCommand(BaseModel):
    """Команда сессии агента."""

    type: Literal[CommandType.AGENT] = CommandType.AGENT
    name: Annotated[str, Field(min_length=1)]
    description: str
    owner: Literal[CommandOwner.AGENT] = CommandOwner.AGENT
    arguments: list[DeclaredArgument] = Field(default_factory=list)


class DeclaredHarnessCommand(BaseModel):
    """Команда клиента: её объявляет и исполняет клиент."""

    type: Literal[CommandType.HARNESS] = CommandType.HARNESS
    name: Annotated[str, Field(min_length=1)]
    description: str
    owner: Literal[CommandOwner.HARNESS] = CommandOwner.HARNESS
    arguments: list[DeclaredArgument] = Field(default_factory=list)


DeclaredCommand: TypeAlias = Annotated[
    DeclaredPrompt | DeclaredApp | DeclaredAgentCommand | DeclaredHarnessCommand,
    Field(discriminator="type"),
]
"""Команда сессии целиком по виду type."""


class SessionCommandsRequest(BaseModel):
    """Параметры `_boba/session/record` с объявлением команд клиента: все
    его команды разом. Агент пишет в историю разницу с объявленным раньше
    и отвечает, когда она легла."""

    session_id: Annotated[str, Field(alias="sessionId", min_length=1)]
    kind: Literal[RecordedKind.COMMANDS] = RecordedKind.COMMANDS
    commands: list[DeclaredHarnessCommand]


SessionRecordParams: TypeAlias = Annotated[
    SessionRecordRequest | SessionCommandsRequest, Field(discriminator="kind")
]
"""Параметры `_boba/session/record` по виду kind."""


class CommandsUpdate(BaseModel):
    """Уведомление `session/update` вида `_boba/commands`: все команды сессии
    из истории по порядку объявления. Агент шлёт его после ответа на
    открытие сессии и после каждой смены команд, клиенту, который объявил
    приём, — следом идёт стандартный `available_commands_update`."""

    session_update: Annotated[
        Literal[BobaExtension.COMMANDS], Field(alias="sessionUpdate")
    ] = BobaExtension.COMMANDS
    commands: list[DeclaredCommand]

    def wire(self) -> schema.OtherSessionUpdate:
        """Уведомление видом расширения в общем конверте схемы SDK."""
        dumped = self.model_dump(mode="json", by_alias=True, exclude_none=True)

        return schema.OtherSessionUpdate.model_validate(dumped)


class CommandOutcomeUpdate(BaseModel):
    """Уведомление `session/update` вида `_boba/command_outcome`: итог
    команды записи messageId. Агент шлёт его вслед за `user_message` той же
    записи — вживую и при воспроизведении истории одинаково."""

    session_update: Annotated[
        Literal[BobaExtension.COMMAND_OUTCOME], Field(alias="sessionUpdate")
    ] = BobaExtension.COMMAND_OUTCOME
    message_id: Annotated[str, Field(alias="messageId", min_length=1)]
    command: Annotated[str, Field(min_length=1)]
    outcome: CommandOutcomeDict

    def wire(self) -> schema.OtherSessionUpdate:
        """Уведомление видом расширения в общем конверте схемы SDK."""
        dumped = self.model_dump(mode="json", by_alias=True, exclude_none=True)

        return schema.OtherSessionUpdate.model_validate(dumped)


class ToolSourceRequest(BaseModel):
    """Параметры запроса `_boba/tool_source` агента клиенту: вызов
    toolCallId сессии sessionId инструмента name сервера serverId (записи
    `type: "acp"` клиента) с аргументами arguments, который агент сейчас
    запишет и исполнит. Клиент отвечает ToolSourceReply."""

    session_id: Annotated[str, Field(alias="sessionId", min_length=1)]
    tool_call_id: Annotated[str, Field(alias="toolCallId", min_length=1)]
    server_id: Annotated[str, Field(alias="serverId", min_length=1)]
    name: Annotated[str, Field(min_length=1)]
    arguments: dict[str, Any]


class ToolSourceReply(BaseModel):
    """Ответ клиента на `_boba/tool_source`: что сохранить для показа
    вызова, в форме клиента. Агент его не разбирает: кладёт в историю
    записью вызова и отдаёт обратно содержимым ToolSourceContent."""

    source: dict[str, Any]


class ToolSourceContent(BaseModel):
    """Содержимое вызова вида `_boba/tool_source`: источник вызова, каким
    его дал клиент. Агент шлёт его в первом уведомлении `tool_call` вызова
    — вживую и при воспроизведении истории одинаково."""

    type: Literal[BobaExtension.TOOL_SOURCE] = BobaExtension.TOOL_SOURCE
    source: dict[str, Any]

    def wire(self) -> schema.OtherToolCallContent:
        """Содержимое видом расширения в общем конверте схемы SDK."""
        dumped = self.model_dump(mode="json", by_alias=True, exclude_none=True)

        return schema.OtherToolCallContent.model_validate(dumped)


class McpMessage(BaseModel):
    """Параметры mcp/message: запрос агента и уведомление клиента (форма одна)."""

    server_id: Annotated[str, Field(alias="serverId", min_length=1)]
    request_id: Annotated[str, Field(alias="requestId", min_length=1)]
    method: str
    params: dict[str, Any] | None = None
    field_meta: Annotated[dict[str, Any] | None, Field(alias="_meta")] = None


class McpResultOutcome(BaseModel):
    """Исход mcp/message с результатом MCP (result может быть null)."""

    result: Any
    field_meta: Annotated[dict[str, Any] | None, Field(alias="_meta")] = None


class McpErrorOutcome(BaseModel):
    """Исход mcp/message с внутренней ошибкой MCP (не ошибкой ACP)."""

    error: ErrorData
    field_meta: Annotated[dict[str, Any] | None, Field(alias="_meta")] = None


McpOutcome: TypeAlias = McpResultOutcome | McpErrorOutcome


class StdioTransport(Transport):
    """Один объект JSON-RPC на строку stdio. В отличие от NdjsonTransport rc3,
    отвечает на повреждённый JSON и не-объекты, не исполняет массивы сообщений.
    """

    ENCODING: ClassVar[str] = "utf-8"

    def __init__(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        self._reader = reader
        self._writer = writer

    async def receive(self) -> dict[str, Any] | None:
        while True:
            try:
                line = await self._reader.readline()
            except ValueError as exc:
                raise AcpTransportError(
                    f"Could not read a JSON-RPC line from the client because it is "
                    f"longer than the input stream line limit: {exc}."
                ) from exc

            if not line:
                return None
            if not line.strip():
                continue

            try:
                value = json.loads(line.decode(self.ENCODING))
            except ValueError as exc:
                shown = line[:200].decode(self.ENCODING, errors="replace")
                details = (
                    f"The input line {shown!r} is not {self.ENCODING} JSON: {exc}."
                )
                error = ErrorData(code=PARSE_ERROR, message=details)
            else:
                if isinstance(value, dict):
                    return value
                details = f"Expected one JSON-RPC object, got {repr(value)[:200]}."
                error = ErrorData(code=INVALID_REQUEST, message=details)

            refusal = JSONRPCError(jsonrpc=JSONRPC_VERSION, id=None, error=error)
            await self.send(refusal.model_dump(mode="json"))

    async def send(self, message: dict[str, Any]) -> None:
        text = json.dumps(message) + "\n"
        self._writer.write(text.encode(self.ENCODING))
        await self._writer.drain()

    async def close(self) -> None:
        """Потоки принадлежат процессу; Connection отменяет обработчики сам."""


class InitializationParams(StrictModel):
    """Версия запроса initialize до преобразований моделей SDK."""

    model_config = ConfigDict(extra="allow")
    protocol_version: Annotated[
        int, Field(alias="protocolVersion", strict=True, ge=0, le=65535)
    ]


class ReplayCursor(StrictModel):
    """Обязательный строковый вид курсора до нормализации replayFrom в SDK."""

    type: Annotated[str, Field(strict=True)]


class ResumeParams(StrictModel):
    """Проверка формы replayFrom, не допускающая подстановки другого курсора."""

    replay_from: Annotated[ReplayCursor | None, Field(alias="replayFrom")] = None


class NegotiationState(InitializationState):
    """Совместимость rc3 с повтором initialize по V2 revision 7ec5185.

    SDK хранит готовность и проверяет версию ответа. Здесь меняется только
    жизненный цикл: повторы до первого другого запроса и откат ошибки.
    """

    def __init__(self) -> None:
        super().__init__()
        self._used = False
        self._previous = False

    def begin(self, request: schema.InitializeRequest) -> None:
        if self._used or self.phase == "initializing":
            raise RequestError.invalid_request(
                {"details": "initialize must finish before the first other request"}
            )
        self._previous = self.phase == "initialized"
        previous_phase = self._phase
        self._phase = "uninitialized"
        try:
            super().begin(request)
        except BaseException:
            self._phase = previous_phase
            raise
        self._failure = None
        self._ready.clear()

    def fail(self, error: BaseException) -> None:
        self._phase = "initialized" if self._previous else "uninitialized"
        self._failure = None if self._previous else error
        self._ready.set()

    def accepts_notifications(self) -> bool:
        """Уведомления допустимы после первого успешного initialize."""
        return self.phase == "initialized" or (
            self.phase == "initializing" and self._previous
        )

    def request(self, method: str) -> None:
        """Первый другой запрос клиента завершает согласование навсегда."""
        if self.phase != "initialized":
            raise RequestError.invalid_request(
                {"details": f"ACP must be initialized before {method!r}"}
            )
        self._used = True


class AgentSide:
    """Сторона агента ACP v2 на AgentSideConnection.attach SDK.

    Проверяет версию и курсор до нормализации SDK, уведомления mcp/message
    отдаёт получателю активного запроса, остальное — роутеру SDK. listening
    включает чтение соединением (HTTP); иначе чтение ведёт serve (stdio, WS).
    """

    def __init__(
        self,
        agent_factory: Callable[[AgentSide, AgentSideConnection], object],
        transport: Transport,
        listening: bool = False,
    ) -> None:
        self._receivers: dict[str, Callable[[McpMessage], Awaitable[None]]] = {}
        self._outcomes: TypeAdapter[McpOutcome] = TypeAdapter(McpOutcome)
        self._state = NegotiationState()
        self._errors = AgentRequestErrors()
        self._connection = Connection(self, transport, listening=listening)

        def attach(connection: AgentSideConnection) -> object:
            # attach rc3 создаёт роутер после фабрики: оба используют одно состояние.
            connection._state = self._state
            return agent_factory(self, connection)

        self._sdk, self._router = AgentSideConnection.attach(attach, self._connection)

    async def __call__(
        self, method: str, params: JsonValue | None, is_notification: bool
    ) -> JsonValue | None:
        try:
            return await self._dispatch(method, params, is_notification)
        except RequestError as exc:
            if exc.code != AcpErrorCode.INTERNAL_ERROR:
                raise

            logger.exception("ACP %s failed: %s; data=%r", method, exc, exc.data)
            raise self._errors.internal(exc) from exc
        except ValidationError as exc:
            raise RequestError.invalid_params() from exc
        except Exception as exc:
            logger.exception("ACP %s failed: %s", method, exc)
            raise self._errors.internal(exc) from exc

    async def _dispatch(
        self, method: str, params: JsonValue | None, is_notification: bool
    ) -> JsonValue | None:
        # роутер rc3 проверяет отсутствующие params моделью, схема v2 их не требует
        if params is None:
            params = {}

        if is_notification and not self._state.accepts_notifications():
            return None
        if not is_notification and method != AGENT_METHODS["initialize"]:
            self._state.request(method)

        mcp_notification = is_notification and method == AGENT_METHODS["mcp_message"]
        if mcp_notification:
            message = McpMessage.model_validate(params)
            receiver = self._receivers.get(message.request_id)
            if receiver is None:
                logger.info(
                    "mcp/message %s for request %s that is not running; dropped",
                    message.method,
                    message.request_id,
                )
                return None

            await receiver(message)
            return None

        if not is_notification:
            if method == AGENT_METHODS["initialize"]:
                params = self._negotiate(params)

            if method == AGENT_METHODS["session_resume"]:
                try:
                    ResumeParams.model_validate(params)
                except ValidationError as exc:
                    raise RequestError.invalid_params() from exc

        return await self._router(method, params, is_notification)

    @staticmethod
    def _negotiate(params: JsonValue) -> dict[str, Any]:
        try:
            request = InitializationParams.model_validate(params)
        except ValidationError as exc:
            raise RequestError.invalid_params() from exc
        if request.protocol_version < AcpVersion.V2:
            raise RequestError(
                AcpErrorCode.UNSUPPORTED_PROTOCOL_VERSION,
                "Unsupported protocol version",
                {"requested": request.protocol_version, "supported": [AcpVersion.V2]},
            )

        selected = request.model_dump(by_alias=True)
        selected["protocolVersion"] = AcpVersion.V2.value

        return selected

    async def request(
        self,
        message: McpMessage,
        receiver: Callable[[McpMessage], Awaitable[None]] | None,
    ) -> McpOutcome:
        """Запрос mcp/message клиенту; уведомления по его requestId, пока он
        идёт, получает receiver. Отмена задачи шлёт $/cancel_request."""
        params = message.model_dump(mode="json", by_alias=True, exclude_unset=True)
        if receiver is not None:
            self._receivers[message.request_id] = receiver

        try:
            raw = await self._connection.send_request(
                CLIENT_METHODS["mcp_message"], params
            )
        except ConnectionError as exc:
            raise AcpTransportError(
                f"Could not send mcp/message '{message.method}' to server "
                f"'{message.server_id}' (request '{message.request_id}') because "
                f"the ACP connection to the client is gone: {exc}."
            ) from exc
        finally:
            self._receivers.pop(message.request_id, None)

        try:
            return self._outcomes.validate_python(raw)
        except ValidationError as exc:
            shown = repr(raw)[:200]
            raise AcpTransportError(
                f"The client answered mcp/message '{message.method}' to server "
                f"'{message.server_id}' (request '{message.request_id}') with "
                f"{shown}, expected an object with 'result' or 'error': {exc}"
            ) from exc

    async def tool_source(self, request: ToolSourceRequest) -> ToolSourceReply:
        """Запрос `_boba/tool_source` клиенту: источник вызова для истории.

        Ошибки:
        RequestError — клиент ответил ошибкой ACP.
        AcpTransportError — соединение закрыто или ответ не по форме.
        """
        method = BobaExtension.TOOL_SOURCE.value
        params = request.model_dump(mode="json", by_alias=True, exclude_none=True)
        try:
            raw = await self._sdk.send_extension_request(method, params)
        except ConnectionError as exc:
            raise AcpTransportError(
                f"Could not send {method} for call '{request.tool_call_id}' of "
                f"tool '{request.name}' to the client because the ACP connection "
                f"is gone: {exc}."
            ) from exc

        try:
            return ToolSourceReply.model_validate(raw)
        except ValidationError as exc:
            shown = repr(raw)[:200]
            raise AcpTransportError(
                f"The client answered {method} for call '{request.tool_call_id}' "
                f"of tool '{request.name}' with {shown}, expected an object with "
                f"'source': {exc}"
            ) from exc

    @property
    def connection(self) -> Connection:
        """Соединение SDK этой стороны: его отдаёт роутер сервера сети."""
        return self._connection

    async def serve(self) -> None:
        """Читает соединение до конца потока и закрывает его."""
        async with self._connection:
            await self._connection.main_loop()


class ToolSourceClient:
    """Обработчик `_boba/tool_source` для публичного API расширений SDK.

    Остальные методы, расширения и уведомления остаются у исходного клиента.
    Делегирование сохраняет идентичность заглушек Client: SDK отличает их от
    реализованных обработчиков и отвечает method not found.
    """

    def __init__(
        self,
        client: Client,
        source: Callable[[ToolSourceRequest], Awaitable[ToolSourceReply]],
    ) -> None:
        self._client = client
        self._source = source

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)

    async def handle_extension_request(self, method: str, params: Any) -> Any:
        """Источник вызова — source, другие расширения — исходному клиенту.

        Ошибки:
        RequestError — неизвестный метод (-32601), параметры источника не
            по форме (-32602), либо ошибка обработчика клиента.
        """
        if method != BobaExtension.TOOL_SOURCE.value:
            handler = getattr(self._client, "handle_extension_request", None)
            if handler is None:
                raise RequestError.method_not_found(method)
            return await handler(method, params)

        try:
            request = ToolSourceRequest.model_validate(params)
        except ValidationError as exc:
            details = (
                f"{BobaExtension.TOOL_SOURCE.value}: expected sessionId, "
                f"toolCallId, serverId, name and arguments: {exc}"
            )
            raise RequestError.invalid_params(
                {ErrorDataKey.DETAILS.value: details}
            ) from exc

        reply = await self._source(request)

        return reply.model_dump(mode="json", by_alias=True, exclude_none=True)


class ClientConnection(ClientSideConnection):
    """Адаптер rc3 для повторов initialize и нового конверта mcp/message.

    MCP использует serverId/requestId вместо connectionId. Расширения проходят
    через handle_extension_request SDK; notify шлёт уведомление mcp/message.
    Закрытые имена SDK нужны для состояния согласования и MCP-адаптера.
    """

    def __init__(
        self,
        client: Client,
        answer: Callable[[McpMessage], Awaitable[McpOutcome]],
        source: Callable[[ToolSourceRequest], Awaitable[ToolSourceReply]],
        input_stream: asyncio.StreamWriter | Transport,
        output_stream: asyncio.StreamReader | None,
        **connection_kwargs: Any,
    ) -> None:
        self._answer = answer
        self._state = NegotiationState()
        self._router = _ClientRouter(ToolSourceClient(client, source), self._state)
        self._conn = open_connection(
            self._receive, input_stream, output_stream, **connection_kwargs
        )

    @param_model(schema.InitializeRequest)
    async def initialize(
        self,
        protocol_version: int,
        info: schema.Implementation,
        capabilities: schema.ClientCapabilities | None = None,
        **kwargs: Any,
    ) -> schema.InitializeResponse:
        """Повтор не закрывает соединение при отказе агента, в отличие от rc3."""
        request = build_request(
            schema.InitializeRequest,
            {
                "protocol_version": protocol_version,
                "info": info,
                "capabilities": capabilities,
            },
            kwargs,
        )
        self._state.begin(request)
        try:
            response = await self._conn.send_request(
                AGENT_METHODS["initialize"],
                request.model_dump(mode="json", by_alias=True, exclude_unset=True),
            )
        except BaseException as error:
            self._state.fail(error)
            raise
        try:
            parsed = schema.InitializeResponse.model_validate(response)
            self._state.complete(parsed)
        except BaseException as error:
            self._state.fail(error)
            await self._conn.close()
            raise
        return parsed

    async def _request(self, method: str, request: BaseModel) -> Any:
        self._state.request(method)
        return await super()._request(method, request)

    async def send_extension_request(self, method: str, params: Any = None) -> Any:
        self._state.request(method)
        return await super().send_extension_request(method, params)

    async def _receive(
        self, method: str, params: JsonValue | None, is_notification: bool
    ) -> JsonValue | None:
        await self._state.require(method)

        mcp_request = not is_notification and method == CLIENT_METHODS["mcp_message"]
        if mcp_request:
            outcome = await self._answer(McpMessage.model_validate(params))
            return outcome.model_dump(mode="json", by_alias=True, exclude_unset=True)

        return await self._router(method, params, is_notification)

    def connected(self) -> bool:
        """Соединение живо: SDK не видел конца потока агента и его не закрывали."""
        connection = self._conn
        if connection._disconnected:
            return False

        return not connection._closed

    async def notify(self, message: McpMessage) -> None:
        """Уведомление mcp/message агенту по его активному запросу."""
        try:
            await self._notify(AGENT_METHODS["mcp_message"], message)
        except ConnectionError as exc:
            raise AcpTransportError(
                f"Could not send mcp/message notification '{message.method}' for "
                f"server '{message.server_id}' (request '{message.request_id}') "
                f"because the ACP connection to the agent is gone: {exc}."
            ) from exc
