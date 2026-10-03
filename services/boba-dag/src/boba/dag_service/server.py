"""MCP-сервер сервиса исполнения: инструменты реестра по ролям вызывающего.

Сервер собран на примитивах fastmcp. Список инструментов отдаёт провайдер
по ролям токена; вызов исполняет порт ToolServer — та же цепочка обвязок и
тот же исполнитель DAG, что в процессе чата; контекст вызова ставит
middleware из токена. Логики во входе нет: DagTool.run разбирает запрос в
ToolCall и зовёт порт.

Ошибки:
наружу уходит только итог вызова — сбой любого вида возвращается
    результатом с isError и моделью отказа (FailureResult) в structuredContent.
TokenClaimsError — токен прошёл проверку подписи, но не несёт логина и ролей;
    вызов и список инструментов для него отказывают.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from enum import StrEnum
from typing import Any, ClassVar
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import mcp_types as mt
from fastmcp import FastMCP
from fastmcp.server.auth import AccessToken, AuthProvider
from fastmcp.server.dependencies import get_access_token, get_context
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.server.providers import Provider
from fastmcp.tools import Tool
from fastmcp.tools.base import ToolResult
from fastmcp.utilities.components import FastMCPComponent
from fastmcp.utilities.versions import VersionSpec
from langchain_core.messages import ToolCall, ToolMessage
from langchain_core.tools import BaseTool
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, ValidationError

from boba.cancellation import RunCancellation, StopReason
from boba.identity.context import (
    CallContext,
    HumanInitiator,
    NoUserCredential,
    Scope,
    Subject,
)
from boba.identity.run import Runs
from boba.toolkit.calls import CallIdPrefix
from boba.toolkit.failure import FailurePacker
from boba.toolkit.result import ErrorResult, MarkdownResult, ToolResultBase
from boba.toolkit.wire import CallStatus, ResultWire, WireResult
from boba.toolrun.registry import ToolRegistry
from boba.toolrun.stream_calls import ToolServer

__all__ = [
    "CallContextMiddleware",
    "CallSchemas",
    "DagServer",
    "DagTool",
    "DagToolProvider",
    "RoleToolServers",
    "RunLimitMiddleware",
    "RunLimits",
    "TokenClaim",
    "TokenClaimsError",
    "TokenSubjects",
]

logger = logging.getLogger(__name__)


class TokenClaim(StrEnum):
    """Клеймы токена доступа, по которым сервис узнаёт вызывающего."""

    LOGIN = "login"
    ROLES = "roles"
    USER_ID = "user_id"
    PROFILE = "profile"


class TokenClaimsError(Exception):
    """Токен принят, но вызывающего по нему не узнать."""


class TokenClaims(BaseModel):
    """Клеймы вызывающего в токене доступа.

    login и roles обязательны. user_id и profile присылает клиент, у
    которого они есть (чат: строка users и профиль чата); без них сервис
    выводит идентификатор из логина и берёт профиль по умолчанию.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    login: str = Field(min_length=1)
    roles: frozenset[str]
    user_id: UUID | None = None
    profile: str | None = None


class TokenSubjects:
    """Субъект вызова по токену доступа.

    Создаётся сборкой сервера с профилем по умолчанию; им пользуются
    провайдер инструментов (по ролям — список) и middleware контекста (по
    субъекту — контекст вызова).
    """

    def __init__(self, default_profile: str) -> None:
        self._default_profile = default_profile

    def current(self) -> Subject:
        """Субъект токена текущего запроса; без токена — TokenClaimsError."""
        token = get_access_token()
        if token is None:
            msg = (
                "resolving the caller: the request carries no access token, "
                "expected a bearer token with login and roles"
            )
            raise TokenClaimsError(msg)

        return self.of(token)

    def of(self, token: AccessToken) -> Subject:
        try:
            claims = TokenClaims.model_validate(token.claims)
        except ValidationError as exc:
            msg = (
                f"resolving the caller of client {token.client_id!r}: the token "
                f"claims do not carry login and roles: {exc}"
            )
            raise TokenClaimsError(msg) from exc

        user_id = claims.user_id
        if user_id is None:
            user_id = uuid5(NAMESPACE_URL, f"boba-dag:{claims.login}")

        profile = claims.profile
        if profile is None:
            profile = self._default_profile

        return Subject(
            user_id=user_id, login=claims.login, roles=claims.roles, profile=profile
        )


class RoleToolServers:
    """Порт инструментов на набор ролей и профиль вызывающего.

    Создаётся сборкой сервера из реестра инструментов. Порт (ToolServer)
    строится один раз на пару «роли, профиль» и содержит только инструменты,
    доступные этой паре: по ним же собирается инструмент workflow.
    """

    def __init__(self, registry: ToolRegistry) -> None:
        self._registry = registry
        self._servers: dict[tuple[frozenset[str], str], ToolServer] = {}

    def of(self, subject: Subject) -> ToolServer:
        key = (subject.roles, subject.profile)
        server = self._servers.get(key)
        if server is not None:
            return server

        tools = self._registry.for_headless(subject.roles, subject.profile)
        server = self._registry.server(tools.values())
        self._servers[key] = server

        return server


class CallSchemas:
    """Схема вызова инструмента для клиента: та, что видит модель после
    всех обвязок. Создаётся провайдером инструментов."""

    def of(self, tool: BaseTool) -> dict[str, Any]:
        schema = tool.tool_call_schema
        if isinstance(schema, dict):
            return schema

        if isinstance(schema, type) and issubclass(schema, BaseModel):
            return schema.model_json_schema()

        msg = (
            f"tool {tool.name!r}: the call schema is expected to be a pydantic "
            f"model or a JSON schema, got {schema!r}"
        )
        raise TypeError(msg)


class DagTool(Tool):
    """Инструмент реестра как инструмент fastmcp.

    Создаёт его DagToolProvider из инструмента порта: схема для клиента —
    готовая схема вызова после всех обвязок, тело — вызов порта ToolServer.
    fastmcp аргументы не проверяет: проверка одна, по схеме инструмента
    внутри исполнителя.
    """

    META_CALL_ID: ClassVar[str] = "boba/tool_call_id"
    """Ключ _meta запроса с идентификатором вызова клиента."""

    _server: ToolServer = PrivateAttr()
    _wire: ResultWire = PrivateAttr()
    _failures: FailurePacker = PrivateAttr()

    def __init__(
        self, tool: BaseTool, server: ToolServer, parameters: dict[str, Any]
    ) -> None:
        super().__init__(
            name=tool.name, description=tool.description, parameters=parameters
        )
        self._server = server
        self._wire = ResultWire()
        self._failures = FailurePacker()

    async def run(self, arguments: dict[str, object]) -> ToolResult:
        call_id = CallIdPrefix.API.new_id()
        try:
            call_id = self._call_id(call_id)
            call = ToolCall(
                name=self.name, args=arguments, id=call_id, type="tool_call"
            )
            message = await self._server.call(call)
            packed = self._packed(message, call_id)
        except Exception as exc:
            logger.exception("tool[%s]: the call crashed outside its body", self.name)
            failure = self._failures.pack(exc)
            packed = self._wire.packed(
                failure.llm_view(), failure, CallStatus.ERROR, call_id
            )

        return self._result(packed)

    def _call_id(self, issued: str) -> str:
        """Идентификатор вызова клиента из _meta запроса; без него — issued."""
        request = get_context().request_context
        if request is None:
            return issued

        meta = request.meta
        if meta is None:
            return issued

        sent = meta.get(self.META_CALL_ID)
        if isinstance(sent, str) and sent:
            return sent

        return issued

    def _packed(self, message: ToolMessage, call_id: str) -> WireResult:
        content = message.content
        if not isinstance(content, str):
            content = message.text

        artifact = message.artifact
        if not isinstance(artifact, ToolResultBase):
            artifact = MarkdownResult(text=content)

        return self._wire.packed(content, artifact, CallStatus(message.status), call_id)

    @staticmethod
    def _result(packed: WireResult) -> ToolResult:
        return ToolResult(
            content=[mt.TextContent(type="text", text=packed.content)],
            structured_content=dict(packed.structured or {}),
            meta=dict(packed.meta),
            is_error=packed.is_error,
        )


class DagToolProvider(Provider):
    """Провайдер fastmcp: инструменты порта вызывающего.

    Создаётся сборкой сервера. На каждый запрос узнаёт субъекта по токену,
    берёт его порт инструментов и отдаёт инструменты порта как DagTool;
    объекты DagTool строятся один раз на порт.
    """

    def __init__(self, servers: RoleToolServers, subjects: TokenSubjects) -> None:
        super().__init__()
        self._servers = servers
        self._subjects = subjects
        self._schemas = CallSchemas()
        self._tools: dict[ToolServer, dict[str, DagTool]] = {}

    async def _list_tools(self) -> Sequence[Tool]:
        return list(self._offered().values())

    async def _get_tool(
        self, name: str, version: VersionSpec | None = None
    ) -> Tool | None:
        return self._offered().get(name)

    async def get_tasks(self) -> Sequence[FastMCPComponent]:
        """Фоновых задач у сервиса нет: вызов живёт, пока живо соединение."""
        return []

    def _offered(self) -> dict[str, DagTool]:
        server = self._servers.of(self._subjects.current())
        offered = self._tools.get(server)
        if offered is not None:
            return offered

        offered = {}
        for tool in server.tools():
            offered[tool.name] = DagTool(tool, server, self._schemas.of(tool))

        self._tools[server] = offered

        return offered


class CallContextMiddleware(Middleware):
    """Контекст вызова и запуск области вокруг вызова инструмента.

    Создаётся сборкой сервера из реестра запусков. Из токена запроса
    собирает контекст вызова (субъект, область запуска, отмена) и держит
    запуск открытым на время вызова: задачи узлов DAG и тела в потоках
    наследуют контекст, обвязки прав и значений контекста читают его как в
    чате. Клиент закрыл соединение — отмена помечается, DAG гаснет.
    """

    NO_CREDENTIAL: ClassVar[str] = "a service call carries no delegated credential"

    def __init__(self, runs: Runs, subjects: TokenSubjects) -> None:
        self._runs = runs
        self._subjects = subjects

    async def on_call_tool(
        self,
        context: MiddlewareContext[mt.CallToolRequestParams],
        call_next: CallNext[mt.CallToolRequestParams, ToolResult],
    ) -> ToolResult:
        cancellation = RunCancellation()
        call = CallContext(
            subject=self._subjects.current(),
            scope=Scope.job(uuid4().hex),
            initiator=HumanInitiator(via="api"),
            credential=NoUserCredential(reason=self.NO_CREDENTIAL),
            cancellation=cancellation,
        )

        with self._runs.open(call):
            try:
                return await call_next(context)
            except asyncio.CancelledError:
                cancellation.cancel(StopReason.ABORTED)
                raise


class RunLimits(BaseModel):
    """Пределы одновременных вызовов инструментов сервиса.

    max_runs — сколько вызовов исполняется разом; max_waiting — сколько
    ещё ждут места в памяти. Вызов сверх обоих получает отказ результатом.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_runs: int = Field(gt=0)
    max_waiting: int = Field(ge=0)


class RunLimitMiddleware(Middleware):
    """Предел одновременных вызовов инструментов.

    Создаётся сборкой сервера из пределов (RunLimits) и ставится снаружи
    middleware контекста: ждущий вызов запуска ещё не открыл. Мест нет —
    вызов ждёт; очередь ожидания полна — отказ результатом с isError.
    """

    REFUSED: ClassVar[str] = "run_limit"

    def __init__(self, limits: RunLimits) -> None:
        self._limits = limits
        self._slots = asyncio.Semaphore(limits.max_runs)
        self._running = 0
        self._waiting = 0
        self._wire = ResultWire()

    async def on_call_tool(
        self,
        context: MiddlewareContext[mt.CallToolRequestParams],
        call_next: CallNext[mt.CallToolRequestParams, ToolResult],
    ) -> ToolResult:
        if self._running >= self._limits.max_runs:
            if self._waiting >= self._limits.max_waiting:
                return self._refusal(context.message.name)

        self._waiting += 1
        try:
            await self._slots.acquire()
        finally:
            self._waiting -= 1

        self._running += 1
        try:
            return await call_next(context)
        finally:
            self._running -= 1
            self._slots.release()

    def _refusal(self, tool: str) -> ToolResult:
        limits = self._limits
        failure = ErrorResult(
            message=(
                f"tool {tool!r} is not started: the service already runs "
                f"{limits.max_runs} calls and {limits.max_waiting} more wait "
                "for a slot; repeat the call later"
            ),
            error_kind=self.REFUSED,
        )
        packed = self._wire.packed(
            failure.llm_view(), failure, CallStatus.ERROR, CallIdPrefix.API.new_id()
        )

        return ToolResult(
            content=[mt.TextContent(type="text", text=packed.content)],
            structured_content=dict(packed.structured or {}),
            meta=dict(packed.meta),
            is_error=True,
        )


class DagServer:
    """Сборка MCP-сервера сервиса исполнения.

    Создаётся корнем сборки сервиса из реестра инструментов, реестра
    запусков, проверяющего токены, профиля по умолчанию и пределов запусков;
    mcp() отдаёт готовый сервер fastmcp.
    """

    NAME: ClassVar[str] = "boba-dag"

    def __init__(
        self,
        registry: ToolRegistry,
        runs: Runs,
        auth: AuthProvider,
        default_profile: str,
        limits: RunLimits,
    ) -> None:
        self._auth = auth
        self._limits = RunLimitMiddleware(limits)
        self._subjects = TokenSubjects(default_profile)
        self._provider = DagToolProvider(RoleToolServers(registry), self._subjects)
        self._contexts = CallContextMiddleware(runs, self._subjects)

    def mcp(self) -> FastMCP:
        server = FastMCP(
            self.NAME,
            auth=self._auth,
            providers=[self._provider],
            middleware=[self._limits, self._contexts],
            dereference_schemas=False,
        )
        # сбой списка инструментов — ошибка, а не «у вызывающего нет инструментов»
        server.provider_error_strategy = "raise"

        return server
