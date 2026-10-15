"""Провод ACP v2 (alpha.8) агента и клиента поверх SDK acp.experimental.v2.

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
from typing import Annotated, Any, ClassVar, TypeAlias, assert_never

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
    "ClientConnection",
    "ErrorDataKey",
    "IdleErrorNotification",
    "IdleErrorUpdate",
    "McpErrorOutcome",
    "McpMessage",
    "McpOutcome",
    "McpResultOutcome",
    "McpVersion",
    "MetaKey",
    "StopReason",
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
    RESOURCE_NOT_FOUND = -32002
    REQUEST_CANCELLED = -32800
    BINDING_LIMIT = -33000
    UNKNOWN_SERVER = -33001
    BACKEND_FAILED = -33002


class StopReason(StrEnum):
    """Причины конца хода (`stopReason` в idle); у ERROR причина в IdleErrorUpdate."""

    END_TURN = "end_turn"
    MAX_TOKENS = "max_tokens"
    MAX_TURN_REQUESTS = "max_turn_requests"
    REFUSAL = "refusal"
    CANCELLED = "cancelled"
    ERROR = "error"


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
    MESSAGE = "message"
    DETAILS = "details"
    SESSION_ID = "sessionId"
    UP_TO_MESSAGE_ID = "upToMessageId"


class MetaKey(StrEnum):
    """Ключи boba в `_meta` запросов ACP: срез ветки `session/fork` по messageId."""

    UP_TO_MESSAGE_ID = "boba/upToMessageId"


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


class IdleErrorUpdate(schema.IdleSessionStateUpdate):
    """idle со stopReason "error" и полем error (alpha.8)."""

    error: schema.Error | None = None


class IdleErrorNotification(schema.UpdateSessionNotification):
    """session/update с IdleErrorUpdate: шлёт AgentSide.send_update, разбирает
    ClientConnection."""

    update: IdleErrorUpdate


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
    с ошибкой."""

    def __init__(
        self,
        agent_factory: Callable[[AgentSide, AgentSideConnection], object],
        info: schema.Implementation,
        transport: Transport,
    ) -> None:
        self._info = info
        self._receivers: dict[str, Callable[[McpMessage], Awaitable[None]]] = {}
        self._outcomes: TypeAdapter[McpOutcome] = TypeAdapter(McpOutcome)
        self._connection = Connection(self, transport, listening=False)
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

    async def send_update(self, notification: IdleErrorNotification) -> None:
        """idle со stopReason "error"; прочие обновления агент шлёт через SDK."""
        params = notification.model_dump(mode="json", by_alias=True, exclude_unset=True)

        try:
            await self._connection.send_notification(
                CLIENT_METHODS["session_update"], params
            )
        except ConnectionError as exc:
            raise AcpTransportError(
                f"Could not tell the client that session '{notification.session_id}' "
                f"stopped with an error because the ACP connection is gone: {exc}."
            ) from exc

    async def serve(self) -> None:
        """Читает соединение до конца потока и закрывает его."""
        async with self._connection:
            await self._connection.main_loop()


class ClientConnection(ClientSideConnection):
    """Наследник ClientSideConnection SDK на его закрытых именах. Запрос
    mcp/message отдаёт answer, idle с ошибкой — client.session_update как
    IdleErrorUpdate, остальное — роутеру SDK; notify шлёт уведомление mcp/message."""

    def __init__(
        self,
        client: Client,
        answer: Callable[[McpMessage], Awaitable[McpOutcome]],
        input_stream: asyncio.StreamWriter | Transport,
        output_stream: asyncio.StreamReader | None,
        **connection_kwargs: Any,
    ) -> None:
        self._client = client
        self._answer = answer
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

        update = is_notification and method == CLIENT_METHODS["session_update"]
        if not update:
            return await self._router(method, params, is_notification)

        notification = schema.UpdateSessionNotification.model_validate(params)
        if not isinstance(notification.update, schema.IdleSessionStateUpdate):
            return await self._router(method, params, is_notification)

        if notification.update.stop_reason != StopReason.ERROR:
            return await self._router(method, params, is_notification)

        failed = IdleErrorNotification.model_validate(params)
        await self._client.session_update(
            session_id=failed.session_id, update=failed.update
        )
        return None

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
