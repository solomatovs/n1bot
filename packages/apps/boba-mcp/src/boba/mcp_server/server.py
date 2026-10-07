"""MCP-сервер сервиса исполнения: инструменты реестра по ролям вызывающего.

Сервер собран на примитивах fastmcp. Список инструментов отдаёт провайдер
по ролям токена; вызов исполняет порт ToolServer — та же цепочка обвязок и
тот же исполнитель DAG, что в процессе чата; контекст вызова ставит
middleware из токена. Логики во входе нет: McpTool.run разбирает запрос в
вызов-узел DagNode и зовёт порт; итог порта (NodeOutcome) уходит клиенту
ответом McpReplies — стандартными полями результата MCP (ResultWire). Тем же
компонентом отвечают операции сервиса и middleware: отказ сервера — тоже
итог с моделью отказа.

Во время вызова сервер пересылает клиенту отчёты тела о ходе работы
уведомлениями notifications/progress (CallProgress); журнал вызова читает
операция stream_read (StreamReadTool) по адресу из итога вызова, адрес
загрузки файла отдаёт операция file_upload (FileUploadTool).

Ошибки:
наружу уходит только итог вызова — сбой любого вида возвращается
    результатом с isError и моделью отказа (FailureResult) в structuredContent.
TokenClaimsError — токен прошёл проверку подписи, но не несёт логина и ролей;
    вызов и список инструментов для него отказывают.
ValidationError — structuredContent ответа несёт сведения о вызове, которые
    не проходят свою модель (нарушение контракта McpReplies).
ProgressChannelError — тело написало в канал отчётов о ходе работы строку,
    которая не проходит модель ProgressReport; остаётся в логе сервиса,
    вызов от неё не срывается.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import AsyncExitStack, asynccontextmanager
from enum import StrEnum
from typing import Any, ClassVar
from uuid import uuid4

import mcp_types as mt
from fastmcp import Context, FastMCP
from fastmcp.server.auth import AuthProvider
from fastmcp.server.dependencies import get_context
from fastmcp.server.extensions import ServerExtension
from fastmcp.server.http import StarletteWithLifespan
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.server.providers import Provider
from fastmcp.tools import Tool
from fastmcp.tools.base import ToolResult
from fastmcp.utilities.components import FastMCPComponent
from fastmcp.utilities.versions import VersionSpec
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, ValidationError
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import BaseRoute, Mount, Route
from starlette.types import ASGIApp, Receive, Send
from starlette.types import Scope as AsgiScope

from boba.access import GrantCheck, ProfileGrant, ToolGrant, ToolGrants
from boba.cancellation import RunCancellation, StopReason, ToolStopped
from boba.canvas.journal import StreamSlice
from boba.canvas.keys import ObjectKey
from boba.identity.context import (
    CallContext,
    HumanInitiator,
    Subject,
)
from boba.identity.run import Runs
from boba.identity.signin import ProfileCatalog
from boba.mcp_server.auth import (
    CallScopeError,
    CallScopes,
    ServiceAuth,
    TokenSubjects,
)
from boba.mcp_server.files import FileRoutes, JournalRoutes, RouteCallers
from boba.runtime.storage import LocalStorageConfig, StorageFactory
from boba.toolkit.calls import CallIdPrefix, CallViews
from boba.toolkit.channels import JournalChannels, ToolChannel
from boba.toolkit.dag import DagNode, NodeOutcome, NodeOutcomes, ToolCard, ToolServer
from boba.toolkit.failure import FailurePacker, ValidationText
from boba.toolkit.ports import ProgressReport
from boba.toolkit.result import ErrorResult, FailureResult
from boba.toolkit.wire import (
    FilesFeature,
    JournalFeature,
    JournalRead,
    ResultWire,
)
from boba.toolrun.hosted import DirectCalls
from boba.toolrun.registry import ToolRegistry
from boba.toolrun.streams import CallJournals, ToolStream

__all__ = [
    "CallContextMiddleware",
    "CallProgress",
    "EndpointCatalog",
    "FileUploadTool",
    "McpEndpoints",
    "McpReplies",
    "McpServer",
    "McpTool",
    "McpToolProvider",
    "RoleToolServers",
    "RunLimitMiddleware",
    "RunLimits",
    "ServerRefusal",
    "StreamReadTool",
]

logger = logging.getLogger(__name__)


class RoleToolServers:
    """Порт инструментов на набор ролей и профиль вызывающего.

    Создаётся сборкой сервера из реестра инструментов. Порт (ToolServer)
    строится один раз на пару «роли, профиль» и содержит только инструменты,
    доступные этой паре: по ним же собирается инструмент workflow.
    """

    def __init__(self, registry: ToolRegistry) -> None:
        self._registry = registry
        self._calls = DirectCalls()
        self._servers: dict[tuple[frozenset[str], str], ToolServer] = {}

    def of(self, subject: Subject) -> ToolServer:
        key = (subject.roles, subject.profile)
        server = self._servers.get(key)
        if server is not None:
            return server

        tools = self._registry.for_headless(subject.roles, subject.profile)
        server = self._registry.server(tools.values(), self._calls)
        self._servers[key] = server

        return server


class ServerRefusal(StrEnum):
    """Виды отказов сервера вызову: не дошёл до исполнителя либо остановлен
    вместе с запуском."""

    INVALID_REQUEST = "invalid_request"
    FORBIDDEN = "forbidden"
    NOT_FOUND = "not_found"
    RUN_LIMIT = "run_limit"
    STOPPED = "stopped"


class McpReplies:
    """Ответы сервера клиенту в формате fastmcp — единственное место, где
    итог вызова становится ToolResult.

    Создаётся каждым, кто отвечает на tools/call: инструментом реестра
    (McpTool), операциями сервиса (StreamReadTool, FileUploadTool) и
    middleware контекста и пределов. Итог вызова (NodeOutcome) и отказ
    сервера едут клиенту полями ResultWire: текст для модели в content,
    сведения о вызове и результат семейства в structuredContent. Ответ
    операции сервиса — её модель в structuredContent.
    """

    def __init__(self) -> None:
        self._wire = ResultWire()
        self._outcomes = NodeOutcomes()

    def of(self, outcome: NodeOutcome) -> ToolResult:
        """Итог вызова инструмента."""
        packed = self._wire.packed(outcome)

        return ToolResult(
            content=[mt.TextContent(type="text", text=packed.content)],
            structured_content=dict(packed.structured or {}),
            is_error=packed.is_error,
        )

    def refused(self, tool: str, failure: FailureResult) -> ToolResult:
        """Отказ сервера вызову tool, который до исполнителя не дошёл:
        идентификатор вызова выдаётся здесь."""
        call_id = CallIdPrefix.API.new_id()

        return self.of(self._outcomes.unnamed(call_id, tool, failure))

    def answered(self, text: str, answer: BaseModel) -> ToolResult:
        """Ответ операции сервиса: текст и её модель ответа."""
        return ToolResult(
            content=[mt.TextContent(type="text", text=text)],
            structured_content=answer.model_dump(mode="json"),
        )

    def stamped(self, result: ToolResult, run: str) -> ToolResult:
        """Тот же ответ с запуском run в сведениях о вызове: по запуску и
        идентификатору вызова клиент читает журнал."""
        structured = result.structured_content
        if structured is None:
            return result

        return ToolResult(
            content=result.content,
            structured_content=self._wire.stamped(structured, run),
            meta=result.meta,
            is_error=result.is_error,
        )


class McpTool(Tool):
    """Инструмент реестра как инструмент fastmcp.

    Адаптер ядра запуска к fastmcp. Создаёт его McpToolProvider из карточки
    инструмента порта: схема для клиента — готовая схема вызова после всех
    обвязок с видом аргументов карточки для ленты клиента (CallViews),
    тело — вызов порта ToolServer узлом DagNode под идентификатором,
    который выдаёт сервер; итог порта (NodeOutcome) уходит клиенту ответом
    McpReplies.
    fastmcp аргументы не проверяет: проверка одна, по схеме инструмента
    внутри исполнителя.
    """

    _server: ToolServer = PrivateAttr()
    _replies: McpReplies = PrivateAttr()
    _outcomes: NodeOutcomes = PrivateAttr()
    _failures: FailurePacker = PrivateAttr()

    def __init__(self, card: ToolCard, server: ToolServer) -> None:
        super().__init__(
            name=card.name,
            description=card.description,
            parameters=CallViews().marked(card.parameters, card.views),
        )
        self._server = server
        self._replies = McpReplies()
        self._outcomes = NodeOutcomes()
        self._failures = FailurePacker()

    async def run(self, arguments: dict[str, object]) -> ToolResult:
        call_id = CallIdPrefix.API.new_id()
        try:
            call = DagNode.model_validate(
                {"key": call_id, "tool": self.name, "args": arguments}
            )
            outcome = await self._server.call(call)
        except Exception as exc:
            logger.exception("tool[%s]: the call crashed outside its body", self.name)
            failure = self._failures.pack(exc)
            outcome = self._outcomes.unnamed(call_id, self.name, failure)

        return self._replies.of(outcome)


class McpToolProvider(Provider):
    """Провайдер fastmcp: инструменты порта вызывающего.

    Создаётся сборкой сервера. На каждый запрос узнаёт субъекта по токену,
    берёт его порт инструментов и отдаёт инструменты порта как McpTool;
    объекты McpTool строятся один раз на порт. Операции сервиса (operations)
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
        for card in server.tools():
            offered[card.name] = McpTool(card, server)

        for operation in self._operations:
            offered[operation.name] = operation

        self._tools[server] = offered

        return offered


class ProgressChannelError(Exception):
    """Тело инструмента написало в канал отчётов то, что отчётом не является."""


class CallProgress:
    """Ход работы вызова уведомлениями notifications/progress.

    Создаётся CallContextMiddleware на каждый вызов инструмента и следит за
    журналами запуска (CallJournals.following зовёт opened). Тело пишет
    отчёты о ходе работы в канал ToolChannel.PROGRESS; отсюда они уходят
    клиенту стандартным прогрессом запроса: числа done и total и текст
    отчёта. Тело ничего не сообщило — уведомлений нет; клиент без токена
    прогресса их не получает. Отчёты ждут в очереди, а отправляет их задача,
    заведённая start() внутри запроса: уведомление привязано к запросу, и
    из чужой задачи оно не уходит. Значение прогресса запроса обязано расти:
    отчёт, в котором оно не выросло (узлы связки считают каждый в своих
    единицах), не пересылается.
    """

    POLL_SEC: ClassVar[float] = 1.0
    """Предел ожидания будильника журнала: страховка от пропущенного
    пробуждения."""

    CLOSE_SEC: ClassVar[float] = 2.0

    def __init__(
        self, context: Context, journals: CallJournals, user: str, run: str
    ) -> None:
        self._context = context
        self._journals = journals
        self._user = user
        self._run = run
        self._queue: asyncio.Queue[ProgressReport | None] = asyncio.Queue()
        self._sender: asyncio.Task[None] | None = None
        self._followers: set[asyncio.Task[None]] = set()

    def start(self) -> None:
        self._sender = asyncio.create_task(self._send(), name=f"progress:{self._run}")

    def opened(self, call_id: str, stream: ToolStream) -> None:
        """Наблюдатель CallJournals: журнал вызова открыт — за его каналом
        отчётов следит своя задача."""
        task = asyncio.create_task(
            self._follow(call_id, stream), name=f"progress-follow:{call_id}"
        )
        self._followers.add(task)
        task.add_done_callback(self._followers.discard)

    async def close(self) -> None:
        """Дожидается чтения и отправки всех отчётов: после ответа на вызов
        уведомления теряются без ошибки."""
        followers = list(self._followers)
        if followers:
            done, pending = await asyncio.wait(followers, timeout=self.CLOSE_SEC)
            for task in pending:
                task.cancel()

            for task in pending:
                with contextlib.suppress(asyncio.CancelledError):
                    await task

            for task in done:
                self._logged(task)

        sender = self._sender
        if sender is None:
            return

        self._queue.put_nowait(None)
        await sender

    @staticmethod
    def _logged(task: asyncio.Task[None]) -> None:
        """Сбой чтения отчётов — в лог сервиса: вызов от него не срывается."""
        if task.cancelled():
            return

        error = task.exception()
        if error is None:
            return

        logger.error(
            "progress follower %s failed: %s", task.get_name(), error, exc_info=error
        )

    async def _follow(self, call_id: str, stream: ToolStream) -> None:
        waker = stream.attach_waker()
        offset = 0
        try:
            while True:
                closed = stream.closed
                offset = self._take(call_id, offset)
                if closed:
                    return

                await self._pause(waker)
        finally:
            stream.detach_waker(waker)

    def _take(self, call_id: str, offset: int) -> int:
        """Отчёты канала с байта offset — в очередь; итог — байт, до
        которого канал прочитан."""
        while True:
            piece = self._journals.recorded_slice(
                self._user, self._run, call_id, offset, ToolChannel.PROGRESS
            )
            if piece is None:
                return offset

            if piece.end <= offset:
                return offset

            for line in piece.text.splitlines():
                self._queue.put_nowait(self._report_of(call_id, line))

            offset = piece.end

    @staticmethod
    def _report_of(call_id: str, line: str) -> ProgressReport:
        try:
            return ProgressReport.model_validate_json(line)
        except ValidationError as exc:
            msg = (
                f"progress of call {call_id}: the body wrote a report that does "
                f"not match ProgressReport, got {line!r}: {exc}"
            )
            raise ProgressChannelError(msg) from exc

    async def _pause(self, waker: asyncio.Event) -> None:
        try:
            await asyncio.wait_for(waker.wait(), timeout=self.POLL_SEC)
        except TimeoutError:
            return

        waker.clear()

    async def _send(self) -> None:
        sent = -1.0
        while True:
            report = await self._queue.get()
            if report is None:
                return

            if report.done <= sent:
                continue

            sent = report.done
            await self._context.report_progress(
                progress=report.done, total=report.total, message=report.message
            )


class StreamReadTool(Tool):
    """Операция сервиса stream_read: кусок журнала вызова по границам строк.

    Создаётся сборкой сервера из журналов процесса. Журнал доступен только
    тому, кто вызывал инструмент: ключ журнала — пользователь токена, запуск
    и узел; чужой запуск — отказ вида ServerRefusal.FORBIDDEN.
    Итог — текст окна и его координаты (StreamSlice) в structuredContent;
    по ним клиент стыкует следующее окно. Запуск области операция не
    открывает и в предел запусков не входит.
    """

    NAME: ClassVar[str] = "stream_read"

    DESCRIPTION: ClassVar[str] = (
        "Read a window of the output journal of a tool call:\n"
        "   - run — run id from the result of the call\n"
        "   - node — id of the call\n"
        "   - channel — journal channel\n"
        "   - offset — window from this byte, or before — window ending at this byte"
    )

    _journals: CallJournals = PrivateAttr()
    _subjects: TokenSubjects = PrivateAttr()
    _replies: McpReplies = PrivateAttr()

    def __init__(self, journals: CallJournals, subjects: TokenSubjects) -> None:
        super().__init__(
            name=self.NAME,
            description=self.DESCRIPTION,
            parameters=JournalRead.model_json_schema(),
        )
        self._journals = journals
        self._subjects = subjects
        self._replies = McpReplies()

    async def run(self, arguments: dict[str, object]) -> ToolResult:
        try:
            request = JournalRead.model_validate(arguments)
        except ValidationError as exc:
            return self._refused(
                ServerRefusal.INVALID_REQUEST,
                f"stream_read: the arguments are invalid: {exc}",
            )

        if not JournalChannels.visible(request.channel):
            return self._refused(
                ServerRefusal.INVALID_REQUEST,
                f"stream_read: channel {request.channel.value!r} is not readable",
            )

        user = self._subjects.current().user_key
        owner = self._journals.owner_of(request.run)
        if owner is not None and owner != user:
            return self._refused(
                ServerRefusal.FORBIDDEN,
                f"stream_read: the journal of run {request.run!r} belongs to "
                "another user; expected a run of the caller",
            )

        piece = self._slice(request, user)
        if piece is None:
            return self._refused(
                ServerRefusal.NOT_FOUND,
                f"stream_read: no journal of call {request.node!r} in run "
                f"{request.run!r} on channel {request.channel.value!r}",
            )

        return self._replies.answered(piece.text, piece)

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

    def _refused(self, kind: ServerRefusal, message: str) -> ToolResult:
        failure = ErrorResult(message=message, error_kind=kind)

        return self._replies.refused(self.NAME, failure)


class FileUploadRequest(BaseModel):
    """Аргументы file_upload: имя файла в каталоге вложений области."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(
        min_length=1,
        description="File name as it should appear in the workspace upload dir.",
    )


class FileUploadAddress(BaseModel):
    """Ответ file_upload: чем и куда клиент шлёт тело файла и где файл
    увидят инструменты."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    method: str
    path: str
    workspace_path: str


class FileUploadTool(Tool):
    """Операция сервиса file_upload: адрес, по которому клиент загружает файл.

    Создаётся сервером endpoint'а рядом с FileRoutes. Байты файла вызовом
    MCP не передаются: модель получает адрес и способ отправки, а клиент
    шлёт файл потоком на маршрут файлов тем же токеном входа. Запуска
    области операция не открывает и в предел запусков не входит.
    """

    NAME: ClassVar[str] = "file_upload"

    DESCRIPTION: ClassVar[str] = (
        "Get the address to upload a file into the workspace:\n"
        "   - name — file name in the upload dir of the workspace\n"
        "The file body is sent by the client with HTTP PUT to the returned "
        "path of this server using the same bearer token; tools then read the "
        "file at the returned workspace path"
    )

    _routes: FileRoutes = PrivateAttr()
    _subjects: TokenSubjects = PrivateAttr()
    _scopes: CallScopes = PrivateAttr()
    _replies: McpReplies = PrivateAttr()

    def __init__(self, routes: FileRoutes, subjects: TokenSubjects) -> None:
        super().__init__(
            name=self.NAME,
            description=self.DESCRIPTION,
            parameters=FileUploadRequest.model_json_schema(),
        )
        self._routes = routes
        self._subjects = subjects
        self._scopes = CallScopes()
        self._replies = McpReplies()

    async def run(self, arguments: dict[str, object]) -> ToolResult:
        try:
            request = FileUploadRequest.model_validate(arguments)
            subject = self._subjects.current()
            scope = self._scopes.of(subject)
            key = ObjectKey(
                user_id=subject.user_key, thread_id=scope.id, name=request.name
            )
        except ValidationError as exc:
            return self._refused(
                f"file_upload: the arguments do not name a file: "
                f"{ValidationText.of(exc)}"
            )
        except CallScopeError as exc:
            return self._refused(f"file_upload: {exc}")

        address = self._routes.address(key.thread_id, key.name)
        text = (
            f"upload the file body with HTTP PUT to {address} of this server "
            f"(same bearer token); tools read it at {key.in_workspace()}"
        )
        answer = FileUploadAddress(
            method="PUT", path=address, workspace_path=key.in_workspace()
        )

        return self._replies.answered(text, answer)

    def _refused(self, message: str) -> ToolResult:
        failure = ErrorResult(message=message, error_kind=ServerRefusal.INVALID_REQUEST)

        return self._replies.refused(self.NAME, failure)


class CallContextMiddleware(Middleware):
    """Контекст вызова и запуск области вокруг вызова инструмента.

    Создаётся сборкой сервера из реестра запусков и журналов. Из токена и
    _meta запроса собирает контекст вызова (субъект, область, ключ запуска,
    отмена) и
    держит запуск открытым на время вызова: задачи узлов DAG и тела в
    потоках наследуют контекст, обвязки прав и значений контекста читают его
    как в чате. Отчёты тел о ходе работы уходят клиенту прогрессом запроса
    (CallProgress), идентификатор запуска — в сведениях о вызове итога.
    Клиент закрыл
    соединение — отмена помечается, DAG гаснет. Операции сервиса
    (operations) проходят мимо: запуска у них нет.
    """

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
        self._replies = McpReplies()
        self._scopes = CallScopes()

    async def on_call_tool(
        self,
        context: MiddlewareContext[mt.CallToolRequestParams],
        call_next: CallNext[mt.CallToolRequestParams, ToolResult],
    ) -> ToolResult:
        if context.message.name in self._operations:
            return await call_next(context)

        subject = self._subjects.current()
        try:
            scope = self._scopes.of(subject)
        except CallScopeError as exc:
            failure = ErrorResult(
                message=str(exc), error_kind=ServerRefusal.INVALID_REQUEST
            )

            return self._replies.refused(context.message.name, failure)

        cancellation = RunCancellation()
        run_id = uuid4().hex
        call = CallContext(
            subject=subject,
            scope=scope,
            run_id=run_id,
            initiator=HumanInitiator(via="api"),
            credential=self._subjects.credential(),
            cancellation=cancellation,
        )

        progress = CallProgress(get_context(), self._journals, subject.user_key, run_id)
        progress.start()
        try:
            with (
                self._runs.open(call) as run,
                self._journals.following(run, progress.opened),
            ):
                try:
                    result = await call_next(context)
                except asyncio.CancelledError:
                    cancellation.cancel(StopReason.ABORTED)
                    raise
                except ToolStopped as exc:
                    # запуск остановили при живом клиенте (остановка сервиса):
                    # клиент получает отказ, а не обрыв ответа
                    stopped = ErrorResult(
                        message=(
                            f"tool {context.message.name!r} call was stopped "
                            f"before it finished, reason {cancellation.reason}: {exc}"
                        ),
                        error_kind=ServerRefusal.STOPPED,
                    )
                    result = self._replies.refused(context.message.name, stopped)
        finally:
            # журналы закрыты вместе с запуском: досылаются последние отчёты
            await progress.close()

        return self._replies.stamped(result, run_id)


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

    def __init__(self, limits: RunLimits, operations: frozenset[str]) -> None:
        self._limits = limits
        self._operations = operations
        self._slots = asyncio.Semaphore(limits.max_runs)
        self._running = 0
        self._waiting = 0
        self._replies = McpReplies()

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
            error_kind=ServerRefusal.RUN_LIMIT,
        )

        return self._replies.refused(tool, failure)


class FeatureExtension(ServerExtension):
    """Возможность сервиса как расширение MCP: идентификатор и настройки,
    которые клиент получает при согласовании протокола.

    Создаётся сборкой сервера endpoint'а на каждую возможность (журнал
    вызовов, запечатывание соединений) и регистрируется в сервере fastmcp.
    """

    def __init__(self, identifier: str, settings: Mapping[str, Any]) -> None:
        self.identifier = identifier
        self._settings = dict(settings)

    def settings(self) -> dict[str, Any]:
        return dict(self._settings)


class McpServer:
    """Сборка MCP-сервера одного endpoint'а сервиса исполнения.

    Создаётся сборкой endpoint'ов (McpEndpoints) на endpoint конфига сервиса:
    из реестра инструментов, реестра запусков, журналов, проверяющего
    токены, имени профиля и общего на процесс предела запусков. Инструменты
    endpoint'а — те, что профиль и роли вошедшего разрешают; mcp() отдаёт
    готовый сервер fastmcp.
    """

    NAME: ClassVar[str] = "boba-mcp"

    def __init__(  # noqa: PLR0913 — сервер собирается всеми своими входами
        self,
        registry: ToolRegistry,
        runs: Runs,
        journals: CallJournals,
        auth: AuthProvider,
        profile: str,
        limits: RunLimitMiddleware,
        storage: LocalStorageConfig,
        path: str,
        published: str,
    ) -> None:
        self._name = f"{self.NAME}/{profile}"
        self._auth = auth
        self._subjects = TokenSubjects(profile)
        self._limits = limits
        callers = RouteCallers(self._subjects, profile)
        self._files = FileRoutes(
            StorageFactory.create(storage), callers, path, published
        )
        self._journal_files = JournalRoutes(journals, callers, storage, path, published)
        upload = FileUploadTool(self._files, self._subjects)
        operations: list[Tool] = [upload]
        self._features = self._features_of(registry)
        self._features[FilesFeature.ID] = self._files.feature(upload.name).settings()
        self._journaled = journals.active()
        if self._journaled:
            stream_read = StreamReadTool(journals, self._subjects)
            operations.append(stream_read)
            journal = JournalFeature(
                read=stream_read.name, path=self._journal_files.path()
            )
            self._features[JournalFeature.ID] = journal.settings()

        self._provider = McpToolProvider(
            RoleToolServers(registry), self._subjects, operations
        )
        self._contexts = CallContextMiddleware(
            runs, journals, self._subjects, McpEndpoints.OPERATIONS
        )

    @staticmethod
    def _features_of(registry: ToolRegistry) -> dict[str, dict[str, Any]]:
        """Возможности сервера для объявления при подключении клиента: те,
        что объявляет порт над всеми инструментами реестра (инструмент-связка,
        ключ запечатывания соединений)."""
        declared: dict[str, dict[str, Any]] = {}
        port = registry.server(registry.tools, DirectCalls())
        for feature, settings in port.features().items():
            declared[feature] = dict(settings)

        return declared

    def mcp(self) -> FastMCP:
        server = FastMCP(
            self._name,
            auth=self._auth,
            providers=[self._provider],
            middleware=[self._limits, self._contexts],
            dereference_schemas=False,
        )
        # сбой списка инструментов — ошибка, а не «у вызывающего нет инструментов»
        server.provider_error_strategy = "raise"
        for identifier, settings in self._features.items():
            server.add_extension(FeatureExtension(identifier, settings))

        self._files.install(server)
        if self._journaled:
            self._journal_files.install(server)

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


class EndpointCatalog(ProfileCatalog):
    """Endpoint'ы сервиса как гранты и как каталог профилей входа.

    Создаётся сборкой процесса из секций [mcp.endpoints.<имя>]. Реализация
    ProfileCatalog: вход перечисляет в токене endpoint'ы, выданные ролям
    вошедшего. grants() отдаёт те же endpoint'ы грантами реестра
    инструментов: набор инструментов — у endpoint'а, а любая роль вошедшего
    разрешает любой инструмент — роли здесь решают только доступ к endpoint'у.
    """

    def __init__(self, endpoints: Mapping[str, ProfileGrant]) -> None:
        self._endpoints = dict(endpoints)

    def granted_by_roles(self, user_roles: frozenset[str]) -> frozenset[str]:
        names: set[str] = set()
        for name, endpoint in self._endpoints.items():
            if endpoint.visible_for(user_roles):
                names.add(name)

        return frozenset(names)

    def known(self, name: str) -> bool:
        return name in self._endpoints

    def grants(self) -> ToolGrants:
        any_role = {ToolGrant.WILDCARD: ToolGrant(tools=[ToolGrant.WILDCARD])}

        return ToolGrants(
            roles=any_role, profiles=self._endpoints, check=GrantCheck.STRICT
        )


class McpEndpoints:
    """Endpoint'ы MCP сервиса исполнения: по одному на секцию
    [mcp.endpoints.<имя>].

    Создаётся сборкой процесса. Endpoint — набор инструментов и роли,
    которым он выдан; каждый отвечает отдельным MCP-сервером на пути
    `{base_path}/{имя}`. Токен без области endpoint'а получает 403 от входа
    fastmcp (EndpointTokens). Реестр инструментов, запуски, журналы и предел запусков
    у endpoint'ов общие. app() — приложение ASGI со всеми endpoint'ами,
    маршрутами входа и пробой готовности.
    """

    HEALTH: ClassVar[str] = "/health"
    """Путь пробы готовности: без входа, отдаёт пути endpoint'ов."""

    OPERATIONS: ClassVar[frozenset[str]] = frozenset(
        {StreamReadTool.NAME, FileUploadTool.NAME}
    )
    """Операции сервиса: запуска не открывают и в предел запусков не входят."""

    def __init__(  # noqa: PLR0913 — endpoint'ы собираются всеми входами процесса
        self,
        registry: ToolRegistry,
        runs: Runs,
        journals: CallJournals,
        auth: ServiceAuth,
        limits: RunLimits,
        base_path: str,
        storage: LocalStorageConfig,
    ) -> None:
        # маршруты входа стоят в корне: сервер авторизации — вне endpoint'ов
        self._routes: list[BaseRoute] = list(auth.routes())
        self._endpoints: list[EndpointApp] = []
        slots = RunLimitMiddleware(limits, self.OPERATIONS)
        for profile in sorted(registry.access.profiles()):
            path = f"{base_path}/{profile}"
            provider = auth.endpoint(profile)
            server = McpServer(
                registry,
                runs,
                journals,
                provider,
                profile,
                slots,
                storage,
                path,
                auth.published(),
            )
            served = server.mcp().http_app(path=path, stateless_http=True)
            self._endpoints.append(EndpointApp(path, served, served))
            # метаданные ресурса клиент ищет от корня хоста, а не под endpoint'ом
            self._routes.extend(provider.get_well_known_routes(path))

    def paths(self) -> Sequence[str]:
        paths: list[str] = []
        for endpoint in self._endpoints:
            paths.append(endpoint.path)

        return paths

    def app(self) -> Starlette:
        routes: list[BaseRoute] = [
            Route(self.HEALTH, self._health, methods=["GET"]),
            *self._routes,
            Mount("/", app=self._dispatch),
        ]

        return Starlette(routes=routes, lifespan=self._lifespan)

    async def _health(self, request: Request) -> Response:
        """Проба готовности: процесс слушает, endpoint'ы собраны."""
        return JSONResponse({"endpoints": list(self.paths())})

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
