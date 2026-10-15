"""Ретранслятор MCP-over-ACP: ответы chainlit на запросы mcp/message агента.

Агент шлёт запросы MCP 2026-07-28 серверам, которые chainlit объявил ему
записями type "acp"; serverId записи — ключ процесса (ProcessKeys), по нему
находится держатель: пользователь, тред и имя сервера. Конверт и версию
запроса проверяет SDK mcp (classify_inbound_request), server/discover, ping
и неизвестные методы отвечает его lowlevel Server; свои здесь — tools/list и
tools/call. Вызов идёт портом McpServers от имени пользователя вкладки,
соединения в аргументах запечатывает HookSealing, ход вызова уходит агенту
уведомлением notifications/progress, ответ сервера — как есть.

Ошибки:
RequestError (acp.exceptions) — ошибка доставки MCP-over-ACP: UNKNOWN_SERVER
    — serverId не выдан этим чатом или снят; INVALID_PARAMS — requestId уже
    в работе; BACKEND_FAILED — сервер за регистрацией недоступен либо вход
    пользователя вкладки больше не годен, исхода MCP нет.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Mapping
from contextlib import asynccontextmanager, nullcontext
from typing import Any, ClassVar
from urllib.parse import urlsplit

import anyio
import mcp_types as mt
from acp.exceptions import RequestError
from fastmcp.apps.config import AppConfig
from mcp.server.connection import Connection, NotifyOnlyOutbound
from mcp.server.context import ServerRequestContext
from mcp.server.lowlevel import Server
from mcp.server.runner import serve_one
from mcp.shared.dispatcher import CallOptions, DispatchContext
from mcp.shared.exceptions import MCPError, NoBackChannelError
from mcp.shared.inbound import InboundLadderRejection, classify_inbound_request
from mcp.shared.jsonrpc_dispatcher import (
    handler_exception_to_error_data,
    progress_token_from_params,
)
from mcp.shared.message import MessageMetadata
from mcp.shared.transport_context import TransportContext
from mcp_types.jsonrpc import ErrorData
from pydantic import ValidationError

from boba.agent.hooks import HookDecision, McpServerInfo, PreToolUseEvent
from boba.agent.records import PermissionMode
from boba.chainlit.agent.config import ChatMcpConfig
from boba.chainlit.agent.server import (
    ChatCallers,
    ChatMcpServer,
    HookSealing,
    KeyHolder,
    ProcessKeys,
)
from boba.connections.stored import ConnectionStoreError
from boba.identity.errors import AuthenticationError
from boba.kerberos import KerberosError
from boba.mcp_client.client import (
    HttpEndpoint,
    McpCaller,
    McpClientError,
    McpServerConfig,
    McpServers,
    McpToolServer,
)
from boba.toolkit.mcp import McpToolNames
from boba.transport.acp.protocol import (
    AcpErrorCode,
    McpErrorOutcome,
    McpMessage,
    McpOutcome,
    McpResultOutcome,
)

__all__ = ["McpRelay", "RelayExchange"]

logger = logging.getLogger(__name__)


class RelayExchange(DispatchContext[TransportContext]):
    """Реализация DispatchContext SDK mcp на один запрос mcp/message.

    Создаётся ретранслятором (McpRelay) на запрос агента и уходит в
    serve_one SDK; он же — выход соединения запроса (NotifyOnlyOutbound).
    Уведомления сервера и ход вызова уходят агенту уведомлением mcp/message
    под serverId и requestId запроса. Запросов сервера к клиенту MCP
    2026-07-28 не допускает: send_raw_request отказывает.
    """

    KIND: ClassVar[str] = "acp"

    def __init__(
        self, message: McpMessage, notify: Callable[[McpMessage], Awaitable[None]]
    ) -> None:
        self._message = message
        self._notify = notify
        self._transport = TransportContext(kind=self.KIND, can_send_request=False)
        self._cancel = anyio.Event()

    @property
    def transport(self) -> TransportContext:
        return self._transport

    @property
    def can_send_request(self) -> bool:
        return False

    @property
    def request_id(self) -> mt.RequestId | None:
        return self._message.request_id

    @property
    def message_metadata(self) -> MessageMetadata:
        return None

    @property
    def cancel_requested(self) -> anyio.Event:
        return self._cancel

    async def send_raw_request(
        self,
        method: str,
        params: Mapping[str, Any] | None,
        opts: CallOptions | None = None,
    ) -> dict[str, Any]:
        raise NoBackChannelError(method)

    async def notify(
        self,
        method: str,
        params: Mapping[str, Any] | None,
        opts: CallOptions | None = None,
    ) -> None:
        sent: dict[str, Any] | None = None
        if params is not None:
            sent = dict(params)

        message = McpMessage(
            server_id=self._message.server_id,
            request_id=self._message.request_id,
            method=method,
            params=sent,
        )
        await self._notify(message)

    async def progress(
        self, progress: float, total: float | None = None, message: str | None = None
    ) -> None:
        """Ход вызова агенту под его progressToken; без токена — ничего."""
        token = progress_token_from_params(self._message.params)
        if token is None:
            return

        params = mt.ProgressNotificationParams(
            progress_token=token, progress=progress, total=total, message=message
        )
        notification = mt.ProgressNotification(params=params)
        dumped = params.model_dump(mode="json", by_alias=True, exclude_none=True)
        await self.notify(notification.method, dumped)


class McpRelay:
    """Ретранслятор mcp/message: исполняет запросы MCP агента серверами,
    которые chainlit объявил его процессу.

    Создаётся провайдером процесса, один на инстанс; процесс агента
    (AgentProcess) отдаёт ему каждый запрос вместе с отправкой уведомлений
    своего соединения. Держателя serverId даёт ProcessKeys, вызывающего —
    ChatCallers, порт сервера профиля — McpServers от имени пользователя
    вкладки; сервер инструментов чата до переноса в процесс зовётся по его
    HTTP-адресу ключом регистрации. Инструменты только для интерфейса
    (_meta.ui.visibility без "model") агенту не видны и не вызываются.
    """

    UI_META: ClassVar[str] = "ui"
    """Ключ _meta инструмента с настройками MCP Apps (AppConfig)."""

    def __init__(  # noqa: PLR0913 — ретранслятор собирается всеми входами процесса
        self,
        keys: ProcessKeys,
        callers: ChatCallers,
        servers: McpServers,
        sealing: HookSealing,
        chat: ChatMcpConfig,
        public_url: str,
    ) -> None:
        self._keys = keys
        self._callers = callers
        self._servers = servers
        self._sealing = sealing
        self._chat = chat
        self._public_url = public_url
        self._names = McpToolNames()
        self._active: set[tuple[str, str]] = set()
        # состояние запроса (serverId) ретранслятор отдаёт serve_one сам:
        # lifespan сервера здесь не запускается и только задаёт его тип
        self._server: Server[str] = Server(
            ChatMcpServer.NAME,
            lifespan=lambda _server: nullcontext(""),
            on_list_tools=self._list_tools,
            on_call_tool=self._call_tool,
        )

    async def answer(
        self, message: McpMessage, notify: Callable[[McpMessage], Awaitable[None]]
    ) -> McpOutcome:
        """Исход запроса mcp/message; notify — уведомления агенту по ходу.

        Ошибки:
        RequestError — нет регистрации serverId, requestId уже в работе либо
            сервер за регистрацией не дал исхода.
        """
        self._holder(message.server_id)
        active = (message.server_id, message.request_id)
        if active in self._active:
            msg = (
                f"mcp/message {message.method!r} to server {message.server_id!r}: "
                f"requestId {message.request_id!r} is already in work, expected "
                "a new requestId for every request"
            )
            raise RequestError(AcpErrorCode.INVALID_PARAMS, msg)

        envelope = {"method": message.method, "params": message.params}
        route = classify_inbound_request(envelope)
        if isinstance(route, InboundLadderRejection):
            error = ErrorData(code=route.code, message=route.message, data=route.data)
            return McpErrorOutcome(error=error)

        exchange = RelayExchange(message, notify)
        connection = Connection.from_envelope(
            route.protocol_version,
            route.client_info,
            route.client_capabilities,
            outbound=NotifyOnlyOutbound(exchange),
        )
        self._active.add(active)
        try:
            result = await serve_one(
                self._server,
                exchange,
                message.method,
                message.params,
                connection=connection,
                lifespan_state=message.server_id,
            )
        except (MCPError, ValidationError) as exc:
            error = handler_exception_to_error_data(exc)
            if error is None:
                raise

            return McpErrorOutcome(error=error)
        except (McpClientError, AuthenticationError) as exc:
            msg = (
                f"mcp/message {message.method!r} to server {message.server_id!r} "
                f"got no MCP outcome: {exc}"
            )
            raise RequestError(AcpErrorCode.BACKEND_FAILED, msg) from exc
        finally:
            self._active.discard(active)

        return McpResultOutcome(result=result)

    async def _list_tools(
        self, ctx: ServerRequestContext[str], params: mt.PaginatedRequestParams | None
    ) -> mt.ListToolsResult:
        async with self._port(ctx.lifespan_context) as port:
            tools = list(self._model_tools(port))

        return mt.ListToolsResult(tools=tools)

    async def _call_tool(
        self, ctx: ServerRequestContext[str], params: mt.CallToolRequestParams
    ) -> mt.CallToolResult | mt.InputRequiredResult:
        """Вызов инструмента для модели: соединения запечатаны, тред —
        областью вызова, отказ запечатывания — isError с причиной.

        Ошибки:
        MCPError — инструмента у сервера нет либо он не для модели.
        """
        server_id = ctx.lifespan_context
        holder = self._holder(server_id)
        async with self._port(server_id) as port:
            offered: dict[str, mt.Tool] = {}
            for tool in self._model_tools(port):
                offered[tool.name] = tool

            tool = offered.get(params.name)
            if tool is None:
                msg = (
                    f"mcp server {holder.server!r} offers the model no tool "
                    f"{params.name!r}; expected one of {sorted(offered)}"
                )
                raise MCPError(code=mt.INVALID_PARAMS, message=msg)

            asked: Mapping[str, Any] = {}
            if params.arguments is not None:
                asked = params.arguments

            event = PreToolUseEvent(
                session_id=holder.thread_id,
                cwd=holder.thread_id,
                permission_mode=PermissionMode.DEFAULT,
                tool_name=self._names.full(holder.server, tool.name),
                tool_input=asked,
                tool_use_id=str(ctx.request_id),
                tool_schema=tool.input_schema,
                mcp_server=McpServerInfo(name=holder.server, features=port.features()),
            )
            caller = self._callers.of_key(server_id)
            try:
                reply = await self._sealing.reply(event, caller)
            except (ConnectionStoreError, KerberosError) as exc:
                reason = f"{event.tool_name}: sealing the connections failed: {exc}"
                logger.warning("mcp relay: %s", reason)
                text = mt.TextContent(text=reason)

                return mt.CallToolResult(content=[text], is_error=True)

            arguments = asked
            decided = reply.hook_specific_output
            if decided is not None:
                if decided.permission_decision is HookDecision.DENY:
                    text = mt.TextContent(text=decided.permission_decision_reason)

                    return mt.CallToolResult(content=[text], is_error=True)

                if decided.updated_input is not None:
                    arguments = decided.updated_input

            sent = params.model_copy(update={"arguments": dict(arguments)})

            return await port.call(sent, holder.thread_id, ctx.session.report_progress)

    @asynccontextmanager
    async def _port(self, server_id: str) -> AsyncIterator[McpToolServer]:
        """Открытый порт сервера регистрации. Сервер профиля — порт McpServers
        от имени пользователя вкладки; сервер чата — его HTTP-вход с ключом
        регистрации, открытый на один запрос.

        Ошибки:
        RequestError — регистрация снята.
        AuthenticationError — вход пользователя вкладки больше не годен.
        McpClientError — сервер недоступен.
        """
        holder = self._holder(server_id)
        subject = self._callers.of_key(server_id).subject
        if holder.server != self._chat.name:
            caller = McpCaller(login=subject.login, roles=subject.roles)
            yield await self._servers.port(holder.server, caller)
            return

        public = urlsplit(self._public_url)
        endpoint = HttpEndpoint.model_validate(
            {
                "scheme": public.scheme,
                "host": public.hostname,
                "port": public.port,
                "path": public.path.rstrip("/") + self._chat.path,
                "auth": {"auth": "bearer", "token": server_id},
            }
        )
        config = McpServerConfig(
            endpoint=endpoint,
            connect_timeout_sec=self._chat.connect_timeout_sec,
            call_timeout_sec=self._chat.call_timeout_sec,
        )
        port = McpToolServer(holder.server, config)
        try:
            await port.open()
            yield port
        finally:
            await port.close()

    def _model_tools(self, port: McpToolServer) -> Iterator[mt.Tool]:
        """Инструменты сервера, видимые модели: без _meta.ui.visibility либо
        с "model" в нём."""
        for tool in port.tools():
            meta = tool.meta
            if meta is None:
                yield tool
                continue

            ui = meta.get(self.UI_META)
            if ui is None:
                yield tool
                continue

            visibility = AppConfig.model_validate(ui).visibility
            if visibility is None:
                yield tool
                continue

            if "model" in visibility:
                yield tool

    def _holder(self, server_id: str) -> KeyHolder:
        """Ошибки:
        RequestError — serverId не выдан этим чатом либо снят с процессом.
        """
        holder = self._keys.holder(server_id)
        if holder is None:
            msg = (
                f"mcp/message: serverId {server_id!r} is not a server this chat "
                "registered for a running agent process, or it was revoked with "
                "its process"
            )
            raise RequestError(AcpErrorCode.UNKNOWN_SERVER, msg)

        return holder
