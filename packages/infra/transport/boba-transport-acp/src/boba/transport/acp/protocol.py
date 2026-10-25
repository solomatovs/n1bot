"""Провод ACP v2 (alpha.8) агента и клиента поверх SDK acp.experimental.v2.

Расширение записи команд клиента (BobaExtension): клиент пишет в историю
сессии команду, которую исполнил сам, запросом `_boba/session/record`, агент
отдаёт её итог уведомлением `session/update` вида `_boba/command_outcome`.
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
import functools
import json
import logging
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
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
from pydantic import Field, TypeAdapter, ValidationError

from acp import schema as v1_schema
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
from acp.experimental.v2._schema_base import BaseModel
from acp.experimental.v2.client import _ClientRouter
from acp.experimental.v2.meta import AGENT_METHODS, CLIENT_METHODS

logger = logging.getLogger(__name__)

__all__ = [
    "AcpErrorCode",
    "AcpTransportError",
    "AcpVersion",
    "AgentSide",
    "BatchTransport",
    "BobaExtension",
    "ClientConnection",
    "CommandApp",
    "CommandFailure",
    "CommandKind",
    "CommandOutcome",
    "CommandOutcomeUpdate",
    "CommandText",
    "ElicitationRound",
    "ErrorDataKey",
    "McpErrorOutcome",
    "McpMessage",
    "McpOutcome",
    "McpResultOutcome",
    "McpVersion",
    "MetaKey",
    "SessionRecordRequest",
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
    """Расширения ACP boba: имена методов, вид уведомления `session/update` и
    вид содержимого вызова.

    Запись команд клиента: агент объявляет её ключом SESSION_RECORD (true) в
    `capabilities._meta` ответа initialize; клиент без объявления команд,
    которые исполняет сам, не предлагает; итог команды агент отдаёт
    уведомлением COMMAND_OUTCOME.

    Источник вызова: клиент объявляет его ключом TOOL_SOURCE (true) в
    `clientCapabilities._meta` запроса initialize; агент перед записью
    вызова инструмента сервера, который клиент дал записью `type: "acp"`,
    спрашивает клиента запросом TOOL_SOURCE и отдаёт его ответ в первом
    уведомлении вызова содержимым вида TOOL_SOURCE."""

    SESSION_RECORD = "_boba/session/record"
    COMMAND_OUTCOME = "_boba/command_outcome"
    TOOL_SOURCE = "_boba/tool_source"


class CommandKind(StrEnum):
    """Виды итога команды клиента."""

    TEXT = "text"
    APP = "app"
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


CommandOutcome: TypeAlias = Annotated[
    CommandText | CommandApp | CommandFailure, Field(discriminator="kind")
]
"""Итог команды клиента по виду kind."""


class SessionRecordRequest(BaseModel):
    """Параметры `_boba/session/record`: команда, которую клиент исполнил
    сам, — messageId записи (его даёт клиент), имя команды, набранный текст
    `/<имя> <аргументы>` и итог. Агент отвечает, когда запись легла в
    историю сессии и её уведомления ушли клиенту."""

    session_id: Annotated[str, Field(alias="sessionId", min_length=1)]
    message_id: Annotated[str, Field(alias="messageId", min_length=1)]
    command: Annotated[str, Field(min_length=1)]
    text: str
    outcome: CommandOutcome


class CommandOutcomeUpdate(BaseModel):
    """Уведомление `session/update` вида `_boba/command_outcome`: итог
    команды записи messageId. Агент шлёт его вслед за `user_message` той же
    записи — вживую и при воспроизведении истории одинаково."""

    session_update: Annotated[
        Literal[BobaExtension.COMMAND_OUTCOME], Field(alias="sessionUpdate")
    ] = BobaExtension.COMMAND_OUTCOME
    message_id: Annotated[str, Field(alias="messageId", min_length=1)]
    command: Annotated[str, Field(min_length=1)]
    outcome: CommandOutcome

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


@dataclass(slots=True)
class _Batch:
    """Пакет JSON-RPC, ждущий ответов: id неотвеченных запросов и собранные ответы."""

    awaited: list[JsonValue]
    replies: list[JsonValue]


class BatchTransport(Transport):
    """Реализация Transport SDK стороны агента над строками stdio с пакетами
    JSON-RPC 2.0: записи пакета Connection получает по одной, ответы на его запросы
    уходят одним массивом. Временная замена NdjsonTransport, пока SDK не разбирает
    пакеты."""

    ENCODING: ClassVar[str] = "utf-8"

    def __init__(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        self._reader = reader
        self._writer = writer
        self._inbox: deque[dict[str, Any]] = deque()
        self._batches: list[_Batch] = []

    async def receive(self) -> dict[str, Any] | None:
        while not self._inbox:
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
                value = json.loads(line)
            except ValueError as exc:
                shown = line[:200].decode(self.ENCODING, errors="replace")
                details = (
                    f"The input line {shown!r} is not {self.ENCODING} JSON: {exc}."
                )
                error = ErrorData(code=PARSE_ERROR, message=details)
                refusal = JSONRPCError(jsonrpc=JSONRPC_VERSION, id=None, error=error)
                await self._write(refusal.model_dump(mode="json"))
                continue

            await self._accept(value)

        return self._inbox.popleft()

    async def send(self, message: dict[str, Any]) -> None:
        if "method" in message:
            await self._write(message)
            return

        for batch in self._batches:
            if message["id"] not in batch.awaited:
                continue

            batch.awaited.remove(message["id"])
            batch.replies.append(message)
            if batch.awaited:
                return

            self._batches.remove(batch)
            await self._write(batch.replies)
            return

        await self._write(message)

    async def close(self) -> None:
        """Очереди записи нет, writer stdio не закрывается; ждущие пакеты не
        дописываются — Connection.close уже отменил их обработчики."""

    async def _accept(self, value: JsonValue) -> None:
        """Раскладывает разобранную строку: объект — в очередь, записи пакета — в
        очередь с ожиданием ответов на его запросы; на не-объект пишет ошибку."""
        match value:
            case dict():
                self._inbox.append(value)
                return
            case [_, *_]:
                entries = value
            case _:
                details = (
                    "Expected a JSON-RPC object or a non-empty batch, "
                    f"got {repr(value)[:200]}."
                )
                error = ErrorData(code=INVALID_REQUEST, message=details)
                refusal = JSONRPCError(jsonrpc=JSONRPC_VERSION, id=None, error=error)
                await self._write(refusal.model_dump(mode="json"))
                return

        batch = _Batch(awaited=[], replies=[])
        for index, entry in enumerate(entries):
            if not isinstance(entry, dict):
                details = (
                    f"Batch entry {index} must be a JSON-RPC object, "
                    f"got {repr(entry)[:200]}."
                )
                error = ErrorData(code=INVALID_REQUEST, message=details)
                refusal = JSONRPCError(jsonrpc=JSONRPC_VERSION, id=None, error=error)
                batch.replies.append(refusal.model_dump(mode="json"))
                continue

            self._inbox.append(entry)
            if "method" not in entry:
                continue

            if "id" not in entry:
                continue

            batch.awaited.append(entry["id"])

        if batch.awaited:
            self._batches.append(batch)
            return

        if batch.replies:
            await self._write(batch.replies)

    async def _write(self, value: JsonValue) -> None:
        """Одна строка JSON в вывод с ожиданием противодавления."""
        text = json.dumps(value) + "\n"
        self._writer.write(text.encode(self.ENCODING))
        await self._writer.drain()


class AgentSide:
    """Сторона агента ACP v2 на AgentSideConnection.attach SDK. На чужую версию и
    рукопожатие v1 отвечает своей, уведомление mcp/message отдаёт получателю его
    активного запроса, остальное — роутеру SDK; шлёт запрос mcp/message и idle
    с ошибкой. listening — соединение само читает транспорт (так его ждёт
    сервер HTTP SDK); иначе чтение ведёт serve (stdio, WebSocket)."""

    def __init__(
        self,
        agent_factory: Callable[[AgentSide, AgentSideConnection], object],
        info: schema.Implementation,
        transport: Transport,
        listening: bool = False,
    ) -> None:
        self._info = info
        self._receivers: dict[str, Callable[[McpMessage], Awaitable[None]]] = {}
        self._outcomes: TypeAdapter[McpOutcome] = TypeAdapter(McpOutcome)
        self._connection = Connection(self, transport, listening=listening)
        _, self._router = AgentSideConnection.attach(
            functools.partial(agent_factory, self), self._connection
        )

    async def __call__(
        self, method: str, params: JsonValue | None, is_notification: bool
    ) -> JsonValue | None:
        # роутер rc3 проверяет отсутствующие params моделью, схема v2 их не требует
        if params is None:
            params = {}

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

        initialize = not is_notification and method == AGENT_METHODS["initialize"]
        if not initialize:
            return await self._router(method, params, is_notification)

        request = v1_schema.InitializeRequest.model_validate(params)
        known = list(AcpVersion)
        if request.protocol_version in known:
            return await self._router(method, params, is_notification)

        return schema.InitializeResponse(
            protocol_version=max(AcpVersion), info=self._info
        )

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
            raw = await self._connection.send_request(method, params)
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


class ClientConnection(ClientSideConnection):
    """Наследник ClientSideConnection SDK на его закрытых именах. Запрос
    mcp/message отдаёт answer, запрос `_boba/tool_source` — source,
    остальное — роутеру SDK; notify шлёт уведомление mcp/message."""

    def __init__(
        self,
        client: Client,
        answer: Callable[[McpMessage], Awaitable[McpOutcome]],
        source: Callable[[ToolSourceRequest], Awaitable[ToolSourceReply]],
        input_stream: asyncio.StreamWriter | Transport,
        output_stream: asyncio.StreamReader | None,
        **connection_kwargs: Any,
    ) -> None:
        self._client = client
        self._answer = answer
        self._source = source
        self._state = InitializationState()
        self._router = _ClientRouter(client, self._state)
        self._conn = open_connection(
            self._receive, input_stream, output_stream, **connection_kwargs
        )

    async def _receive(
        self, method: str, params: JsonValue | None, is_notification: bool
    ) -> JsonValue | None:
        await self._state.require(method)

        mcp_request = not is_notification and method == CLIENT_METHODS["mcp_message"]
        if mcp_request:
            outcome = await self._answer(McpMessage.model_validate(params))
            return outcome.model_dump(mode="json", by_alias=True, exclude_unset=True)

        source_request = not is_notification and method == BobaExtension.TOOL_SOURCE
        if source_request:
            return await self._sourced(params)

        return await self._router(method, params, is_notification)

    async def _sourced(self, params: JsonValue | None) -> JsonValue:
        """Ответ на `_boba/tool_source` обработчиком source.

        Ошибки:
        RequestError — параметры не по форме запроса (-32602).
        """
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
