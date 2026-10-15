"""Провод ACP v2 (alpha.8) агента и клиента поверх SDK acp.experimental.v2.

Ошибки:
RequestError (acp.exceptions) — собеседник ответил ошибкой ACP (коды AcpErrorCode)
    или агент выбрал версию, которой клиент не знает.
AcpTransportError — соединение закрыто или ответ на mcp/message пришёл не по конверту.
"""

from __future__ import annotations

import asyncio
import functools
import logging
from collections.abc import Awaitable, Callable
from enum import IntEnum, StrEnum
from types import ModuleType
from typing import Annotated, Any, TypeAlias, assert_never

import mcp_types
from mcp_types.jsonrpc import ErrorData
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
    SERVER_UNAVAILABLE = -33001
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


class AgentSide:
    """Сторона агента ACP v2 на AgentSideConnection.attach SDK. На чужую версию и
    рукопожатие v1 отвечает своей, уведомление mcp/message отдаёт получателю его
    активного запроса, остальное — роутеру SDK; шлёт запрос mcp/message и idle
    с ошибкой."""

    def __init__(
        self,
        agent_factory: Callable[[AgentSide, AgentSideConnection], object],
        info: schema.Implementation,
        writer: asyncio.StreamWriter | Transport,
        reader: asyncio.StreamReader | None,
    ) -> None:
        self._info = info
        self._receivers: dict[str, Callable[[McpMessage], Awaitable[None]]] = {}
        self._outcomes: TypeAdapter[McpOutcome] = TypeAdapter(McpOutcome)
        self._connection = Connection(self, writer, reader, listening=False)
        _, self._router = AgentSideConnection.attach(
            functools.partial(agent_factory, self), self._connection
        )

    async def __call__(
        self, method: str, params: JsonValue | None, is_notification: bool
    ) -> JsonValue | None:
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
