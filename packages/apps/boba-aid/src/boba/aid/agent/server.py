"""Чат как сервер инструментов своего агента: MCP-сервер инструментов
соединений под `[agent.chat_mcp].path`, запечатывание соединений в вызовах
агента и ключи процессов агента, по которым вход и ретранслятор mcp/message
узнают вызывающего.

Ключ процесса — serverId, который запуск агента (AgentLauncher) выдаёт на
каждый сервер процесса и объявляет агенту записью type "acp"; токен входа
пользователя агенту не передаётся. HTTP-вход сервера инструментов чата
живёт в FastAPI chainlit и принимает такой ключ Bearer-токеном: его зовёт
ретранслятор ключом регистрации сервера чата. По ключу чат находит запись
его держателя (ProcessKeys): пользователя, тред, профиль треда и сервер;
роли и секреты входа читаются из живой сессии пользователя в момент вызова.
Сессий MCP нет: сервер stateless streamable HTTP, собран из общего адаптера
boba.mcp_adapter; инструменты — собственные инструменты чата из реестра
(connection_list, connection_search) по ролям пользователя и профилю треда.
Запечатывание (CallSealing) находит по схеме инструмента
параметры-соединения (SealedParamSchema) и заменяет имя соединения профилем,
запечатанным ключом сервера MCP из его возможностей (SealFeature);
неизвестное или невыданное соединение — отказ с причиной.

Ошибки:
AuthenticationError — ключа нет, он неизвестен или снят либо сессия его
    держателя больше не несёт годного входа: вход отвечает 401.
RefusalError — соединение неизвестно, не выдано пользователю или сервер
    инструмента не принимает соединений.
ToolConfigError — соединение не годится инструменту.
ConnectionStoreError — таблица соединений недоступна.
KerberosError — билет к соединению не выпущен.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from collections.abc import Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, replace
from datetime import timedelta
from enum import StrEnum
from typing import Any, ClassVar
from uuid import UUID, uuid4

import mcp_types as mt
from acp.experimental.v2 import schema
from fastmcp import FastMCP
from fastmcp.server.auth import TokenVerifier
from fastmcp.server.dependencies import get_access_token
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools.base import ToolResult
from mcp.server.auth.provider import AccessToken
from pydantic import ValidationError
from starlette.routing import Route

from boba.aid.agent.config import ChatMcpConfig
from boba.aid.infra.session import LiveSignIn
from boba.cancellation import RunCancellation, StopReason
from boba.connection_broker.user_connections import ArmedConnections
from boba.connections.marks import ConnectionRefusal
from boba.connections.sealed import (
    ConnectionSeal,
    SealedParamSchema,
    SealFeature,
    SealKey,
)
from boba.identity.context import (
    CallContext,
    CallContexts,
    Credential,
    HumanInitiator,
    Scope,
    Subject,
)
from boba.identity.errors import AuthenticationError, RefusalError
from boba.identity.run import Runs
from boba.mcp_adapter import CallerServers, McpReplies, McpToolProvider
from boba.toolkit.dag import ToolServer
from boba.toolkit.failure import ValidationText
from boba.toolkit.result import ErrorResult
from boba.toolrun.hosted import DirectCalls
from boba.toolrun.registry import ToolRegistry

__all__ = [
    "CallSealing",
    "ChatCaller",
    "ChatCallers",
    "ChatMcpServer",
    "ChatTokenVerifier",
    "ChatToolServers",
    "KeyHolder",
    "McpServerInfo",
    "ProcessKeys",
]

logger = logging.getLogger(__name__)


class ChatServerRefusal(StrEnum):
    """Виды отказов сервера инструментов чата вызову до исполнителя."""

    UNAUTHENTICATED = "unauthenticated"


class HttpMethod(StrEnum):
    """Методы HTTP входов чата для агента."""

    GET = "GET"
    POST = "POST"
    DELETE = "DELETE"


@dataclass(frozen=True)
class KeyHolder:
    """Держатель ключа процесса агента: чей это агент, какого треда и для
    какого сервера выдан ключ.

    Создаётся запуском агента (AgentLauncher): на каждый сервер, который
    процесс получает объявлением type "acp", — свой ключ, он же serverId.
    profile — профиль треда: по нему сервер инструментов чата выбирает набор
    инструментов. session — живой вход пользователя вкладки. server — имя
    сервера профиля либо сервера инструментов чата ([agent.chat_mcp].name).
    """

    user_id: UUID
    login: str
    thread_id: str
    profile: str
    session: LiveSignIn
    server: str


class ProcessKeys:
    """Ключи процессов агента этого инстанса чата: ключ → его держатель.

    Создаётся провайдером процесса, один на инстанс. Запуск агента
    регистрирует серверы процесса (register): ключ каждого — serverId его
    объявления агенту; при остановке и перезапуске ключи снимаются
    (revoke). Ретранслятор mcp/message и сервер инструментов чата находят
    по ключу держателя (holder). Реестр живёт в
    памяти: процесс агента — потомок этого же инстанса и с его рестартом не
    остаётся.
    """

    KEY_BYTES: ClassVar[int] = 32

    def __init__(self) -> None:
        self._holders: dict[str, KeyHolder] = {}

    def issue(self, holder: KeyHolder) -> str:
        """Новый случайный ключ держателя."""
        key = secrets.token_urlsafe(self.KEY_BYTES)
        self._holders[key] = holder

        return key

    def register(
        self, holder: KeyHolder, names: Sequence[str]
    ) -> list[schema.AcpMcpServer]:
        """Объявления серверов names для агента: на каждое имя новый ключ
        держателя с этим сервером, он же serverId."""
        servers: list[schema.AcpMcpServer] = []
        for name in names:
            key = self.issue(replace(holder, server=name))
            servers.append(schema.AcpMcpServer(name=name, server_id=key))

        return servers

    def revoke(self, key: str) -> None:
        """Снимает ключ; неизвестный ключ — ничего."""
        self._holders.pop(key, None)

    def holder(self, key: str) -> KeyHolder | None:
        """Держатель ключа; None — ключ не выдан этим инстансом или снят."""
        return self._holders.get(key)


@dataclass(frozen=True)
class ChatCaller:
    """Вызывающий по ключу процесса: субъект (пользователь, его роли, профиль
    треда) и секреты его входа для билетов к соединениям."""

    subject: Subject
    credential: Credential


class ChatCallers:
    """Вызывающий по Bearer ключу процесса агента.

    Создаётся провайдером процесса; им пользуются сервер инструментов (порт
    по ролям и профилю) и ретранслятор mcp/message (субъект и креды для
    соединений).
    Держателя ключа отдаёт реестр ProcessKeys; роли и секреты входа читаются
    из живой сессии держателя.
    """

    def __init__(self, keys: ProcessKeys) -> None:
        self._keys = keys

    def of_key(self, key: str) -> ChatCaller:
        """Ошибки:
        AuthenticationError — ключ неизвестен или снят либо сессия держателя
            не несёт годного входа.
        """
        holder = self._keys.holder(key)
        if holder is None:
            msg = (
                "the bearer token is not a key of a running agent process of "
                "this chat instance: expected the serverId the chat registered "
                "for the agent, got an unknown or revoked one"
            )
            raise AuthenticationError(msg)

        sign_in = holder.session.signed_in().sign_in()
        subject = Subject.of_user(
            holder.user_id, holder.login, sign_in.roles, holder.profile
        )

        return ChatCaller(subject=subject, credential=sign_in.credential())

    def context(self, caller: ChatCaller) -> CallContext:
        """Контекст вызова от агента пользователя: область пользователя,
        свой ключ запуска, инициатор — человек через API."""
        return CallContext(
            subject=caller.subject,
            scope=Scope.user(caller.subject.user_id),
            run_id=uuid4().hex,
            initiator=HumanInitiator(via="api"),
            credential=caller.credential,
            cancellation=RunCancellation(),
        )


class ChatTokenVerifier(TokenVerifier):
    """Проверка Bearer ключа процесса агента на сервере инструментов чата.

    Отдаётся в FastMCP(auth=…): без ключа, с неизвестным или снятым ключом
    fastmcp отвечает 401. Токен входа пользователя ключом не является.
    """

    def __init__(self, keys: ProcessKeys) -> None:
        super().__init__()
        self._keys = keys

    async def verify_token(self, token: str) -> AccessToken | None:
        holder = self._keys.holder(token)
        if holder is None:
            logger.info("chat mcp: the bearer token is not a key of an agent process")
            return None

        return AccessToken(
            token=token, client_id=holder.login, scopes=[], expires_at=None, claims={}
        )


class ChatToolServers(CallerServers):
    """Реализация CallerServers чата: порт собственных инструментов чата на
    роли и профиль вызывающего.

    Порт строится один раз на пару «роли, профиль» прямым вызовом тел
    (DirectCalls): ленты у вызова от агента нет, шаг рисует агент по своим
    уведомлениям.
    """

    def __init__(self, registry: ToolRegistry, callers: ChatCallers) -> None:
        self._registry = registry
        self._callers = callers
        self._calls = DirectCalls()
        self._servers: dict[tuple[frozenset[str], str], ToolServer] = {}

    async def current(self) -> ToolServer:
        """Ошибки:
        AuthenticationError — запрос без принятого ключа процесса либо
            сессия его держателя не несёт годного входа.
        """
        token = get_access_token()
        if token is None:
            msg = (
                "chat mcp: the request carries no accepted bearer token, "
                "expected the key of the agent process"
            )
            raise AuthenticationError(msg)

        caller = self._callers.of_key(token.token)

        return self.of(caller.subject)

    def of(self, subject: Subject) -> ToolServer:
        key = (subject.roles, subject.profile)
        server = self._servers.get(key)
        if server is not None:
            return server

        tools = self._registry.for_session(subject.roles, subject.profile)
        server = self._registry.server(tools, self._calls)
        self._servers[key] = server

        return server


class ChatCallMiddleware(Middleware):
    """Контекст вызова вокруг инструмента чата, вызванного агентом.

    По ключу запроса собирает контекст вызова (субъект, область
    пользователя, креды входа) и держит запуск открытым на время вызова:
    тела инструментов читают субъекта из контекста, как в ходе чата.
    Клиент закрыл соединение — отмена помечается.
    """

    def __init__(self, callers: ChatCallers, runs: Runs) -> None:
        self._callers = callers
        self._runs = runs
        self._replies = McpReplies()

    async def on_call_tool(
        self,
        context: MiddlewareContext[mt.CallToolRequestParams],
        call_next: CallNext[mt.CallToolRequestParams, ToolResult],
    ) -> ToolResult:
        token = get_access_token()
        if token is None:
            return self._refused(
                context.message.name,
                "chat mcp: the call carries no accepted bearer token",
            )

        try:
            caller = self._callers.of_key(token.token)
        except AuthenticationError as exc:
            return self._refused(context.message.name, str(exc))

        call = self._callers.context(caller)
        with self._runs.open(call):
            try:
                return await call_next(context)
            except asyncio.CancelledError:
                call.cancellation.cancel(StopReason.ABORTED)
                raise

    def _refused(self, tool: str, message: str) -> ToolResult:
        failure = ErrorResult(
            message=message, error_kind=ChatServerRefusal.UNAUTHENTICATED
        )

        return self._replies.refused(tool, failure)


class ChatMcpServer:
    """MCP-сервер инструментов чата для агента: сборка FastMCP и его
    приложение под путём `[agent.chat_mcp].path`.

    Создаётся провайдером процесса; bootstrap ставит маршрут в приложение
    chainlit и держит жизненный цикл сервера вместе с контейнером.
    """

    NAME: ClassVar[str] = "boba-aid"

    def __init__(
        self,
        config: ChatMcpConfig,
        verifier: ChatTokenVerifier,
        servers: ChatToolServers,
        calls: ChatCallMiddleware,
    ) -> None:
        self._path = config.path
        server = FastMCP(
            f"{self.NAME}/{config.name}",
            auth=verifier,
            providers=[McpToolProvider(servers, ())],
            middleware=[calls],
            dereference_schemas=False,
        )
        # сбой списка инструментов — ошибка, а не «у вызывающего нет инструментов»
        server.provider_error_strategy = "raise"
        self._app = server.http_app(path=config.path, stateless_http=True)

    @property
    def path(self) -> str:
        return self._path

    def route(self) -> Route:
        """Маршрут приложения chainlit: запрос уходит приложению fastmcp как
        есть, путь внутри него тот же."""
        return Route(
            self._path,
            endpoint=self._app,
            methods=[method.value for method in HttpMethod],
        )

    def lifespan(self) -> AbstractAsyncContextManager[Any]:
        """Жизненный цикл менеджера сессий fastmcp: входит bootstrap."""
        return self._app.router.lifespan_context(self._app)


@dataclass(frozen=True)
class McpServerInfo:
    """Сервер MCP вызываемого инструмента и возможности, которые он объявил:
    по ним запечатывание находит ключ сервера."""

    name: str
    features: Mapping[str, Mapping[str, object]]


class CallSealing:
    """Запечатывание соединений в аргументах вызова инструмента сервера MCP:
    имя соединения пользователя заменяется профилем, запечатанным ключом
    сервера, либо вызов получает отказ.

    Создаётся провайдером call_sealing; зовёт ретранслятор mcp/message.
    Какие аргументы — соединения и какого вида, читает из схемы
    инструмента (SealedParamSchema); ключ сервера — из объявленных им
    возможностей (SealFeature); соединение пользователя по виду и имени
    берёт ArmedConnections в контексте вызывающего.
    """

    def __init__(
        self,
        callers: ChatCallers,
        connections: ArmedConnections,
        contexts: CallContexts,
        ttl: timedelta,
    ) -> None:
        self._callers = callers
        self._connections = connections
        self._contexts = contexts
        self._ttl = ttl
        self._declared = SealedParamSchema()

    async def seal(
        self,
        tool: str,
        arguments: Mapping[str, Any],
        tool_schema: Mapping[str, Any],
        server: McpServerInfo,
        caller: ChatCaller,
    ) -> dict[str, Any]:
        """Аргументы вызова tool с запечатанными соединениями на месте имён;
        у инструмента без параметров-соединений — как есть. Значение не
        строка уходит как есть, его отвергнет сервер.

        Ошибки:
        RefusalError — соединение неизвестно или не выдано пользователю либо
            сервер инструмента не принимает соединений.
        ToolConfigError — соединение не годится инструменту.
        ConnectionStoreError — таблица соединений недоступна.
        KerberosError — билет к соединению не выпущен.
        """
        updated: dict[str, Any] = dict(arguments)
        kinds = self._declared.kinds_of(tool_schema)
        if not kinds:
            return updated

        seal = ConnectionSeal(self._key(server))
        with self._contexts.applied(self._callers.context(caller)):
            for param, kind in kinds.items():
                name = updated.get(param)
                if not isinstance(name, str):
                    continue

                sealed = await self._connections.sealed(kind, name, self._ttl)
                updated[param] = seal.seal(sealed)

                logger.info(
                    "sealing: connection %s of kind %s sealed in %s of %s for %s",
                    name,
                    kind,
                    param,
                    tool,
                    caller.subject.login,
                )

        return updated

    def _key(self, server: McpServerInfo) -> SealKey:
        """Ключ сервера инструмента из возможностей, которые он объявил.

        Ошибки:
        RefusalError — сервер не принимает соединений.
        """
        declared = server.features.get(SealFeature.ID)
        if declared is None:
            msg = (
                f"mcp server {server.name!r} does not declare the feature "
                f"{SealFeature.ID!r}, so it does not accept connections"
            )
            raise RefusalError(ConnectionRefusal.SEAL_KEY_UNKNOWN, msg)

        try:
            return SealFeature.model_validate(declared).key
        except ValidationError as exc:
            msg = (
                f"mcp server {server.name!r}: the feature {SealFeature.ID!r} does "
                f"not carry an encryption key: {ValidationText.of(exc)}"
            )
            raise RefusalError(ConnectionRefusal.SEAL_KEY_UNKNOWN, msg) from exc
