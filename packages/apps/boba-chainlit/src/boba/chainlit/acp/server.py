"""Чат как сервер инструментов своего агента: MCP-сервер инструментов
соединений под `[agent.chat_mcp].path` и обработчик хука PreToolUse под
`[agent.hook].path`.

Оба входа живут в FastAPI chainlit и принимают на каждом запросе Bearer JWT
входа chainlit — тот, что `SessionServers` и `SettingsFileBuilder` отдают
агенту в `mcpServers` и в заголовках хука. По токену чат узнаёт вызывающего
(ChatCallers): логин, роли и профиль из клеймов, строку users — из таблицы.
Сессий MCP нет: сервер stateless streamable HTTP, собран из общего адаптера
boba.mcp_adapter; инструменты — собственные инструменты чата из реестра
(connection_list, connection_search) по ролям и профилю входа. Обработчик
хука читает событие PreToolUse, находит по схеме инструмента параметры-
соединения (SealedParamSchema), заменяет имя соединения профилем,
запечатанным ключом сервера MCP из `mcp_server.features` (SealFeature), и
отвечает `updatedInput`; неизвестное или невыданное соединение — `deny` с
причиной.

Ошибки:
AuthenticationError — токена нет, он не принят либо у входа нет строки
    users: вход отвечает 401.
RefusalError — входу не выдан ни один профиль чата: 403 через
    DomainErrorMiddleware.
ConnectionStoreError — таблица соединений недоступна: обработчик отвечает
    500, агент блокирует вызов по onFailure.
KerberosError — билет к соединению не выпущен: то же.
DataUnavailableError — таблица users недоступна.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum
from http import HTTPStatus
from typing import Any, ClassVar
from uuid import uuid4

import mcp_types as mt
from fastmcp import FastMCP
from fastmcp.server.auth import TokenVerifier
from fastmcp.server.dependencies import get_access_token
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools.base import ToolResult
from mcp.server.auth.provider import AccessToken
from pydantic import ValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from boba.agent.hooks import (
    HookDecision,
    HookEventName,
    HookReplyBody,
    HookSpecificOutput,
    PreToolUseEvent,
)
from boba.cancellation import RunCancellation, StopReason
from boba.chainlit.acp.config import ChatMcpConfig, HookConfig
from boba.chat.profiles import ChatProfiles
from boba.connection_broker.user_connections import ArmedConnections
from boba.connections.marks import ConnectionRefusal
from boba.connections.sealed import (
    ConnectionSeal,
    SealedParamSchema,
    SealFeature,
    SealKey,
)
from boba.identity.api import PersistedUsers
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
from boba.identity.token import TokenReader, TokenRejectedError
from boba.mcp_adapter import CallerServers, McpReplies, McpToolProvider
from boba.runtime.http import RequestTokens
from boba.toolkit.dag import ToolServer
from boba.toolkit.failure import ValidationText
from boba.toolkit.result import ErrorResult
from boba.toolrun.hosted import DirectCalls
from boba.toolrun.injected import ToolConfigError
from boba.toolrun.registry import ToolRegistry

__all__ = [
    "ChatCaller",
    "ChatCallers",
    "ChatMcpServer",
    "ChatTokenVerifier",
    "ChatToolServers",
    "HookSealing",
    "PreToolUseHandler",
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


class ResponseHeader(StrEnum):
    """Заголовки отказа входа по форме RFC 6750."""

    CHALLENGE = "WWW-Authenticate"
    BEARER = "Bearer"


class DetailKey(StrEnum):
    """Ключ текста отказа в JSON-ответе входа, как у chainlit."""

    DETAIL = "detail"


@dataclass(frozen=True)
class ChatCaller:
    """Вызывающий по токену: субъект (строка users, роли, профиль) и секреты
    его входа для билетов к соединениям."""

    subject: Subject
    credential: Credential


class ChatCallers:
    """Вызывающий по Bearer JWT входа chainlit.

    Создаётся провайдером процесса; им пользуются сервер инструментов (порт
    по ролям и профилю) и обработчик хука (субъект и креды для соединений).
    Токен читает читатель сессий процесса, строку users — таблица users,
    профиль инструментов — выбор входа из клеймов по правилам ChatProfiles.
    """

    def __init__(
        self, tokens: TokenReader, users: PersistedUsers, profiles: ChatProfiles
    ) -> None:
        self._tokens = tokens
        self._users = users
        self._profiles = profiles

    async def of_token(self, token: str) -> ChatCaller:
        """Ошибки:
        AuthenticationError — токен не принят либо у входа нет строки users.
        RefusalError — входу не выдан ни один профиль чата.
        """
        try:
            claims = self._tokens.read(token)
        except TokenRejectedError as exc:
            msg = f"sign-in token rejected ({exc.reason.value}): {exc}"
            raise AuthenticationError(msg) from exc

        user = await self._users.get_user(claims.identifier)
        if user is None:
            msg = (
                f"sign-in {claims.identifier!r} has no users row: the chat tools "
                "serve only users who signed in to the chat"
            )
            raise AuthenticationError(msg)

        sign_in = claims.sign_in()
        profile = self._profiles.resolve(None, sign_in)
        subject = Subject.of_user(
            user.id, claims.identifier, sign_in.roles, profile.name
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
    """Проверка Bearer JWT входа chainlit на сервере инструментов чата.

    Отдаётся в FastMCP(auth=…): без токена, с чужим или просроченным токеном
    fastmcp отвечает 401. Клеймы токена едут в AccessToken: по ним
    ChatCallers узнаёт вызывающего без второго чтения подписи.
    """

    def __init__(self, tokens: TokenReader) -> None:
        super().__init__()
        self._tokens = tokens

    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            claims = self._tokens.read(token)
        except TokenRejectedError as exc:
            logger.info("chat mcp: sign-in token rejected (%s)", exc.reason.value)
            return None

        return AccessToken(
            token=token,
            client_id=claims.identifier,
            scopes=[],
            expires_at=claims.exp,
            claims=claims.model_dump(mode="json"),
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
        AuthenticationError — запрос без принятого токена или вход без строки users.
        RefusalError — входу не выдан ни один профиль чата.
        """
        token = get_access_token()
        if token is None:
            msg = (
                "chat mcp: the request carries no accepted bearer token, "
                "expected the sign-in JWT of the chat"
            )
            raise AuthenticationError(msg)

        caller = await self._callers.of_token(token.token)

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

    По токену запроса собирает контекст вызова (субъект, область
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
            caller = await self._callers.of_token(token.token)
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

    NAME: ClassVar[str] = "boba-chainlit"

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


class HookSealing:
    """Решение обработчика хука по событию PreToolUse: запечатать соединения
    в аргументах либо отказать.

    Создаётся обработчиком. Какие аргументы — соединения и какого вида,
    читает из схемы инструмента события (SealedParamSchema); ключ сервера —
    из его возможностей в событии (SealFeature); соединение пользователя по
    виду и имени берёт ArmedConnections в контексте вызывающего.
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

    async def reply(self, event: PreToolUseEvent, caller: ChatCaller) -> HookReplyBody:
        """Ошибки:
        ConnectionStoreError — таблица соединений недоступна.
        KerberosError — билет к соединению не выпущен.
        """
        kinds = self._declared.kinds_of(event.tool_schema)
        if not kinds:
            return self._decided(HookDecision.ALLOW, "", None)

        try:
            updated = await self._sealed(event, caller, kinds)
        except (RefusalError, ToolConfigError) as exc:
            logger.warning(
                "hook: sealing connections of %s for %s refused: %s",
                event.tool_name,
                caller.subject.login,
                exc,
            )
            return self._decided(HookDecision.DENY, f"{event.tool_name}: {exc}", None)

        return self._decided(HookDecision.ALLOW, "", updated)

    async def _sealed(
        self, event: PreToolUseEvent, caller: ChatCaller, kinds: Mapping[str, str]
    ) -> dict[str, Any]:
        """Аргументы события с запечатанными соединениями на месте имён.
        Значение не строка — уходит как есть, его отвергнет сервер."""
        seal = ConnectionSeal(self._key(event))
        updated: dict[str, Any] = dict(event.tool_input)
        with self._contexts.applied(self._callers.context(caller)):
            for param, kind in kinds.items():
                name = updated.get(param)
                if not isinstance(name, str):
                    continue

                sealed = await self._connections.sealed(kind, name, self._ttl)
                updated[param] = seal.seal(sealed)

                logger.info(
                    "hook: connection %s of kind %s sealed in %s of %s for %s",
                    name,
                    kind,
                    param,
                    event.tool_name,
                    caller.subject.login,
                )

        return updated

    def _key(self, event: PreToolUseEvent) -> SealKey:
        """Ключ сервера инструмента из возможностей, которые он объявил агенту.

        Ошибки:
        RefusalError — сервера у инструмента нет либо он не принимает соединений.
        """
        server = event.mcp_server
        if server is None:
            msg = (
                f"{event.tool_name} is not a tool of an MCP server, so a connection "
                "cannot be sent to it sealed"
            )
            raise RefusalError(ConnectionRefusal.SEAL_KEY_UNKNOWN, msg)

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

    @staticmethod
    def _decided(
        decision: HookDecision, reason: str, updated: Mapping[str, Any] | None
    ) -> HookReplyBody:
        return HookReplyBody(
            hook_specific_output=HookSpecificOutput(
                hook_event_name=HookEventName.PRE_TOOL_USE.value,
                permission_decision=decision,
                permission_decision_reason=reason,
                updated_input=updated,
            )
        )


class PreToolUseHandler:
    """HTTP-обработчик хука PreToolUse агента под `[agent.hook].path`.

    Создаётся провайдером процесса, маршрут ставит bootstrap. Запрос: Bearer
    JWT входа и JSON события по форме хуков (PreToolUseEvent); ответ — JSON
    по форме ответа (HookReplyBody): `allow` с `updatedInput`, где имена
    соединений заменены запечатанными профилями, либо `deny` с причиной.
    """

    def __init__(
        self,
        config: HookConfig,
        tokens: RequestTokens,
        callers: ChatCallers,
        sealing: HookSealing,
    ) -> None:
        self._path = config.path
        self._tokens = tokens
        self._callers = callers
        self._sealing = sealing

    def route(self) -> Route:
        return Route(self._path, self.handle, methods=[HttpMethod.POST.value])

    async def handle(self, request: Request) -> Response:
        token = self._tokens.of_request(request)
        if token is None:
            return self._unauthorized(
                "the hook request carries no bearer token, expected the "
                "sign-in JWT of the chat"
            )

        try:
            caller = await self._callers.of_token(token)
        except AuthenticationError as exc:
            return self._unauthorized(str(exc))

        try:
            event = PreToolUseEvent.model_validate(await request.json())
        except (ValueError, ValidationError) as exc:
            return JSONResponse(
                {
                    DetailKey.DETAIL.value: (
                        f"the hook request body is not a PreToolUse event: {exc}"
                    )
                },
                status_code=HTTPStatus.BAD_REQUEST,
            )

        reply = await self._sealing.reply(event, caller)

        return JSONResponse(
            reply.model_dump(mode="json", by_alias=True, exclude_none=True)
        )

    @staticmethod
    def _unauthorized(message: str) -> Response:
        return JSONResponse(
            {DetailKey.DETAIL.value: message},
            status_code=HTTPStatus.UNAUTHORIZED,
            headers={ResponseHeader.CHALLENGE.value: ResponseHeader.BEARER.value},
        )
