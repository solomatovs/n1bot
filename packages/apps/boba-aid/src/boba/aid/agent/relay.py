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

import asyncio
import logging
import secrets
from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence
from contextlib import nullcontext
from dataclasses import dataclass, replace
from enum import StrEnum
from functools import partial
from typing import Any, ClassVar
from uuid import UUID, uuid4

import anyio
import mcp_types as mt
from acp.exceptions import RequestError
from acp.experimental.v2 import schema
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
from pydantic import ValidationError

from boba.aid.infra.session import LiveSignIn
from boba.cancellation import RunCancellation, StopReason
from boba.connection_broker.sealing import CallSealing, McpServerInfo
from boba.connections.stored import ConnectionStoreError
from boba.identity.context import CallContext, HumanInitiator, Scope, Subject
from boba.identity.errors import AuthenticationError, RefusalError
from boba.identity.run import Runs
from boba.kerberos import KerberosError
from boba.mcp_adapter import McpTool, RoleTools
from boba.mcp_client.client import (
    McpCaller,
    McpClientError,
    McpServers,
    McpToolPort,
)
from boba.toolkit.chain import StreamPlanError
from boba.toolkit.dag import DagNode, NestedCallsFeature, ToolServer
from boba.toolkit.failure import InvokeErrorKind, ValidationText
from boba.toolkit.mcp import McpToolNames
from boba.toolrun.hosted import DirectCalls
from boba.toolrun.injected import ToolConfigError
from boba.toolrun.registry import ToolRegistry
from boba.transport.acp.protocol import (
    AcpErrorCode,
    McpErrorOutcome,
    McpMessage,
    McpOutcome,
    McpResultOutcome,
)

__all__ = [
    "ChatToolPort",
    "ChatTools",
    "ClientName",
    "McpRegistration",
    "McpRegistrations",
    "McpRelay",
    "RelayExchange",
]

logger = logging.getLogger(__name__)


class ClientName(StrEnum):
    """Как чат представляется агенту: клиентом в initialize и сервером
    MCP ретранслятора в server/discover."""

    NAME = "boba-aid"


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


@dataclass(frozen=True)
class McpRegistration:
    """Регистрация сервера процесса агента: чей это агент, какого треда и
    какой сервер объявлен ему под serverId.

    Создаётся запуском агента (AgentLauncher) и хранится в McpRegistrations
    на каждый сервер, который процесс получает объявлением type "acp".
    profile — профиль треда: по нему сервер chat выбирает набор
    инструментов. session — живой вход пользователя вкладки. server — имя
    сервера профиля либо ChatTools.NAME. Контекст вызова от агента
    ретранслятор строит из регистрации (context).
    """

    user_id: UUID
    login: str
    thread_id: str
    profile: str
    session: LiveSignIn
    server: str

    def context(self) -> CallContext:
        """Контекст вызова от агента пользователя: роли и секреты входа из
        живой сессии в момент вызова, область пользователя, свой ключ
        запуска, инициатор — человек через API.

        Ошибки:
        AuthenticationError — сессия больше не несёт годного входа.
        """
        sign_in = self.session.signed_in().sign_in()
        subject = Subject.of_user(self.user_id, self.login, sign_in.roles, self.profile)

        return CallContext(
            subject=subject,
            scope=Scope.user(self.user_id),
            run_id=uuid4().hex,
            initiator=HumanInitiator(via="api"),
            credential=sign_in.credential(),
            cancellation=RunCancellation(),
        )


class McpRegistrations:
    """Регистрации серверов процессов агента этого инстанса чата:
    serverId → регистрация.

    Создаётся провайдером процесса, один на инстанс. Запуск агента
    регистрирует серверы процесса (register): каждому — новый случайный
    serverId его объявления агенту; при остановке и перезапуске регистрации
    снимаются (revoke). Ретранслятор mcp/message находит по serverId
    регистрацию (of). Реестр живёт в памяти: процесс агента — потомок этого
    же инстанса и с его рестартом не остаётся.
    """

    KEY_BYTES: ClassVar[int] = 32

    def __init__(self) -> None:
        self._registrations: dict[str, McpRegistration] = {}

    def register(
        self, registration: McpRegistration, names: Sequence[str]
    ) -> list[schema.AcpMcpServer]:
        """Объявления серверов names для агента: на каждое имя новый serverId
        регистрации с этим сервером."""
        servers: list[schema.AcpMcpServer] = []
        for name in names:
            server_id = secrets.token_urlsafe(self.KEY_BYTES)
            self._registrations[server_id] = replace(registration, server=name)
            servers.append(schema.AcpMcpServer(name=name, server_id=server_id))

        return servers

    def revoke(self, server_id: str) -> None:
        """Снимает регистрацию; неизвестный serverId — ничего."""
        self._registrations.pop(server_id, None)

    def of(self, server_id: str) -> McpRegistration:
        """Ошибки:
        RequestError — serverId не выдан этим чатом либо снят с процессом.
        """
        registration = self._registrations.get(server_id)
        if registration is None:
            msg = (
                f"mcp/message: serverId {server_id!r} is not a server this chat "
                "registered for a running agent process, or it was revoked with "
                "its process"
            )
            raise RequestError(AcpErrorCode.UNKNOWN_SERVER, msg)

        return registration


class ChatToolPort(McpToolPort):
    """Реализация McpToolPort собственными инструментами чата в его процессе
    на один запрос mcp/message.

    Создаёт ChatTools.port из инструментов McpTool пары «роли, профиль»
    вызывающего и контекста вызова регистрации. Карточки и ответы — общего
    адаптера boba.mcp_adapter; вызов идёт под открытым запуском контекста:
    тела читают субъекта из контекста, как в ходе чата. Файлов, хода и
    расширений у инструментов чата нет.
    """

    def __init__(
        self, tools: Mapping[str, McpTool], context: CallContext, runs: Runs
    ) -> None:
        self._tools = tools
        self._context = context
        self._runs = runs

    def tools(self) -> Sequence[mt.Tool]:
        listed: list[mt.Tool] = []
        for tool in self._tools.values():
            listed.append(tool.to_mcp_tool())

        return listed

    def features(self) -> Mapping[str, Mapping[str, object]]:
        return {}

    def nested_calls(self) -> NestedCallsFeature | None:
        return None

    async def call(
        self,
        params: mt.CallToolRequestParams,
        scope: str | None,
        progress: Callable[[mt.ProgressNotificationParams], Awaitable[None]] | None,
    ) -> mt.CallToolResult | mt.InputRequiredResult:
        tool = self._tools[params.name]

        arguments: dict[str, object] = {}
        if params.arguments is not None:
            arguments = dict(params.arguments)

        with self._runs.open(self._context):
            try:
                result = await tool.run(arguments)
            except asyncio.CancelledError:
                self._context.cancellation.cancel(StopReason.ABORTED)
                raise

        return mt.CallToolResult(
            content=result.content,
            structured_content=result.structured_content,
            is_error=result.is_error,
        )


class ChatTools(RoleTools):
    """Собственные инструменты чата (connection_list, connection_search) как
    сервер chat, объявленный агенту, на базе RoleTools.

    Создаётся провайдером ретранслятора и живёт в нём рядом с McpServers:
    на запрос регистрации chat ретранслятор берёт порт ChatToolPort по
    контексту вызова. Порт пары «роли, профиль» — инструменты хода чата
    реестра с прямым вызовом тел (DirectCalls): ленты у вызова от агента
    нет, шаг рисует агент по своим уведомлениям.
    """

    NAME: ClassVar[str] = "chat"

    def __init__(self, registry: ToolRegistry, runs: Runs) -> None:
        super().__init__()
        self._registry = registry
        self._runs = runs
        self._calls = DirectCalls()

    def port(self, context: CallContext) -> ChatToolPort:
        """Порт инструментов вызывающего context."""
        subject = context.subject
        tools = self.tools(subject.roles, subject.profile)

        return ChatToolPort(tools, context, self._runs)

    def _server(self, roles: frozenset[str], profile: str) -> ToolServer:
        tools = self._registry.for_session(roles, profile)

        return self._registry.server(tools, self._calls)


class McpRelay:
    """Ретранслятор mcp/message: исполняет запросы MCP агента серверами,
    которые chainlit объявил его процессу.

    Создаётся провайдером процесса, один на инстанс; процесс агента
    (AgentProcess) отдаёт ему каждый запрос вместе с отправкой уведомлений
    своего соединения. По serverId запроса McpRegistrations даёт
    регистрацию (пользователь, тред, имя сервера), а она — контекст вызова.
    Конверт и версию запроса проверяет SDK mcp
    (classify_inbound_request), server/discover и неизвестные методы
    отвечает его lowlevel Server; свои здесь — tools/list и tools/call.
    Сервер профиля зовётся портом McpServers от имени пользователя вкладки,
    сервер chat — собственными инструментами чата в процессе (ChatTools).
    Соединения в аргументах запечатывает CallSealing,
    у инструмента со вложенными вызовами (расширение NestedCallsFeature
    сервера) — и в аргументах каждого узла по схеме его инструмента. Ход
    вызова уходит агенту уведомлением notifications/progress, ответ
    сервера — как есть. Инструменты только для интерфейса (_meta.ui.visibility
    без "model") агенту не видны и не вызываются.
    """

    UI_META: ClassVar[str] = "ui"
    """Ключ _meta инструмента с настройками MCP Apps (AppConfig)."""

    def __init__(
        self,
        registrations: McpRegistrations,
        servers: McpServers,
        sealing: CallSealing,
        chat: ChatTools,
    ) -> None:
        self._registrations = registrations
        self._servers = servers
        self._sealing = sealing
        self._chat = chat
        self._names = McpToolNames()
        self._active: set[tuple[str, str]] = set()
        # состояние запроса (serverId) ретранслятор отдаёт serve_one сам:
        # lifespan сервера здесь не запускается и только задаёт его тип
        self._server: Server[str] = Server(
            ClientName.NAME.value,
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
        self._registrations.of(message.server_id)
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
        registration = self._registrations.of(server_id)
        context = registration.context()
        port = await self._port(registration, context)
        tools = list(self._model_tools(port, registration.server))

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
        registration = self._registrations.of(server_id)
        context = registration.context()
        port = await self._port(registration, context)

        offered: dict[str, mt.Tool] = {}
        for tool in self._model_tools(port, registration.server):
            offered[tool.name] = tool

        tool = offered.get(params.name)
        if tool is None:
            msg = (
                f"mcp server {registration.server!r} offers the model no tool "
                f"{params.name!r}; expected one of {sorted(offered)}"
            )
            raise MCPError(code=mt.INVALID_PARAMS, message=msg)

        asked: Mapping[str, Any] = {}
        if params.arguments is not None:
            asked = params.arguments

        full = self._names.full(registration.server, tool.name)
        server = McpServerInfo(name=registration.server, features=port.features())
        try:
            arguments = await self._sealed(port, tool, asked, offered, server, context)
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

        return await port.call(sent, registration.thread_id, forwarded)

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

    async def _sealed(  # noqa: PLR0913 — вызов запечатывается по порту, инструменту и контексту вызова
        self,
        port: McpToolPort,
        tool: mt.Tool,
        asked: Mapping[str, Any],
        offered: Mapping[str, mt.Tool],
        server: McpServerInfo,
        context: CallContext,
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
            full, asked, tool.input_schema, server, context
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
                    node_full, call.args, node_tool.input_schema, server, context
                )
            except RefusalError as exc:
                msg = f"node {call.key!r} ({node_full}): {exc}"
                raise RefusalError(exc.kind, msg) from exc
            except ToolConfigError as exc:
                msg = f"node {call.key!r} ({node_full}): {exc}"
                raise ToolConfigError(msg) from exc

            sealed.append(call.model_copy(update={"args": args}))

        return nested.replaced(arguments, sealed)

    async def _port(
        self, registration: McpRegistration, context: CallContext
    ) -> McpToolPort:
        """Порт сервера регистрации: сервер chat — собственные инструменты
        чата в процессе, сервер профиля — порт McpServers от имени
        пользователя вкладки.

        Ошибки:
        McpClientError — сервер профиля недоступен.
        """
        if registration.server == ChatTools.NAME:
            return self._chat.port(context)

        subject = context.subject
        user = McpCaller(login=subject.login, roles=subject.roles)

        return await self._servers.port(registration.server, user)

    def _model_tools(self, port: McpToolPort, server: str) -> Iterator[mt.Tool]:
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
