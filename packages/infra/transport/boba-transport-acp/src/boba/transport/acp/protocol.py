"""Провод ACP v2 для агента и клиента поверх SDK acp.experimental.v2.

Здесь то, чего нет в SDK 1.0.0rc3 и что нужно обеим сторонам: конверт
mcp/message по alpha.8 (MCP-over-ACP), idle со stopReason "error",
ответ агента своей версией на чужую и на рукопожатие v1, общие значения
протокола и стороны соединения, которые перехватывают mcp/message до
роутера SDK.

Ошибки:
RequestError (acp.exceptions) — собеседник ответил ошибкой ACP (коды AcpErrorCode)
    или агент выбрал версию, которой клиент не знает (клиент закрыл соединение).
AcpTransportError — соединение закрыто или ответ на mcp/message пришёл не по конверту.
"""

from __future__ import annotations

import asyncio
import functools
from collections.abc import Awaitable, Callable
from enum import IntEnum, StrEnum
from types import ModuleType
from typing import Annotated, Any, TypeAlias, assert_never

import mcp_types
from mcp_types.jsonrpc import ErrorData
from mcp_types.version import LATEST_MODERN_VERSION
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
    """Ошибка провода: соединение ACP закрыто или собеседник ответил на
    mcp/message не по конверту alpha.8. Ошибки собеседника по существу
    приходят как RequestError SDK."""


class AcpVersion(IntEnum):
    """Версии ACP, на которых говорит провод; значение берётся из SDK.

    Реестр «версия → проводной модуль SDK»: агент сверяет с ним версию из
    initialize, остальной код получает модели версии через wire().
    """

    V2 = v2.PROTOCOL_VERSION

    def wire(self) -> ModuleType:
        """Модуль SDK с моделями и сторонами соединения этой версии."""
        match self:
            case AcpVersion.V2:
                return v2

        assert_never(self)


class McpVersion(StrEnum):
    """Версии MCP, которые несёт конверт mcp/message; значение — из mcp_types.

    Реестр «версия → модуль моделей»: модели и разбор методов версии даёт
    wire(), своих моделей MCP пакет не держит.
    """

    V2026_07_28 = LATEST_MODERN_VERSION

    def wire(self) -> ModuleType:
        """Модуль mcp_types с моделями этой версии."""
        match self:
            case McpVersion.V2026_07_28:
                return mcp_types

        assert_never(self)


class AcpErrorCode(IntEnum):
    """Коды внешних ошибок ACP, которые стороны различают: стандартные
    JSON-RPC и коды привязки MCP-over-ACP. Бросаются как RequestError SDK."""

    INVALID_PARAMS = -32602
    METHOD_NOT_FOUND = -32601
    INTERNAL_ERROR = -32603
    RESOURCE_NOT_FOUND = -32002
    REQUEST_CANCELLED = -32800
    BINDING_LIMIT = -33000
    SERVER_UNAVAILABLE = -33001
    BACKEND_FAILED = -33002


class StopReason(StrEnum):
    """Причины конца хода (`stopReason` в idle); ERROR — ход сорвался,
    подробности в поле error у IdleErrorUpdate."""

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
        """Вызов закончен, новых состояний у него не будет."""
        return self in (ToolStatus.COMPLETED, ToolStatus.FAILED, ToolStatus.CANCELLED)


class ErrorDataKey(StrEnum):
    """Ключи `data` ошибки JSON-RPC, которые пишет агент и читает клиент."""

    KIND = "kind"
    MESSAGE = "message"
    DETAILS = "details"
    SESSION_ID = "sessionId"
    UP_TO_MESSAGE_ID = "upToMessageId"


class MetaKey(StrEnum):
    """Расширения boba в `_meta` запросов ACP: точка среза ветки у
    `session/fork` — messageId сообщения, до которого копируется история."""

    UP_TO_MESSAGE_ID = "boba/upToMessageId"


class McpMessage(BaseModel):
    """Параметры mcp/message по alpha.8: запрос агента к серверу MCP клиента
    и уведомление клиента агенту по активному запросу (формы совпадают).

    params — внутренние параметры MCP как есть; null равен отсутствию.
    """

    server_id: Annotated[str, Field(alias="serverId", min_length=1)]
    request_id: Annotated[str, Field(alias="requestId", min_length=1)]
    method: str
    params: dict[str, Any] | None = None
    field_meta: Annotated[dict[str, Any] | None, Field(alias="_meta")] = None


class McpResultOutcome(BaseModel):
    """Исход mcp/message с результатом MCP; result обязателен и может быть
    null, поэтому модель пишется с exclude_unset, а не exclude_none."""

    result: Any
    field_meta: Annotated[dict[str, Any] | None, Field(alias="_meta")] = None


class McpErrorOutcome(BaseModel):
    """Исход mcp/message с внутренней ошибкой MCP (не ошибкой ACP)."""

    error: ErrorData
    field_meta: Annotated[dict[str, Any] | None, Field(alias="_meta")] = None


McpOutcome: TypeAlias = McpResultOutcome | McpErrorOutcome


class IdleErrorUpdate(schema.IdleSessionStateUpdate):
    """idle со stopReason "error" (alpha.8): ход сорвался, error — причина.

    В SDK rc3 у idle нет поля error, и модель SDK молча его отбрасывает;
    этот наследник его несёт. stop_reason отправитель ставит сам
    (StopReason.ERROR).
    """

    error: schema.Error | None = None


class IdleErrorNotification(schema.UpdateSessionNotification):
    """Параметры session/update, где update — IdleErrorUpdate.

    Поле update конкретного типа, поэтому error переживает и запись, и
    разбор; через объединение update у модели SDK оно теряется. Агент шлёт
    его AgentSide.send_update, клиент разбирает в ClientConnection.
    """

    update: IdleErrorUpdate


class AgentSide:
    """Сторона агента ACP v2: соединение SDK с перехватом до его роутера.

    Собирает Connection SDK и роутер агента через публичный
    AgentSideConnection.attach и сама служит обработчиком соединения:
    на initialize с чужой версией или рукопожатие v1 отвечает своей версией,
    не пуская запрос в SDK; уведомление mcp/message разбирает в McpMessage и
    отдаёт accept; остальное — роутеру SDK. Запрос mcp/message клиенту и idle
    с ошибкой шлёт сама: в SDK rc3 для них старые формы.
    """

    def __init__(
        self,
        agent_factory: Callable[[AgentSide, AgentSideConnection], object],
        accept: Callable[[McpMessage], Awaitable[None]],
        info: schema.Implementation,
        writer: asyncio.StreamWriter | Transport,
        reader: asyncio.StreamReader | None,
    ) -> None:
        self._accept = accept
        self._info = info
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
            await self._accept(McpMessage.model_validate(params))
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

    async def request(self, message: McpMessage) -> McpOutcome:
        """Запрос mcp/message клиенту; отмена задачи шлёт $/cancel_request."""
        params = message.model_dump(mode="json", by_alias=True, exclude_unset=True)

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
    """Сторона клиента ACP v2: ClientSideConnection SDK с перехватом до его роутера.

    У ClientSideConnection в rc3 нет attach, поэтому __init__ повторяет его
    тело на закрытых именах SDK (_ClientRouter, InitializationState,
    open_connection) — это держится точным пином 1.0.0rc3 и тестом-стражем.
    Запрос mcp/message от агента разбирается в McpMessage и уходит answer,
    idle с ошибкой — клиенту как IdleErrorUpdate; остальное — роутеру SDK.
    Уведомление mcp/message агенту шлёт notify.
    """

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
