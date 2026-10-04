"""MCP-сервер сервиса исполнения: инструменты реестра по ролям вызывающего.

Сервер собран на примитивах fastmcp. Список инструментов отдаёт провайдер
по ролям токена; вызов исполняет порт ToolServer — та же цепочка обвязок и
тот же исполнитель DAG, что в процессе чата; контекст вызова ставит
middleware из токена. Логики во входе нет: DagTool.run разбирает запрос в
ToolCall и зовёт порт.

Во время вызова сервер шлёт сигналы роста журнала уведомлениями
notifications/progress (ProgressSignals); кусок журнала читает операция
stream_read (StreamReadTool).

Ошибки:
наружу уходит только итог вызова — сбой любого вида возвращается
    результатом с isError и моделью отказа (FailureResult) в structuredContent.
TokenClaimsError — токен прошёл проверку подписи, но не несёт логина и ролей;
    вызов и список инструментов для него отказывают.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Sequence
from contextlib import AsyncExitStack, asynccontextmanager
from enum import StrEnum
from typing import Any, ClassVar
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import mcp_types as mt
from fastmcp import Context, FastMCP
from fastmcp.server.auth import AccessToken, AuthProvider, TokenVerifier
from fastmcp.server.dependencies import get_access_token, get_context
from fastmcp.server.http import StarletteWithLifespan
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.server.providers import Provider
from fastmcp.tools import Tool
from fastmcp.tools.base import ToolResult
from fastmcp.utilities.components import FastMCPComponent
from fastmcp.utilities.versions import VersionSpec
from langchain_core.messages import ToolCall, ToolMessage
from langchain_core.tools import BaseTool
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, ValidationError
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import BaseRoute, Mount, Route
from starlette.types import ASGIApp, Receive, Send
from starlette.types import Scope as AsgiScope

from boba.cancellation import RunCancellation, StopReason
from boba.canvas.journal import StreamSlice
from boba.dag_service.auth import EndpointGate
from boba.identity.context import (
    CallContext,
    HumanInitiator,
    NoUserCredential,
    Scope,
    Subject,
)
from boba.identity.run import Runs
from boba.messaging import StreamAppended, StreamFeed
from boba.toolkit.calls import CallIdPrefix
from boba.toolkit.channels import JournalChannels
from boba.toolkit.failure import FailurePacker
from boba.toolkit.result import ErrorResult, MarkdownResult, ToolResultBase
from boba.toolkit.wire import (
    CallStatus,
    JournalFeature,
    JournalRead,
    JournalSignal,
    ResultWire,
    WireResult,
)
from boba.toolrun.registry import ToolRegistry
from boba.toolrun.stream_calls import ToolServer
from boba.toolrun.streams import CallJournals, StreamPumps

__all__ = [
    "CallContextMiddleware",
    "CallSchemas",
    "DagEndpoints",
    "DagServer",
    "DagTool",
    "DagToolProvider",
    "ProgressSignals",
    "RoleToolServers",
    "RunLimitMiddleware",
    "RunLimits",
    "StreamReadTool",
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


class TokenClaimsError(Exception):
    """Токен принят, но вызывающего по нему не узнать."""


class TokenClaims(BaseModel):
    """Клеймы вызывающего в токене доступа.

    login и roles обязательны. user_id несёт готовый токен конфига, у
    которого он задан; без него сервис выводит идентификатор из логина.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    login: str = Field(min_length=1)
    roles: frozenset[str]
    user_id: UUID | None = None


class TokenSubjects:
    """Субъект вызова по токену доступа.

    Создаётся сервером endpoint'а с его профилем; им пользуются провайдер
    инструментов (по ролям и профилю — список) и middleware контекста (по
    субъекту — контекст вызова).
    """

    def __init__(self, profile: str) -> None:
        self._profile = profile

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

        return Subject(
            user_id=user_id,
            login=claims.login,
            roles=claims.roles,
            profile=self._profile,
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
    объекты DagTool строятся один раз на порт. Операции сервиса (operations)
    видит каждый вызывающий.
    """

    def __init__(
        self,
        servers: RoleToolServers,
        subjects: TokenSubjects,
        operations: Sequence[Tool],
    ) -> None:
        super().__init__()
        self._servers = servers
        self._subjects = subjects
        self._operations = tuple(operations)
        self._schemas = CallSchemas()
        self._tools: dict[ToolServer, dict[str, Tool]] = {}

    async def _list_tools(self) -> Sequence[Tool]:
        return list(self._offered().values())

    async def _get_tool(
        self, name: str, version: VersionSpec | None = None
    ) -> Tool | None:
        return self._offered().get(name)

    async def get_tasks(self) -> Sequence[FastMCPComponent]:
        """Фоновых задач у сервиса нет: вызов живёт, пока живо соединение."""
        return []

    def _offered(self) -> dict[str, Tool]:
        server = self._servers.of(self._subjects.current())
        offered = self._tools.get(server)
        if offered is not None:
            return offered

        offered = {}
        for tool in server.tools():
            offered[tool.name] = DagTool(tool, server, self._schemas.of(tool))

        for operation in self._operations:
            offered[operation.name] = operation

        self._tools[server] = offered

        return offered


class ProgressSignals(StreamFeed):
    """Реализация StreamFeed уведомлениями notifications/progress вызова.

    Создаётся CallContextMiddleware на каждый вызов инструмента. Насосы
    журнала (StreamPumps) сообщают сюда о росте каналов; сигналы ждут в
    очереди, а отправляет их задача, заведённая start() внутри запроса:
    уведомление привязано к запросу, и из чужой задачи оно не уходит.
    progress — растущий счётчик сигналов, сам сигнал едет в message
    моделью JournalSignal. Клиент без токена прогресса сигналов не получает.
    """

    def __init__(self, context: Context, run: str) -> None:
        self._context = context
        self._run = run
        self._queue: asyncio.Queue[StreamAppended | None] = asyncio.Queue()
        self._sender: asyncio.Task[None] | None = None

    def start(self) -> None:
        self._sender = asyncio.create_task(self._send(), name=f"progress:{self._run}")

    async def stream_appended(self, message: StreamAppended) -> None:
        self._queue.put_nowait(message)

    async def close(self) -> None:
        """Дожидается отправки всех сигналов: после ответа на вызов
        уведомления теряются без ошибки."""
        sender = self._sender
        if sender is None:
            return

        self._queue.put_nowait(None)
        await sender

    async def _send(self) -> None:
        sent = 0
        while True:
            message = await self._queue.get()
            if message is None:
                return

            sent += 1
            signal = JournalSignal(
                run=self._run,
                node=message.call_id,
                channel=message.channel,
                size=message.size,
                closed=message.closed,
                note=message.note,
            )
            await self._context.report_progress(
                progress=sent, message=signal.model_dump_json()
            )


class StreamReadTool(Tool):
    """Операция сервиса stream_read: кусок журнала вызова по границам строк.

    Создаётся сборкой сервера из журналов процесса. Журнал доступен только
    тому, кто вызывал инструмент: ключ журнала — пользователь токена, запуск
    и узел; чужой запуск — отказ 403.
    Итог — текст окна и его координаты (StreamSlice) в structuredContent;
    по ним клиент стыкует следующее окно. Запуск области операция не
    открывает и в предел запусков не входит.
    """

    NAME: ClassVar[str] = "stream_read"

    FORBIDDEN: ClassVar[int] = 403
    """Статус отказа в structuredContent: журнал чужой."""

    DESCRIPTION: ClassVar[str] = (
        "Read a window of the output journal of a tool call:\n"
        "   - run — run id from the result of the call\n"
        "   - node — id of the call\n"
        "   - channel — journal channel\n"
        "   - offset — window from this byte, or before — window ending at this byte"
    )

    _journals: CallJournals = PrivateAttr()
    _subjects: TokenSubjects = PrivateAttr()

    def __init__(self, journals: CallJournals, subjects: TokenSubjects) -> None:
        super().__init__(
            name=self.NAME,
            description=self.DESCRIPTION,
            parameters=JournalRead.model_json_schema(),
        )
        self._journals = journals
        self._subjects = subjects

    def feature(self) -> dict[str, Any]:
        return {JournalFeature.READ.value: self.NAME}

    async def run(self, arguments: dict[str, object]) -> ToolResult:
        try:
            request = JournalRead.model_validate(arguments)
        except ValidationError as exc:
            return self._refused(f"stream_read: the arguments are invalid: {exc}")

        if not JournalChannels.visible(request.channel):
            return self._refused(
                f"stream_read: channel {request.channel.value!r} is not readable"
            )

        user = self._subjects.current().user_key
        owner = self._journals.owner_of(request.run)
        if owner is not None and owner != user:
            return self._forbidden(request)

        piece = self._slice(request, user)
        if piece is None:
            return self._refused(
                f"stream_read: no journal of call {request.node!r} in run "
                f"{request.run!r} on channel {request.channel.value!r}"
            )

        return ToolResult(
            content=[mt.TextContent(type="text", text=piece.text)],
            structured_content=piece.model_dump(mode="json"),
        )

    def _slice(self, request: JournalRead, user: str) -> StreamSlice | None:
        if request.before is not None:
            return self._journals.recorded_slice_before(
                user, request.run, request.node, request.before, request.channel
            )

        offset = request.offset
        if offset is None:
            offset = 0

        return self._journals.recorded_slice(
            user, request.run, request.node, offset, request.channel
        )

    @staticmethod
    def _refused(message: str) -> ToolResult:
        return ToolResult(
            content=[mt.TextContent(type="text", text=message)], is_error=True
        )

    def _forbidden(self, request: JournalRead) -> ToolResult:
        """Отказ 403: журнал запуска принадлежит другому пользователю."""
        message = (
            f"403 Forbidden: the journal of run {request.run!r} belongs to another user"
        )

        return ToolResult(
            content=[mt.TextContent(type="text", text=message)],
            structured_content={"status": self.FORBIDDEN, "error": message},
            is_error=True,
        )


class CallContextMiddleware(Middleware):
    """Контекст вызова и запуск области вокруг вызова инструмента.

    Создаётся сборкой сервера из реестра запусков и журналов. Из токена
    запроса собирает контекст вызова (субъект, область запуска, отмена) и
    держит запуск открытым на время вызова: задачи узлов DAG и тела в
    потоках наследуют контекст, обвязки прав и значений контекста читают его
    как в чате. Рост журналов запуска уходит клиенту сигналами
    (ProgressSignals), идентификатор запуска — в _meta итога. Клиент закрыл
    соединение — отмена помечается, DAG гаснет. Операции сервиса
    (operations) проходят мимо: запуска у них нет.
    """

    NO_CREDENTIAL: ClassVar[str] = "a service call carries no delegated credential"

    def __init__(
        self,
        runs: Runs,
        journals: CallJournals,
        subjects: TokenSubjects,
        operations: frozenset[str],
    ) -> None:
        self._runs = runs
        self._journals = journals
        self._subjects = subjects
        self._operations = operations
        self._wire = ResultWire()

    async def on_call_tool(
        self,
        context: MiddlewareContext[mt.CallToolRequestParams],
        call_next: CallNext[mt.CallToolRequestParams, ToolResult],
    ) -> ToolResult:
        if context.message.name in self._operations:
            return await call_next(context)

        cancellation = RunCancellation()
        run_id = uuid4().hex
        call = CallContext(
            subject=self._subjects.current(),
            scope=Scope.job(run_id),
            initiator=HumanInitiator(via="api"),
            credential=NoUserCredential(reason=self.NO_CREDENTIAL),
            cancellation=cancellation,
        )

        signals = ProgressSignals(get_context(), run_id)
        pumps = StreamPumps(signals)
        signals.start()
        try:
            with (
                self._runs.open(call) as run,
                self._journals.following(run, pumps.opened),
            ):
                try:
                    result = await call_next(context)
                except asyncio.CancelledError:
                    cancellation.cancel(StopReason.ABORTED)
                    raise
        finally:
            # журналы закрыты вместе с запуском: насосы досылают итог каналов
            await pumps.close()
            await signals.close()

        meta = result.meta
        if meta is None:
            meta = {}

        return ToolResult(
            content=result.content,
            structured_content=result.structured_content,
            meta=self._wire.stamped(meta, run_id),
            is_error=result.is_error,
        )


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
    Операции сервиса (operations) запусками не считаются и не ждут.
    """

    REFUSED: ClassVar[str] = "run_limit"

    def __init__(self, limits: RunLimits, operations: frozenset[str]) -> None:
        self._limits = limits
        self._operations = operations
        self._slots = asyncio.Semaphore(limits.max_runs)
        self._running = 0
        self._waiting = 0
        self._wire = ResultWire()

    async def on_call_tool(
        self,
        context: MiddlewareContext[mt.CallToolRequestParams],
        call_next: CallNext[mt.CallToolRequestParams, ToolResult],
    ) -> ToolResult:
        if context.message.name in self._operations:
            return await call_next(context)

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
    """Сборка MCP-сервера одного endpoint'а сервиса исполнения.

    Создаётся сборкой endpoint'ов (DagEndpoints) на профиль конфига сервиса:
    из реестра инструментов, реестра запусков, журналов, проверяющего
    токены, имени профиля и общего на процесс предела запусков. Инструменты
    endpoint'а — те, что профиль и роли вошедшего разрешают; mcp() отдаёт
    готовый сервер fastmcp.
    """

    NAME: ClassVar[str] = "boba-dag"

    def __init__(  # noqa: PLR0913 — сервер собирается всеми своими входами
        self,
        registry: ToolRegistry,
        runs: Runs,
        journals: CallJournals,
        auth: AuthProvider,
        profile: str,
        limits: RunLimitMiddleware,
    ) -> None:
        self._name = f"{self.NAME}/{profile}"
        self._auth = auth
        self._subjects = TokenSubjects(profile)
        self._limits = limits
        operations: list[Tool] = []
        self._features = self._features_of(registry)
        if journals.active():
            stream_read = StreamReadTool(journals, self._subjects)
            operations.append(stream_read)
            self._features[JournalFeature.ID.value] = stream_read.feature()

        self._provider = DagToolProvider(
            RoleToolServers(registry), self._subjects, operations
        )
        self._contexts = CallContextMiddleware(
            runs, journals, self._subjects, DagEndpoints.OPERATIONS
        )

    @staticmethod
    def _features_of(registry: ToolRegistry) -> dict[str, dict[str, Any]]:
        """Возможности сервера для объявления при подключении клиента: те,
        что объявляет порт над всеми инструментами реестра (инструмент-связка,
        ключ запечатывания соединений)."""
        declared: dict[str, dict[str, Any]] = {}
        for feature, settings in registry.server(registry.tools).features().items():
            declared[feature] = dict(settings)

        return declared

    def mcp(self) -> FastMCP:
        server = FastMCP(
            self._name,
            auth=self._auth,
            providers=[self._provider],
            middleware=[self._limits, self._contexts],
            dereference_schemas=False,
            experimental_capabilities=self._features,
        )
        # сбой списка инструментов — ошибка, а не «у вызывающего нет инструментов»
        server.provider_error_strategy = "raise"

        return server


class EndpointApp:
    """Приложение одного endpoint'а MCP и путь, на котором оно отвечает."""

    def __init__(
        self, path: str, app: ASGIApp, lifespan: StarletteWithLifespan
    ) -> None:
        self.path = path
        self.app = app
        self.lifespan = lifespan

    def serves(self, path: str) -> bool:
        if path == self.path:
            return True

        return path.startswith(f"{self.path}/")


class DagEndpoints:
    """Endpoint'ы MCP сервиса исполнения: по одному на профиль конфига.

    Создаётся сборкой процесса. Профиль конфига сервиса — набор инструментов
    и роли, которым он выдан; каждый профиль отвечает отдельным MCP-сервером
    на пути `{base_path}/{профиль}`. Вошедший без ролей профиля получает 403
    (EndpointGate). Реестр инструментов, запуски, журналы и предел запусков
    у endpoint'ов общие. app() — приложение ASGI со всеми endpoint'ами и
    маршрутами входа.
    """

    OPERATIONS: ClassVar[frozenset[str]] = frozenset({StreamReadTool.NAME})
    """Операции сервиса: запуска не открывают и в предел запусков не входят."""

    def __init__(  # noqa: PLR0913 — endpoint'ы собираются всеми входами процесса
        self,
        registry: ToolRegistry,
        runs: Runs,
        journals: CallJournals,
        verifier: TokenVerifier,
        limits: RunLimits,
        base_path: str,
        routes: Sequence[Route],
    ) -> None:
        self._routes = tuple(routes)
        self._endpoints: list[EndpointApp] = []
        slots = RunLimitMiddleware(limits, self.OPERATIONS)
        for profile in sorted(registry.access.profiles()):
            grant = registry.access.profile_grant(profile)
            if grant is None:
                continue

            path = f"{base_path}/{profile}"
            server = DagServer(registry, runs, journals, verifier, profile, slots)
            served = server.mcp().http_app(path=path, stateless_http=True)
            gate = EndpointGate(served, verifier, profile, grant)
            self._endpoints.append(EndpointApp(path, gate, served))

    def paths(self) -> Sequence[str]:
        paths: list[str] = []
        for endpoint in self._endpoints:
            paths.append(endpoint.path)

        return paths

    def app(self) -> Starlette:
        routes: list[BaseRoute] = [*self._routes, Mount("/", app=self._dispatch)]

        return Starlette(routes=routes, lifespan=self._lifespan)

    async def _dispatch(self, scope: AsgiScope, receive: Receive, send: Send) -> None:
        """Запрос — endpoint'у, чей это путь; чужой путь — 404."""
        for endpoint in self._endpoints:
            if not endpoint.serves(scope["path"]):
                continue

            await endpoint.app(scope, receive, send)
            return

        known = ", ".join(self.paths())
        message = f"404 Not Found: no mcp endpoint at {scope['path']}; known: {known}"
        await JSONResponse({"error": message}, status_code=404)(scope, receive, send)

    @asynccontextmanager
    async def _lifespan(self, app: Starlette) -> AsyncIterator[None]:
        """Жизненный цикл endpoint'ов: у каждого свой менеджер сессий MCP."""
        async with AsyncExitStack() as stack:
            for endpoint in self._endpoints:
                served = endpoint.lifespan
                await stack.enter_async_context(served.router.lifespan_context(served))

            yield
