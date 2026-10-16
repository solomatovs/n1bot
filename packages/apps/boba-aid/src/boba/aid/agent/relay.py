"""Ретранслятор MCP-over-ACP: ответы chainlit на запросы mcp/message агента
серверам, которые chainlit объявил ему записями type "acp".

Ошибки:
RequestError (acp.exceptions) — ошибка доставки MCP-over-ACP:
    UNKNOWN_SERVER — serverId не выдан этим чатом или снят;
    INVALID_PARAMS — requestId уже в работе;
    BACKEND_FAILED — сервер за регистрацией недоступен, ответил не по схеме
    инструмента либо вход пользователя вкладки больше не годен, исхода MCP нет.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Mapping
from contextlib import asynccontextmanager, nullcontext
from functools import partial
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
from mcp.shared.jsonrpc_dispatcher import progress_token_from_params
from mcp.shared.message import MessageMetadata
from mcp.shared.transport_context import TransportContext
from mcp_types.jsonrpc import ErrorData
from pydantic import SecretStr, ValidationError

from boba.aid.agent.config import ChatMcpConfig
from boba.aid.agent.server import (
    CallSealing,
    ChatCaller,
    ChatCallers,
    ChatMcpServer,
    KeyHolder,
    McpServerInfo,
    ProcessKeys,
)
from boba.connections.stored import ConnectionStoreError
from boba.identity.errors import AuthenticationError, RefusalError
from boba.kerberos import KerberosError
from boba.mcp_client.client import (
    BearerAuth,
    HttpEndpoint,
    McpCaller,
    McpClientError,
    McpServerConfig,
    McpServers,
    McpToolServer,
)
from boba.toolkit.chain import StreamPlanError
from boba.toolkit.dag import DagNode
from boba.toolkit.failure import InvokeErrorKind, ValidationText
from boba.toolkit.mcp import McpToolNames
from boba.toolrun.injected import ToolConfigError
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
    своего соединения. serverId запроса — ключ процесса: держателя
    (пользователь, тред, имя сервера) даёт ProcessKeys, вызывающего —
    ChatCallers. Конверт и версию запроса проверяет SDK mcp
    (classify_inbound_request), server/discover и неизвестные методы
    отвечает его lowlevel Server; свои здесь — tools/list и tools/call.
    Сервер профиля зовётся портом McpServers от имени пользователя вкладки,
    сервер инструментов чата до переноса в процесс — по его HTTP-адресу
    ключом регистрации. Соединения в аргументах запечатывает CallSealing,
    у инструмента со вложенными вызовами (расширение NestedCallsFeature
    сервера) — и в аргументах каждого узла по схеме его инструмента. Ход
    вызова уходит агенту уведомлением notifications/progress, ответ
    сервера — как есть. Инструменты только для интерфейса (_meta.ui.visibility
    без "model") агенту не видны и не вызываются.
    """

    UI_META: ClassVar[str] = "ui"
    """Ключ _meta инструмента с настройками MCP Apps (AppConfig)."""

    def __init__(  # noqa: PLR0913 — ретранслятор собирается всеми входами процесса
        self,
        keys: ProcessKeys,
        callers: ChatCallers,
        servers: McpServers,
        sealing: CallSealing,
        chat: ChatMcpConfig,
        public_url: str,
    ) -> None:
        self._keys = keys
        self._callers = callers
        self._servers = servers
        self._sealing = sealing
        self._chat = chat
        public = urlsplit(public_url)
        self._chat_endpoint = HttpEndpoint.model_validate(
            {
                "scheme": public.scheme,
                "host": public.hostname,
                "port": public.port,
                "path": public.path.rstrip("/") + chat.path,
            }
        )
        """Адрес сервера инструментов чата без входа: ключ регистрации
        ставится Bearer-токеном на запрос."""
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
        except MCPError as exc:
            return McpErrorOutcome(error=exc.error)
        except ValidationError as exc:
            text = (
                f"mcp/message {message.method!r} to server {message.server_id!r}: "
                f"the params do not match the request: {ValidationText.of(exc)}"
            )
            error = ErrorData(code=mt.INVALID_PARAMS, message=text)
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
        server_id = ctx.lifespan_context
        holder = self._holder(server_id)
        caller = self._callers.of_key(server_id)
        async with self._port(server_id, holder, caller) as port:
            tools = list(self._model_tools(port, holder.server))

        return mt.ListToolsResult(tools=tools)

    async def _call_tool(
        self, ctx: ServerRequestContext[str], params: mt.CallToolRequestParams
    ) -> mt.CallToolResult | mt.InputRequiredResult:
        """Вызов инструмента для модели: соединения запечатаны, у вызова со
        вложенными вызовами — и в узлах, тред — областью вызова, отказ
        запечатывания — isError с причиной.

        Ошибки:
        MCPError — инструмента у сервера нет либо он не для модели.
        """
        server_id = ctx.lifespan_context
        holder = self._holder(server_id)
        caller = self._callers.of_key(server_id)
        async with self._port(server_id, holder, caller) as port:
            offered: dict[str, mt.Tool] = {}
            for tool in self._model_tools(port, holder.server):
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

            full = self._names.full(holder.server, tool.name)
            server = McpServerInfo(name=holder.server, features=port.features())
            try:
                arguments = await self._sealed(
                    port, tool, asked, offered, server, caller
                )
            except (RefusalError, ToolConfigError) as exc:
                reason = f"{full}: {exc}"
                logger.warning("mcp relay: sealing refused: %s", reason)
                denied = mt.TextContent(text=reason)

                return mt.CallToolResult(content=[denied], is_error=True)
            except (ConnectionStoreError, KerberosError) as exc:
                reason = f"{full}: sealing the connections failed: {exc}"
                logger.warning("mcp relay: %s", reason)
                failed = mt.TextContent(text=reason)

                return mt.CallToolResult(content=[failed], is_error=True)

            sent = params.model_copy(update={"arguments": arguments})

            forwarded: Callable[[mt.ProgressNotificationParams], Awaitable[None]] | None
            forwarded = None
            token = progress_token_from_params(ctx.params)
            if token is not None:
                forwarded = partial(self._forwarded, ctx, token)

            return await port.call(sent, holder.thread_id, forwarded)

    async def _forwarded(
        self,
        ctx: ServerRequestContext[str],
        token: mt.ProgressToken,
        params: mt.ProgressNotificationParams,
    ) -> None:
        """Уведомление сервера о ходе вызова — агенту как есть, с _meta,
        под токеном прогресса его запроса token."""
        own = params.model_copy(update={"progress_token": token})
        notification = mt.ProgressNotification(params=own)
        await ctx.session.send_notification(
            notification, related_request_id=ctx.request_id
        )

    async def _sealed(  # noqa: PLR0913 — вызов запечатывается по порту, инструменту и вызывающему
        self,
        port: McpToolServer,
        tool: mt.Tool,
        asked: Mapping[str, Any],
        offered: Mapping[str, mt.Tool],
        server: McpServerInfo,
        caller: ChatCaller,
    ) -> dict[str, Any]:
        """Аргументы вызова tool с запечатанными соединениями: по схеме
        инструмента, а у инструмента, который сервер объявил несущим
        вложенные вызовы, — и в каждом узле по схеме инструмента узла. Узлы
        разбираются по форме, которую объявил сервер; вызов не по форме
        уходит как есть, его отвергнет сервер.

        Ошибки:
        RefusalError — инструмент узла не предложен модели, соединение
            неизвестно или не выдано пользователю.
        ToolConfigError — соединение не годится инструменту.
        ConnectionStoreError — таблица соединений недоступна.
        KerberosError — билет к соединению не выпущен.
        """
        full = self._names.full(server.name, tool.name)
        arguments = await self._sealing.seal(
            full, asked, tool.input_schema, server, caller
        )

        nested = port.nested_calls()
        if nested is None:
            return arguments

        if nested.tool != tool.name:
            return arguments

        try:
            calls = nested.calls(arguments)
        except StreamPlanError as exc:
            logger.info("mcp relay: nodes are sent unsealed: %s", exc)
            return dict(arguments)

        sealed: list[DagNode] = []
        for call in calls:
            node_full = self._names.full(server.name, call.tool)
            node_tool = offered.get(call.tool)
            if node_tool is None:
                msg = (
                    f"node {call.key!r} ({node_full}): mcp server {server.name!r} "
                    f"offers the model no tool {call.tool!r}; expected one of "
                    f"{sorted(offered)}"
                )
                raise RefusalError(InvokeErrorKind.UNKNOWN_TOOL, msg)

            try:
                args = await self._sealing.seal(
                    node_full, call.args, node_tool.input_schema, server, caller
                )
            except RefusalError as exc:
                msg = f"node {call.key!r} ({node_full}): {exc}"
                raise RefusalError(exc.kind, msg) from exc
            except ToolConfigError as exc:
                msg = f"node {call.key!r} ({node_full}): {exc}"
                raise ToolConfigError(msg) from exc

            sealed.append(call.model_copy(update={"args": args}))

        return nested.replaced(arguments, sealed)

    @asynccontextmanager
    async def _port(
        self, server_id: str, holder: KeyHolder, caller: ChatCaller
    ) -> AsyncIterator[McpToolServer]:
        """Открытый порт сервера регистрации server_id. Сервер профиля — порт
        McpServers от имени пользователя вкладки; сервер чата — его HTTP-вход
        с ключом регистрации, открытый на один запрос.

        Ошибки:
        McpClientError — сервер недоступен.
        """
        if holder.server != self._chat.name:
            subject = caller.subject
            user = McpCaller(login=subject.login, roles=subject.roles)
            yield await self._servers.port(holder.server, user)
            return

        auth = BearerAuth(token=SecretStr(server_id))
        endpoint = self._chat_endpoint.model_copy(update={"auth": auth})
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

    def _model_tools(self, port: McpToolServer, server: str) -> Iterator[mt.Tool]:
        """Инструменты сервера server, видимые модели: без _meta.ui.visibility
        либо с "model" в нём.

        Ошибки:
        MCPError — _meta.ui инструмента не по форме MCP Apps: ошибка сервера.
        """
        for tool in port.tools():
            meta = tool.meta
            if meta is None:
                yield tool
                continue

            ui = meta.get(self.UI_META)
            if ui is None:
                yield tool
                continue

            try:
                visibility = AppConfig.model_validate(ui).visibility
            except ValidationError as exc:
                msg = (
                    f"mcp server {server!r}: tool {tool.name!r} carries _meta.ui "
                    "that is not an MCP Apps tool config, so its visibility to "
                    f"the model is unknown: {ValidationText.of(exc)}"
                )
                raise MCPError(code=mt.INTERNAL_ERROR, message=msg) from exc

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
