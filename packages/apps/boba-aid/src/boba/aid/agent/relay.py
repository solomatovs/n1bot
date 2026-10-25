"""Ретранслятор MCP-over-ACP: ответы чата aid на запросы mcp/message агента
серверам, которые чат aid объявил ему записями type "acp", и реестр этих
регистраций (serverId → пользователь, тред, сервер). Сервер профиля
отвечает своим портом McpServers с личностью агента в каждом запросе (его
clientInfo и clientCapabilities из _meta запроса), сервер chat —
собственными инструментами чата прямо в процессе, без своего HTTP-входа.

Ошибки:
RequestError (acp.exceptions) — ошибка доставки MCP-over-ACP:
    UNKNOWN_SERVER — serverId не выдан этим чатом или снят;
    INVALID_PARAMS — requestId уже в работе;
    BACKEND_FAILED — сервер за регистрацией недоступен, ответил не по схеме,
    оборвал поток подписки, имена инструментов сервера chat совпали либо
    вход пользователя вкладки больше не годен, исхода MCP нет.
Запросы приложений MCP Apps (app_call, app_read) ошибок наружу не выпускают:
app_answer отдаёт приложению ошибку JSON-RPC с причиной. Источник вызова
(source) отказывает теми же RequestError: UNKNOWN_SERVER и BACKEND_FAILED.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from collections.abc import (
    Awaitable,
    Callable,
    Collection,
    Iterator,
    Mapping,
    Sequence,
)
from contextlib import nullcontext
from dataclasses import dataclass, replace
from enum import StrEnum
from functools import partial
from typing import Any, ClassVar, Generic, TypeVar
from uuid import UUID, uuid4

import anyio
import mcp_types as mt
from acp.exceptions import RequestError
from acp.experimental.v2 import schema
from fastmcp.utilities.json_schema import compress_schema
from mcp.server.apps import Visibility
from mcp.server.connection import Connection, NotifyOnlyOutbound
from mcp.server.context import ServerRequestContext
from mcp.server.lowlevel import Server
from mcp.server.runner import serve_one
from mcp.shared.dispatcher import CallOptions, DispatchContext
from mcp.shared.exceptions import MCPError, NoBackChannelError
from mcp.shared.inbound import (
    InboundLadderRejection,
    InboundModernRoute,
    classify_inbound_request,
)
from mcp.shared.jsonrpc_dispatcher import progress_token_from_params
from mcp.shared.message import MessageMetadata
from mcp.shared.subscriptions import SUBSCRIPTION_ID_META_KEY, event_to_notification
from mcp.shared.transport_context import TransportContext
from mcp_types.jsonrpc import ErrorData
from mcp_types.version import MODERN_PROTOCOL_VERSIONS
from pydantic import BaseModel, ConfigDict, ValidationError

from boba.aid.canvas.tools import CanvasOpener
from boba.aid.infra.session import LiveSignIn
from boba.aid.rendering.tool import (
    AppMeta,
    AppResource,
    AppSource,
    AppUnread,
    ToolSource,
)
from boba.cancellation import RunCancellation, StopReason
from boba.canvas.canvas import CanvasScope
from boba.connection_broker.sealing import CallSealing, McpServerInfo
from boba.connections.stored import ConnectionStoreError
from boba.core.mcp_names import McpToolNames
from boba.core.validation import ValidationText
from boba.identity.context import (
    CallContext,
    CallContexts,
    HumanInitiator,
    Scope,
    Subject,
)
from boba.identity.errors import AuthenticationError, RefusalError
from boba.identity.run import Runs
from boba.kerberos import KerberosError
from boba.mcp_adapter import McpTool, RoleTools
from boba.mcp_client.client import (
    JournalHeard,
    McpCaller,
    McpClientError,
    McpClientIdentity,
    McpDescription,
    McpServers,
    McpToolPort,
    McpToolServer,
    McpUnansweredError,
    ProgressHeard,
    QuestionHeard,
)
from boba.messaging import JournalOpened, LockToken, MessageBus, MessageBusError
from boba.toolkit.chain import StreamPlanError
from boba.toolkit.dag import DagNode, ToolServer, ToolServersError
from boba.toolkit.failure import InvokeErrorKind
from boba.toolkit.wire import JournalNotice
from boba.toolrun.hosted import DirectCalls
from boba.toolrun.injected import ToolConfigError
from boba.toolrun.registry import ToolRegistry
from boba.transport.acp.protocol import (
    AcpErrorCode,
    BobaExtension,
    ErrorDataKey,
    McpErrorOutcome,
    McpMessage,
    McpOutcome,
    McpResultOutcome,
    ToolSourceReply,
    ToolSourceRequest,
)

__all__ = [
    "AppAction",
    "AppRequest",
    "ChatToolPort",
    "ChatTools",
    "ClientName",
    "HeldCall",
    "McpRegistration",
    "McpRegistrations",
    "McpRelay",
    "RelayExchange",
    "RelayedRequest",
]

logger = logging.getLogger(__name__)

ParamsT = TypeVar("ParamsT", bound=BaseModel)
"""Параметры запроса приложения MCP Apps к чату."""


class AppAction(StrEnum):
    """Действия chainlit, которыми элемент приложения MCP Apps передаёт
    запросы приложения серверной части чата."""

    CALL = "mcp_app_call"
    READ = "mcp_app_read"
    CONTEXT = "mcp_app_context"


class AppRequest(BaseModel, Generic[ParamsT]):
    """Запрос приложения MCP Apps, пришедший действием chainlit: сервер
    профиля, чьё это приложение, вызов инструмента, итог которого рисует
    приложение, и параметры запроса по форме MCP (tools/call,
    resources/read) или MCP Apps (ui/update-model-context). Разбирается в
    обработчике действия, дальше идёт моделью."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    server: str
    call_id: str
    params: ParamsT


class ClientName(StrEnum):
    """Как чат представляется агенту: клиентом в initialize и сервером
    MCP ретранслятора в server/discover."""

    NAME = "boba-aid"


class Handshake(StrEnum):
    """Рукопожатие прежних редакций MCP: метод и параметр версии в нём."""

    METHOD = "initialize"
    VERSION = "protocolVersion"


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
    сервера профиля либо ChatTools.NAME; servers — серверы, которые профиль
    даёт треду: приложение зовёт только их. Контекст вызова от агента
    ретранслятор строит из регистрации (context).
    """

    user_id: UUID
    login: str
    thread_id: str
    profile: str
    session: LiveSignIn
    server: str
    servers: frozenset[str]

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
    ) -> Sequence[schema.AcpMcpServer]:
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


@dataclass(frozen=True)
class RelayedRequest:
    """Запрос mcp/message агента в работе ретранслятора: serverId
    регистрации и личность агента-клиента из _meta запроса.

    Создаёт McpRelay.answer на каждый запрос и отдаёт serve_one состоянием
    запроса: обработчики берут из него регистрацию и личность, с которой
    запрос уходит серверу профиля.
    """

    server_id: str
    client: McpClientIdentity


class ChatToolPort(McpToolPort):
    """Реализация McpToolPort собственными инструментами чата в его процессе
    на один запрос mcp/message.

    Создаёт ChatTools.port из инструментов McpTool пары «роли, профиль»
    вызывающего и контекста вызова регистрации. Карточки и ответы — общего
    адаптера boba.mcp_adapter; вызов идёт под открытым запуском контекста:
    тела читают субъекта из контекста, как в ходе чата. Сервер chat
    объявляет одни инструменты: список их неизменен, ресурсов, промптов,
    подсказок, файлов, хода, журналов и расширений у него нет.
    """

    def __init__(
        self,
        tools: Mapping[str, McpTool],
        context: CallContext,
        runs: Runs,
        info: mt.Implementation,
    ) -> None:
        self._tools = tools
        self._context = context
        self._runs = runs
        self._info = info

    async def described(self, client: McpClientIdentity) -> McpDescription:
        capabilities = mt.ServerCapabilities(
            tools=mt.ToolsCapability(list_changed=False)
        )
        info = self._info.model_dump(mode="json", by_alias=True, exclude_none=True)
        result = mt.DiscoverResult(
            supported_versions=list(MODERN_PROTOCOL_VERSIONS),
            capabilities=capabilities,
            _meta={mt.SERVER_INFO_META_KEY: info},
        )

        return McpDescription(f"server {ChatTools.NAME!r} of the chat", result)

    async def tools(self, client: McpClientIdentity) -> mt.ListToolsResult:
        listed: list[mt.Tool] = []
        for tool in self._tools.values():
            listed.append(tool.to_mcp_tool())

        return mt.ListToolsResult(tools=listed)

    async def call(  # noqa: PLR0913 — вызов несёт приёмники хода, журналов и вопросов
        self,
        client: McpClientIdentity,
        params: mt.CallToolRequestParams,
        card: mt.Tool | None,
        progress: ProgressHeard | None,
        journals: JournalHeard | None,
        asked: QuestionHeard | None,
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
            _meta=result.meta,
        )


class ChatTools(RoleTools[tuple[frozenset[str], str]]):
    """Собственные инструменты чата (connection_list, connection_search) как
    сервер chat, объявленный агенту, на базе RoleTools.

    Создаётся провайдером ретранслятора и живёт в нём рядом с McpServers:
    на запрос регистрации chat ретранслятор берёт порт ChatToolPort по
    контексту вызова. Порт пары «роли, профиль» — инструменты хода чата
    реестра с прямым вызовом тел (DirectCalls): ленты у вызова от агента
    нет, шаг рисует агент по своим уведомлениям. version — версия чата в
    serverInfo сервера chat.
    """

    NAME: ClassVar[str] = "chat"

    def __init__(self, registry: ToolRegistry, runs: Runs, version: str) -> None:
        super().__init__()
        self._registry = registry
        self._runs = runs
        self._calls = DirectCalls()
        self._info = mt.Implementation(name=self.NAME, version=version)

    def port(self, context: CallContext) -> ChatToolPort:
        """Порт инструментов вызывающего context."""
        subject = context.subject
        tools = self.tools((subject.roles, subject.profile))

        return ChatToolPort(tools, context, self._runs, self._info)

    def _server(self, key: tuple[frozenset[str], str]) -> ToolServer:
        roles, profile = key
        tools = self._registry.for_session(roles, profile)

        return self._registry.server(tools, self._calls)


HeldReply = mt.CallToolResult | mt.InputRequiredResult
"""Ответ на tools/call агента: итог вызова либо просьба о вводе."""


@dataclass(frozen=True)
class ServerQuestion:
    """Встречный вопрос пользователю (elicitation/create), который сервер
    прежней редакции задал посреди вызова, и ожидание ответа на него."""

    params: mt.ElicitRequestParams
    answer: asyncio.Future[mt.ElicitResult]


class HeldCall:
    """Вызов инструмента сервера профиля, который ретранслятор ведёт своей
    задачей и держит открытым, пока пользователь отвечает на встречный
    вопрос сервера прежней редакции.

    Сервер 2025-11-25 спрашивает пользователя запросом elicitation/create
    посреди tools/call и ждёт ответа, а агенту ретранслятор — сервер
    2026-07-28, у которого вопрос идёт через MRTR. Создаёт McpRelay на каждый
    tools/call модели; порт отдаёт вопросы сервера в ask. next() ждёт итог
    вызова либо вопрос: вопрос становится InputRequiredResult с requestState
    этого вызова (state), и McpRelay держит вызов по нему до повтора агента;
    ответ повтора уходит серверу (answered), и вызов снова ждёт итог или
    следующий вопрос. Отмена (cancel) отвечает серверу cancel на вопросы,
    которые ждут ответа, а без вопросов отменяет сам вызов. Когда задача
    вызова кончается, ended убирает вызов, который ждал агента, и говорит,
    ждал ли он: итог такого и отменённого вызова читать некому, он уходит в
    лог.
    """

    def __init__(
        self,
        server_id: str,
        tool: str,
        state: str,
        send: Callable[[ProgressHeard, QuestionHeard], Awaitable[HeldReply]],
        ended: Callable[[HeldCall], bool],
    ) -> None:
        self.server_id = server_id
        self.tool = tool
        self.state = state
        self._arrived: asyncio.Queue[ServerQuestion] = asyncio.Queue()
        self._waiting: dict[str, ServerQuestion] = {}
        self._asked = 0
        self._closed = False
        self._dropped = False
        self._progress: ProgressHeard | None = None
        self._ended = ended
        self._task = asyncio.ensure_future(send(self.progressed, self.ask))
        self._task.add_done_callback(self._finished)

    @property
    def asking(self) -> bool:
        """Вопросы сервера ждут ответа агента."""
        return bool(self._waiting)

    def follow(self, progress: ProgressHeard | None) -> None:
        """Ход вызова уходит запросу агента, который ждёт его сейчас."""
        self._progress = progress

    async def progressed(self, params: mt.ProgressNotificationParams) -> None:
        if self._progress is None:
            return

        await self._progress(params)

    async def ask(self, params: mt.ElicitRequestParams) -> mt.ElicitResult:
        """Вопрос сервера ждёт ответа агента; у отменённого вызова — cancel."""
        if self._closed:
            return mt.ElicitResult(action="cancel")

        answer = asyncio.get_running_loop().create_future()
        self._arrived.put_nowait(ServerQuestion(params=params, answer=answer))

        return await answer

    async def next(self) -> HeldReply:
        """Итог вызова либо вопросы сервера просьбой о вводе с requestState
        вызова.

        Ошибки: те же, что у McpToolPort.call.
        """
        arrived = asyncio.ensure_future(self._arrived.get())
        try:
            await asyncio.wait(
                (self._task, arrived), return_when=asyncio.FIRST_COMPLETED
            )
        finally:
            arrived.cancel()

        if self._task.done():
            return self._task.result()

        self._waited(arrived.result())
        while not self._arrived.empty():
            self._waited(self._arrived.get_nowait())

        requests: dict[str, mt.InputRequest] = {}
        for key, question in self._waiting.items():
            requests[key] = mt.ElicitRequest(params=question.params)

        return mt.InputRequiredResult(input_requests=requests, request_state=self.state)

    def answered(self, responses: Mapping[str, mt.InputResponse] | None) -> None:
        """Ответы агента на вопросы сервера по их ключам — серверу.

        Ошибки:
        MCPError — ответа на какой-то вопрос нет либо он не ответ на форму.
        """
        given: Mapping[str, mt.InputResponse] = {}
        if responses is not None:
            given = responses

        answers: dict[str, mt.ElicitResult] = {}
        for key in self._waiting:
            response = given.get(key)
            if not isinstance(response, mt.ElicitResult):
                msg = (
                    f"tools/call of {self.tool!r} retried with requestState "
                    f"{self.state!r}: inputResponses expected an elicitation "
                    f"result under {key!r} for each of {sorted(self._waiting)}, "
                    f"got {response!r}"
                )
                raise MCPError(code=mt.INVALID_PARAMS, message=msg)

            answers[key] = response

        for key, answer in answers.items():
            self._settle(self._waiting[key], answer)

        self._waiting.clear()

    def cancel(self) -> None:
        """Вызов отменён: вопросы, которые ждут ответа, получают cancel и
        сервер доделывает вызов сам; без вопросов отменяется сам вызов."""
        self._closed = True
        self._dropped = True
        while not self._arrived.empty():
            self._waited(self._arrived.get_nowait())

        if not self._waiting:
            self._task.cancel()
            return

        cancelled = mt.ElicitResult(action="cancel")
        for question in self._waiting.values():
            self._settle(question, cancelled)

        self._waiting.clear()

    def _waited(self, question: ServerQuestion) -> None:
        self._asked += 1
        self._waiting[str(self._asked)] = question

    @staticmethod
    def _settle(question: ServerQuestion, answer: mt.ElicitResult) -> None:
        # вопрос, который сервер уже снял, ответа не ждёт
        if question.answer.done():
            return

        question.answer.set_result(answer)

    def _finished(self, task: asyncio.Future[HeldReply]) -> None:
        self._closed = True
        cancelled = mt.ElicitResult(action="cancel")
        for question in self._waiting.values():
            self._settle(question, cancelled)

        self._waiting.clear()
        if task.cancelled():
            return

        error = task.exception()
        waited = self._ended(self)
        unread = waited or self._dropped
        if not unread:
            return

        if error is not None:
            logger.warning(
                "mcp relay: tools/call of %r (requestState %s) ended with %s while "
                "nobody waited for it, the question was stopped or its answer did "
                "not come in time; a retry of the agent finds no call: %s",
                self.tool,
                self.state,
                type(error).__name__,
                error,
            )
            return

        logger.info(
            "mcp relay: tools/call of %r (requestState %s) was answered while "
            "nobody waited for it, the question was stopped or its answer did not "
            "come in time; the answer is dropped",
            self.tool,
            self.state,
        )


class McpRelay:
    """Ретранслятор mcp/message: исполняет запросы MCP агента серверами,
    которые чат aid объявил его процессу.

    Создаётся провайдером процесса, один на инстанс; процесс агента
    (AgentProcess) отдаёт ему каждый запрос вместе с отправкой уведомлений
    своего соединения. По serverId запроса McpRegistrations даёт
    регистрацию (пользователь, тред, имя сервера), а она — контекст вызова.
    Агенту ретранслятор — сервер редакции 2026-07-28, за ним — серверы
    профиля любой редакции: различие эпох поглощает клиент McpServers.
    Состояния между запросами у ретранслятора нет: описание сервера и список
    инструментов он берёт у порта через его кэш ответов и отдаёт агенту со
    сроком свежести и областью кэша сервера. Исключение требует прежняя
    редакция: вопрос пользователю сервер 2025-11-25 задаёт встречным
    запросом посреди вызова, и ретранслятор держит такой вызов открытым
    (HeldCall), а агенту отвечает просьбой о вводе, как сервер 2026-07-28;
    повтор агента с её requestState отдаёт ответ тому же вызову. Вызов
    держится, пока идёт, его срок — call_timeout_sec сервера.
    Конверт и версию запроса проверяет SDK mcp (classify_inbound_request),
    на initialize прежних редакций ретранслятор отвечает отказом с перечнем
    своих версий, неизвестные методы отвечает lowlevel Server SDK;
    обработчики здесь — все запросы редакции 2026-07-28, кроме устаревших.
    server/discover отдаёт возможности, инструкции и serverInfo сервера за
    регистрацией;
    сервер профиля зовётся портом McpServers от имени пользователя вкладки
    с личностью агента (RelayedRequest), сервер chat — собственными
    инструментами чата в процессе (ChatTools) и знает только инструменты.
    Ресурсы, шаблоны, промпты, подсказки и потоки
    subscriptions/listen идут серверу профиля как есть, вместе с просьбами
    о вводе (input_required) в ответах; ход запроса уходит агенту
    уведомлением notifications/progress под его токеном. Логи сервера
    (notifications/message, устаревшие в 2026-07-28) ретранслятор не
    несёт и возможность logging агенту не объявляет. Соединения в
    аргументах запечатывает CallSealing в контексте вызова регистрации, у
    инструмента со вложенными вызовами (расширение NestedCallsFeature
    сервера) — и в аргументах каждого узла по схеме его инструмента.
    Журналы, которые вызов открыл на сервере, ретранслятор называет ленте
    треда сообщением JournalOpened шины (bus). Ответ на вызов уходит как
    есть, кроме файлов, которые сервер просит показать сразу: их открывает
    панель канваса треда (CanvasOpener.reply), и отказ показа — диаграмма не
    отрисовалась — становится isError того же вызова (8.2). У сервера,
    объявившего расширение области вызова (ScopeFeature), параметры с его
    пометкой агент в списке не видит: на вызове ретранслятор ставит в них
    тред регистрации; сервер без расширения показывает их агенту как есть.
    Инструменты только для интерфейса (_meta.ui.visibility без "model") и
    инструменты, которые без задачи не исполняются (execution.taskSupport
    "required"), агенту не видны и не вызываются. Приложения MCP Apps
    серверов с секцией apps зовут
    инструменты с "app" в visibility (app_call) и читают ресурсы сервера
    (app_read) тем же путём от имени пользователя вкладки и от личности
    чата; их ответ приложение получает как есть, файлы показа открывает
    только вызов модели.
    """

    TASK_ONLY: ClassVar[str] = "required"
    """execution.taskSupport инструмента, который без задачи не исполняется."""
    HELD: ClassVar[str] = "boba-aid/held/"
    """Начало requestState вызова, который ретранслятор держит открытым."""
    SIGN_IN_EXPIRED: ClassVar[str] = "the sign-in of this chat is no longer valid"
    """Короткая причина агенту, когда вход пользователя вкладки больше не годен."""

    def __init__(  # noqa: PLR0913 — ретранслятор собирается всеми входами процесса
        self,
        registrations: McpRegistrations,
        servers: McpServers,
        sealing: CallSealing,
        contexts: CallContexts,
        chat: ChatTools,
        canvas: CanvasOpener,
        bus: MessageBus,
        version: str,
    ) -> None:
        self._registrations = registrations
        self._servers = servers
        self._sealing = sealing
        self._contexts = contexts
        self._chat = chat
        self._canvas = canvas
        self._bus = bus
        self._names = McpToolNames()
        self._apps = AppMeta()
        self._active: set[tuple[str, str]] = set()
        self._held: dict[str, HeldCall] = {}
        """Вызовы серверов прежней редакции, чьи вопросы ждут повтора агента,
        по requestState."""
        # состояние запроса ретранслятор отдаёт serve_one сам: lifespan
        # сервера здесь не запускается и только задаёт его тип
        idle = RelayedRequest(server_id="", client=McpServers.OWN)
        self._server: Server[RelayedRequest] = Server(
            ClientName.NAME.value,
            version=version,
            lifespan=lambda _server: nullcontext(idle),
            on_list_tools=self._list_tools,
            on_call_tool=self._call_tool,
            on_list_resources=self._list_resources,
            on_list_resource_templates=self._list_resource_templates,
            on_read_resource=self._read_resource,
            on_subscriptions_listen=self._listen,
            on_list_prompts=self._list_prompts,
            on_get_prompt=self._get_prompt,
            on_completion=self._complete,
        )
        self._server.add_request_handler(
            "server/discover", mt.RequestParams, self._discover
        )

    def _brief(
        self, failure: McpClientError | AuthenticationError
    ) -> Mapping[str, str] | None:
        """`data` отказа агенту: короткая причина для человека, если она
        известна; без неё — None, полный текст несёт сообщение отказа."""
        if isinstance(failure, McpUnansweredError):
            return {ErrorDataKey.DETAILS.value: failure.brief}

        if isinstance(failure, AuthenticationError):
            return {ErrorDataKey.DETAILS.value: self.SIGN_IN_EXPIRED}

        return None

    async def answer(
        self, message: McpMessage, notify: Callable[[McpMessage], Awaitable[None]]
    ) -> McpOutcome:
        """Исход запроса mcp/message; notify — уведомления агенту по ходу.

        Ошибки:
        RequestError — нет регистрации serverId, requestId уже в работе либо
            сервер за регистрацией не дал исхода.
        """
        registration = self._registrations.of(message.server_id)
        active = (message.server_id, message.request_id)
        if active in self._active:
            msg = (
                f"mcp/message {message.method!r} to server {message.server_id!r}: "
                f"requestId {message.request_id!r} is already in work, expected "
                "a new requestId for every request"
            )
            raise RequestError(AcpErrorCode.INVALID_PARAMS, msg)

        handshake = self._handshake_refused(message)
        if handshake is not None:
            return handshake

        envelope = {"method": message.method, "params": message.params}
        route = classify_inbound_request(envelope)
        if isinstance(route, InboundLadderRejection):
            error = ErrorData(code=route.code, message=route.message, data=route.data)
            return McpErrorOutcome(error=error)

        try:
            client = self._client_of(route)
        except ValidationError as exc:
            text = (
                f"mcp/message {message.method!r} to server {message.server_id!r}: "
                f"clientInfo and clientCapabilities in _meta are off the schema: "
                f"{ValidationText.of(exc)}"
            )
            return McpErrorOutcome(
                error=ErrorData(code=mt.INVALID_PARAMS, message=text)
            )

        exchange = RelayExchange(message, notify)
        connection = Connection.from_envelope(
            route.protocol_version,
            route.client_info,
            route.client_capabilities,
            outbound=NotifyOnlyOutbound(exchange),
        )
        request = RelayedRequest(server_id=message.server_id, client=client)
        self._active.add(active)
        try:
            result = await serve_one(
                self._server,
                exchange,
                message.method,
                message.params,
                connection=connection,
                lifespan_state=request,
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
            msg = f"{message.method} to MCP server {registration.server!r}: {exc}"
            raise RequestError(
                AcpErrorCode.BACKEND_FAILED, msg, self._brief(exc)
            ) from exc
        except ToolServersError as exc:
            msg = (
                f"mcp/message {message.method!r} to server {message.server_id!r}: "
                f"the tools of the server cannot be offered, expected every tool "
                f"name once: {exc}"
            )
            raise RequestError(AcpErrorCode.BACKEND_FAILED, msg) from exc
        finally:
            self._active.discard(active)

        return McpResultOutcome(result=result)

    def _handshake_refused(self, message: McpMessage) -> McpErrorOutcome | None:
        """Отказ на initialize прежних редакций с перечнем версий
        ретранслятора; None — запрос не initialize."""
        if message.method != Handshake.METHOD:
            return None

        requested = ""
        if message.params is not None:
            requested = str(message.params.get(Handshake.VERSION, ""))

        supported = list(MODERN_PROTOCOL_VERSIONS)
        data = mt.UnsupportedProtocolVersionErrorData(
            supported=supported, requested=requested
        )
        text = (
            f"mcp/message {message.method!r} to server {message.server_id!r}: "
            f"the relay has no initialize handshake and speaks only {supported} "
            f"with the version in the _meta of every request, got "
            f"protocolVersion {requested!r}"
        )
        error = ErrorData(
            code=mt.UNSUPPORTED_PROTOCOL_VERSION,
            message=text,
            data=data.model_dump(mode="json"),
        )

        return McpErrorOutcome(error=error)

    def _client_of(self, route: InboundModernRoute) -> McpClientIdentity:
        """Личность агента из _meta запроса; без clientInfo (его шлют по
        SHOULD) запрос идёт серверу с именем чата.

        Ошибки:
        ValidationError — clientInfo или clientCapabilities не по схеме.
        """
        capabilities = mt.ClientCapabilities.model_validate(route.client_capabilities)
        info = McpServers.OWN.info
        if route.client_info is not None:
            info = mt.Implementation.model_validate(route.client_info)

        return McpClientIdentity(info=info, capabilities=capabilities)

    async def _discover(
        self, ctx: ServerRequestContext[RelayedRequest], params: mt.RequestParams
    ) -> mt.DiscoverResult:
        """server/discover сервера за регистрацией: его возможности,
        расширения, инструкции, serverInfo, оставшийся срок свежести и
        область кэша; версии и logging — свои: ретранслятор говорит на
        2026-07-28 и логов сервера не несёт."""
        request = ctx.lifespan_context
        port = await self._port_of(request)
        described = await port.described(request.client)
        discovered = described.result
        capabilities = discovered.capabilities.model_copy(update={"logging": None})

        return discovered.model_copy(
            update={
                "supported_versions": list(MODERN_PROTOCOL_VERSIONS),
                "capabilities": capabilities,
            }
        )

    async def _list_tools(
        self,
        ctx: ServerRequestContext[RelayedRequest],
        params: mt.PaginatedRequestParams | None,
    ) -> mt.ListToolsResult:
        """Инструменты сервера для модели из списка порта со сроком
        свежести и областью кэша сервера; параметры области вызова сервера с
        расширением ScopeFeature в схеме спрятаны."""
        request = ctx.lifespan_context
        registration = self._registrations.of(request.server_id)
        port = await self._port_of(request)
        listed = await port.tools(request.client)
        described = await port.described(request.client)
        scoping = described.scope()

        tools: list[mt.Tool] = []
        for tool in self._offered(listed.tools, registration.server, "model"):
            hidden: list[str] = []
            if scoping is not None:
                hidden = list(scoping.params(tool.input_schema))

            schema = compress_schema(dict(tool.input_schema), prune_params=hidden)
            tools.append(tool.model_copy(update={"input_schema": schema}))

        return mt.ListToolsResult(
            tools=tools,
            ttl_ms=listed.ttl_ms,
            cache_scope=listed.cache_scope,
            _meta=listed.meta,
        )

    async def _call_tool(
        self,
        ctx: ServerRequestContext[RelayedRequest],
        params: mt.CallToolRequestParams,
    ) -> mt.CallToolResult | mt.InputRequiredResult:
        """Вызов инструмента для модели; ход вызова уходит агенту, журналы,
        открытые вызовом на сервере, — ленте треда, файлы, которые сервер
        просит показать сразу, открывает панель треда (CanvasOpener.reply).

        Ошибки:
        MCPError — инструмента у сервера нет либо он не для модели.
        """
        request = ctx.lifespan_context
        registration = self._registrations.of(request.server_id)
        context = registration.context()

        forwarded: ProgressHeard | None = None
        journals: JournalHeard | None = None
        token = progress_token_from_params(ctx.params)
        if token is not None:
            forwarded = partial(self._forwarded, ctx, token)
            journals = partial(self._journaled, registration, str(token))

        held = self._resumed(request, registration, params)
        if held is None:
            port = await self._port(registration, context)
            send = partial(
                self._called,
                registration,
                context,
                port,
                request.client,
                params,
                "model",
                journals,
            )
            state = f"{self.HELD}{secrets.token_urlsafe(McpRegistrations.KEY_BYTES)}"
            held = HeldCall(request.server_id, params.name, state, send, self._ended)

        held.follow(forwarded)
        reply = await self._waited(held)
        if not isinstance(reply, mt.CallToolResult):
            return reply

        if registration.server == ChatTools.NAME:
            return reply

        subject = context.subject
        user = McpCaller(login=subject.login, roles=subject.roles)
        workspace = await self._servers.workspace(registration.server, user)
        if workspace is None:
            return reply

        scope = CanvasScope(
            user_id=str(registration.user_id),
            scope_id=registration.thread_id,
            workspace=workspace,
        )

        return await self._canvas.reply(scope, reply)

    def _resumed(
        self,
        request: RelayedRequest,
        registration: McpRegistration,
        params: mt.CallToolRequestParams,
    ) -> HeldCall | None:
        """Вызов, который ждёт ответа на вопрос, — повтору агента с его
        requestState, с ответами повтора; None — повтор не к вызову
        ретранслятора, а requestState сервера, если есть, уходит ему.

        Ошибки:
        MCPError — requestState ретранслятора не называет вызов, который
            ждёт этого агента, либо ответы не к его вопросам.
        """
        state = params.request_state
        if state is None:
            return None

        if not state.startswith(self.HELD):
            return None

        held = self._held.get(state)
        if held is None:
            msg = (
                f"tools/call of {params.name!r} on server {registration.server!r}: "
                f"requestState {state!r} names no call the relay holds open for "
                "an answer of the user; the call to the server ended before the "
                "answer came: it was stopped or ran out of call_timeout_sec of "
                f"[mcp.servers.{registration.server}].limits; call the tool again"
            )
            raise MCPError(code=mt.INVALID_PARAMS, message=msg)

        if held.server_id != request.server_id:
            msg = (
                f"tools/call of {params.name!r} on server {registration.server!r}: "
                f"requestState {state!r} belongs to a call of another server "
                "registration; retry a call with the requestState it got"
            )
            raise MCPError(code=mt.INVALID_PARAMS, message=msg)

        if held.tool != params.name:
            msg = (
                f"tools/call of {params.name!r} on server {registration.server!r}: "
                f"requestState {state!r} belongs to a call of {held.tool!r}; "
                "retry the original request with the same tool"
            )
            raise MCPError(code=mt.INVALID_PARAMS, message=msg)

        del self._held[state]
        try:
            held.answered(params.input_responses)
        except MCPError:
            held.cancel()
            raise

        return held

    async def _waited(self, held: HeldCall) -> HeldReply:
        """Итог вызова либо вопросы сервера; вызов с вопросами ждёт повтора
        агента, отменённый запрос агента отменяет и вызов."""
        try:
            reply = await held.next()
        except asyncio.CancelledError:
            held.cancel()
            raise

        if held.asking:
            self._held[held.state] = held

        return reply

    def stopped(self, server_ids: Collection[str]) -> None:
        """Ход агента с серверами server_ids остановлен: его вызовы, открытые
        для вопросов, отменены — сервер получает cancel."""
        for state, held in list(self._held.items()):
            if held.server_id not in server_ids:
                continue

            del self._held[state]
            held.cancel()

    def _ended(self, held: HeldCall) -> bool:
        """Вызов кончился: если он ждал агента, он убран — True."""
        waiting = self._held.get(held.state)
        if waiting is not held:
            return False

        del self._held[held.state]

        return True

    async def _list_resources(
        self,
        ctx: ServerRequestContext[RelayedRequest],
        params: mt.PaginatedRequestParams | None,
    ) -> mt.ListResourcesResult:
        request = ctx.lifespan_context
        server = await self._server_of(request, "resources/list")

        return await server.list_resources(request.client, params)

    async def _list_resource_templates(
        self,
        ctx: ServerRequestContext[RelayedRequest],
        params: mt.PaginatedRequestParams | None,
    ) -> mt.ListResourceTemplatesResult:
        request = ctx.lifespan_context
        server = await self._server_of(request, "resources/templates/list")

        return await server.list_resource_templates(request.client, params)

    async def _read_resource(
        self,
        ctx: ServerRequestContext[RelayedRequest],
        params: mt.ReadResourceRequestParams,
    ) -> mt.ReadResourceResult | mt.InputRequiredResult:
        request = ctx.lifespan_context
        server = await self._server_of(request, "resources/read")

        return await server.read_resource(
            request.client, params, self._progress_of(ctx)
        )

    async def _list_prompts(
        self,
        ctx: ServerRequestContext[RelayedRequest],
        params: mt.PaginatedRequestParams | None,
    ) -> mt.ListPromptsResult:
        request = ctx.lifespan_context
        server = await self._server_of(request, "prompts/list")

        return await server.list_prompts(request.client, params)

    async def _get_prompt(
        self,
        ctx: ServerRequestContext[RelayedRequest],
        params: mt.GetPromptRequestParams,
    ) -> mt.GetPromptResult | mt.InputRequiredResult:
        request = ctx.lifespan_context
        server = await self._server_of(request, "prompts/get")

        return await server.get_prompt(request.client, params, self._progress_of(ctx))

    async def _complete(
        self,
        ctx: ServerRequestContext[RelayedRequest],
        params: mt.CompleteRequestParams,
    ) -> mt.CompleteResult:
        request = ctx.lifespan_context
        server = await self._server_of(request, "completion/complete")

        return await server.complete(request.client, params)

    async def _listen(
        self,
        ctx: ServerRequestContext[RelayedRequest],
        params: mt.SubscriptionsListenRequestParams,
    ) -> mt.SubscriptionsListenResult:
        """subscriptions/listen агента — потоком к серверу профиля с тем же
        набором: подтверждение несёт набор, который подтвердил сервер,
        события идут агенту как есть, штатный конец потока у сервера —
        штатный конец потока агента. Отмена запроса агентом закрывает поток
        к серверу.

        Ошибки:
        MCPError — сервер chat потоков не держит либо сервер профиля отказал
            в подписке.
        McpClientError — сервер недоступен или оборвал поток.
        """
        request = ctx.lifespan_context
        server = await self._server_of(request, "subscriptions/listen")
        meta: dict[str, Any] = {SUBSCRIPTION_ID_META_KEY: ctx.request_id}
        async with server.listen(request.client, params.notifications) as subscription:
            acknowledged = mt.SubscriptionsAcknowledgedNotificationParams(
                notifications=subscription.honored, _meta=meta
            )
            await ctx.session.send_notification(
                mt.SubscriptionsAcknowledgedNotification(params=acknowledged),
                related_request_id=ctx.request_id,
            )
            async for event in subscription:
                await ctx.session.send_notification(
                    event_to_notification(event, meta),
                    related_request_id=ctx.request_id,
                )

        return mt.SubscriptionsListenResult(_meta=meta)

    async def app_call(
        self, registration: McpRegistration, params: mt.CallToolRequestParams
    ) -> mt.CallToolResult:
        """tools/call приложения MCP Apps: только инструменты своего сервера
        с "app" в visibility. Просьба сервера о вводе приложению не отдаётся —
        итог isError с причиной.

        Ошибки:
        MCPError — сервер не рисует приложений либо инструмента у него нет или
            он не для приложения.
        McpClientError — сервер недоступен.
        AuthenticationError — вход пользователя вкладки больше не годен.
        """
        context = registration.context()
        port = await self._app_server(registration, context)
        result = await self._called(
            registration, context, port, McpServers.OWN, params, "app", None, None, None
        )
        if isinstance(result, mt.CallToolResult):
            return result

        full = self._names.full(registration.server, params.name)
        reason = (
            f"{full} asked the app for more input; an app call takes no input "
            "requests, call the tool from the chat instead"
        )

        return self._refused(reason)

    async def app_read(
        self, registration: McpRegistration, params: mt.ReadResourceRequestParams
    ) -> mt.ReadResourceResult:
        """resources/read приложения MCP Apps: ресурс своего сервера.

        Ошибки:
        MCPError — сервер не рисует приложений, ответил ошибкой MCP либо
            вместо ресурса попросил ввода.
        McpClientError — сервер недоступен.
        AuthenticationError — вход пользователя вкладки больше не годен.
        """
        context = registration.context()
        port = await self._app_server(registration, context)
        read = await port.read_resource(McpServers.OWN, params, None)
        if isinstance(read, mt.ReadResourceResult):
            return read

        msg = (
            f"mcp server {registration.server!r} asked the app for more input "
            f"instead of resource {params.uri}; an app read takes no input requests"
        )
        raise MCPError(code=mt.INVALID_PARAMS, message=msg)

    async def app_answer(self, request: Awaitable[BaseModel]) -> dict[str, Any]:
        """Ответ приложению на его запрос: итог по форме MCP либо отказ
        app_refusal с причиной — приложение получит ошибку JSON-RPC, а не
        тишину."""
        try:
            result = await request
        except MCPError as exc:
            logger.warning("mcp app request refused: %s", exc.error.message)
            return self.app_refusal(exc.error)
        except (McpClientError, AuthenticationError) as exc:
            logger.warning("mcp app request failed: %s", exc)
            error = ErrorData(code=mt.INTERNAL_ERROR, message=str(exc))
            return self.app_refusal(error)

        return result.model_dump(mode="json", by_alias=True, exclude_none=True)

    async def source(self, request: ToolSourceRequest) -> ToolSourceReply:
        """Источник вызова инструмента для истории агента (запрос
        `_boba/tool_source`): то, по чему лента нарисует вызов, не спрашивая
        сервер, — карточка инструмента и описание сервера из ответов порта
        (от личности чата, как их читает лента), карточки инструментов узлов
        вложенного вызова и ресурс приложения MCP Apps у сервера с секцией
        apps. Ресурс, который сервер не отдал, ложится причиной: вызов
        исполняется и без него.

        Ошибки:
        RequestError — serverId не выдан этим чатом либо снят
            (UNKNOWN_SERVER); сервер недоступен, отказал, не знает
            инструмента или вход пользователя вкладки больше не годен
            (BACKEND_FAILED).
        """
        registration = self._registrations.of(request.server_id)
        what = (
            f"{BobaExtension.TOOL_SOURCE.value} of call {request.tool_call_id} of "
            f"tool {request.name!r} on MCP server {registration.server!r}"
        )
        try:
            source = await self._source(registration, request)
        except MCPError as exc:
            msg = f"{what}: the server refused: {exc.error.message}"
            raise RequestError(AcpErrorCode.BACKEND_FAILED, msg) from exc
        except (McpClientError, AuthenticationError) as exc:
            msg = f"{what}: {exc}"
            raise RequestError(
                AcpErrorCode.BACKEND_FAILED, msg, self._brief(exc)
            ) from exc

        dumped = source.model_dump(mode="json", by_alias=True, exclude_none=True)

        return ToolSourceReply(source=dumped)

    async def _source(
        self, registration: McpRegistration, request: ToolSourceRequest
    ) -> ToolSource:
        """Ошибки:
        MCPError — сервер отказал, не знает инструмента или _meta.ui его
            карточки не по форме MCP Apps.
        McpClientError — сервер недоступен.
        AuthenticationError — вход пользователя вкладки больше не годен.
        """
        context = registration.context()
        port = await self._port(registration, context)
        listed = await port.tools(McpServers.OWN)
        described = await port.described(McpServers.OWN)
        cards = self._named(iter(listed.tools))
        card = cards.get(request.name)
        if card is None:
            msg = (
                f"mcp server {registration.server!r} lists no tool "
                f"{request.name!r}; its tools are {sorted(cards)}"
            )
            raise MCPError(code=mt.INVALID_PARAMS, message=msg)

        nodes = list(self._node_cards(described, request, cards))
        app = await self._app_resource(registration, card)

        return ToolSource(
            server=registration.server,
            tool=card,
            description=described.result,
            nodes=nodes,
            app=app,
        )

    @staticmethod
    def _node_cards(
        described: McpDescription,
        request: ToolSourceRequest,
        cards: Mapping[str, mt.Tool],
    ) -> Iterator[mt.Tool]:
        """Карточки инструментов, которые зовут узлы вызова, если сервер
        объявил инструмент несущим вложенные вызовы; вызов не по форме узлов
        лента рисует одним шагом — карточек нет."""
        nested = described.nested_calls()
        if nested is None:
            return

        if nested.tool != request.name:
            return

        try:
            calls = nested.calls(request.arguments)
        except StreamPlanError as exc:
            logger.info(
                "mcp relay: call %s has no node cards: %s", request.tool_call_id, exc
            )
            return

        seen: set[str] = set()
        for call in calls:
            if call.tool in seen:
                continue

            seen.add(call.tool)
            if card := cards.get(call.tool):
                yield card

    async def _app_resource(
        self, registration: McpRegistration, card: mt.Tool
    ) -> AppSource | None:
        """Ресурс приложения инструмента card на момент вызова; None — чат не
        рисует приложений сервера либо у инструмента приложения нет.

        Ошибки:
        MCPError — _meta.ui карточки не по форме MCP Apps.
        """
        if self._servers.apps(registration.server) is None:
            return None

        try:
            config = self._apps.of(card)
        except ValidationError as exc:
            msg = (
                f"mcp server {registration.server!r}: tool {card.name!r} carries "
                f"_meta.ui that is not an MCP Apps tool config: "
                f"{ValidationText.of(exc)}"
            )
            raise MCPError(code=mt.INTERNAL_ERROR, message=msg) from exc

        if config is None:
            return None

        uri = config.resource_uri
        if uri is None:
            return None

        params = mt.ReadResourceRequestParams(uri=uri)
        what = f"resources/read of {uri} on MCP server {registration.server!r}"
        try:
            read = await self.app_read(registration, params)
        except MCPError as exc:
            return AppUnread(error=f"{what} was refused: {exc.error.message}")
        except (McpClientError, AuthenticationError) as exc:
            return AppUnread(error=f"{what} failed: {exc}")

        return AppResource(resource=read)

    def app_refusal(self, error: ErrorData) -> dict[str, Any]:
        """Отказ на запрос приложения в ответе действия: элемент приложения
        отдаёт его мосту ошибкой JSON-RPC."""
        return {"error": error.model_dump(mode="json", exclude_none=True)}

    async def _called(  # noqa: PLR0913 — вызов собирается регистрацией, портом и стороной
        self,
        registration: McpRegistration,
        context: CallContext,
        port: McpToolPort,
        client: McpClientIdentity,
        params: mt.CallToolRequestParams,
        audience: Visibility,
        journals: JournalHeard | None,
        forwarded: ProgressHeard | None,
        questions: QuestionHeard | None,
    ) -> mt.CallToolResult | mt.InputRequiredResult:
        """Вызов инструмента клиента client, видимого стороне audience, по
        списку и описанию сервера из кэша порта: соединения запечатаны, у
        вызова со вложенными вызовами — и в узлах, тред регистрации — в
        параметрах с пометкой области, которую сервер объявил расширением
        ScopeFeature, отказ запечатывания — isError с причиной; вопросы
        сервера прежней редакции — questions.

        Ошибки:
        MCPError — инструмента у сервера нет либо он не для audience.
        """
        listed = await port.tools(client)
        offered = self._named(
            self._offered(listed.tools, registration.server, audience)
        )
        tool = offered.get(params.name)
        if tool is None:
            msg = (
                f"mcp server {registration.server!r} offers the {audience} no tool "
                f"{params.name!r}; expected one of {sorted(offered)}"
            )
            raise MCPError(code=mt.INVALID_PARAMS, message=msg)

        asked: dict[str, Any] = {}
        if params.arguments is not None:
            asked = dict(params.arguments)

        described = await port.described(client)
        if scoping := described.scope():
            for name in scoping.params(tool.input_schema):
                asked[name] = registration.thread_id

        full = self._names.full(registration.server, tool.name)
        server = McpServerInfo(name=registration.server, features=described.features())
        try:
            with self._contexts.applied(context):
                arguments = await self._sealed(described, tool, asked, offered, server)
        except (RefusalError, ToolConfigError) as exc:
            reason = f"{full}: {exc}"
            logger.warning("mcp relay: sealing refused: %s", reason)

            return self._refused(reason)
        except (ConnectionStoreError, KerberosError) as exc:
            reason = f"{full}: sealing the connections failed: {exc}"
            logger.warning("mcp relay: %s", reason)

            return self._refused(reason)

        sent = params.model_copy(update={"arguments": arguments})

        return await port.call(client, sent, tool, forwarded, journals, questions)

    @staticmethod
    def _named(tools: Iterator[mt.Tool]) -> dict[str, mt.Tool]:
        named: dict[str, mt.Tool] = {}
        for tool in tools:
            named[tool.name] = tool

        return named

    def _progress_of(
        self, ctx: ServerRequestContext[RelayedRequest]
    ) -> ProgressHeard | None:
        """Ход запроса агенту под его токеном прогресса; без токена — не нужен."""
        token = progress_token_from_params(ctx.params)
        if token is None:
            return None

        return partial(self._forwarded, ctx, token)

    @staticmethod
    def _refused(reason: str) -> mt.CallToolResult:
        """Отказ вызову ответом isError с причиной: модель видит её в том же
        вызове."""
        return mt.CallToolResult(content=[mt.TextContent(text=reason)], is_error=True)

    async def _forwarded(
        self,
        ctx: ServerRequestContext[RelayedRequest],
        token: mt.ProgressToken,
        params: mt.ProgressNotificationParams,
    ) -> None:
        """Уведомление сервера о ходе запроса — агенту как есть, с _meta,
        под токеном прогресса его запроса token."""
        own = params.model_copy(update={"progress_token": token})
        notification = mt.ProgressNotification(params=own)
        await ctx.session.send_notification(
            notification, related_request_id=ctx.request_id
        )

    async def _journaled(
        self, registration: McpRegistration, call_id: str, notice: JournalNotice
    ) -> None:
        """Журнал, который вызов call_id агента открыл на сервере, — ленте
        треда сообщением JournalOpened. Шина его не приняла — кнопка журнала
        появится по ссылке в итоге вызова, причина — в логе."""
        opened = JournalOpened(
            call_id=call_id,
            node=notice.key,
            server=registration.server,
            run=notice.run,
            call=notice.node,
        )
        scope = Scope.chat(registration.thread_id)
        try:
            await self._bus.publish(scope, opened, LockToken.local())
        except MessageBusError as exc:
            logger.warning(
                "mcp relay: the journal of call %s on server %s is named by its "
                "result only, the bus refused JournalOpened for thread %s: %s",
                call_id,
                registration.server,
                registration.thread_id,
                exc,
            )

    async def _sealed(
        self,
        described: McpDescription,
        tool: mt.Tool,
        asked: Mapping[str, Any],
        offered: Mapping[str, mt.Tool],
        server: McpServerInfo,
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
        arguments = await self._sealing.seal(full, asked, tool.input_schema, server)

        nested = described.nested_calls()
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
            node_tool = offered.get(call.tool)
            if node_tool is None:
                raise self._unknown_node(call, offered, server)

            node_full = self._names.full(server.name, call.tool)
            try:
                args = await self._sealing.seal(
                    node_full, call.args, node_tool.input_schema, server
                )
            except RefusalError as exc:
                msg = f"node {call.key!r} ({node_full}): {exc}"
                raise RefusalError(exc.kind, msg) from exc
            except ToolConfigError as exc:
                msg = f"node {call.key!r} ({node_full}): {exc}"
                raise ToolConfigError(msg) from exc

            sealed.append(call.model_copy(update={"args": args}))

        return nested.replaced(arguments, sealed)

    def _unknown_node(
        self, call: DagNode, offered: Mapping[str, mt.Tool], server: McpServerInfo
    ) -> RefusalError:
        """Отказ узлу call, чей инструмент сервер не предлагает; полному
        имени инструмента этого же сервера отказ называет его имя на сервере."""
        parsed = self._names.parse(call.tool)
        if parsed is None:
            return self._unknown_tool(call, offered, server)

        if parsed.server != server.name:
            return self._unknown_tool(call, offered, server)

        if parsed.tool not in offered:
            return self._unknown_tool(call, offered, server)

        msg = (
            f"node {call.key!r}: {call.tool!r} is the name the agent gives the "
            f"tool {parsed.tool!r} of mcp server {server.name!r}, while the "
            f"nodes of a nested call name tools as the server lists them; "
            f"name the tool of the node {parsed.tool!r}."
        )

        return RefusalError(InvokeErrorKind.UNKNOWN_TOOL, msg)

    @staticmethod
    def _unknown_tool(
        call: DagNode, offered: Mapping[str, mt.Tool], server: McpServerInfo
    ) -> RefusalError:
        msg = (
            f"node {call.key!r}: mcp server {server.name!r} offers the caller of "
            f"the call no tool {call.tool!r}; expected one of {sorted(offered)}"
        )

        return RefusalError(InvokeErrorKind.UNKNOWN_TOOL, msg)

    async def _port_of(self, request: RelayedRequest) -> McpToolPort:
        """Порт сервера регистрации запроса request.

        Ошибки:
        RequestError — serverId не выдан этим чатом либо снят.
        AuthenticationError — вход пользователя вкладки больше не годен.
        McpClientError — сервер профиля недоступен.
        """
        registration = self._registrations.of(request.server_id)
        context = registration.context()

        return await self._port(registration, context)

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

    async def _server_of(self, request: RelayedRequest, method: str) -> McpToolServer:
        """Порт сервера профиля для запроса method, кроме инструментов: у
        сервера chat ничего, кроме инструментов, нет.

        Ошибки:
        MCPError — регистрация — сервер chat (-32601).
        AuthenticationError — вход пользователя вкладки больше не годен.
        McpClientError — сервер профиля недоступен.
        """
        registration = self._registrations.of(request.server_id)
        if registration.server == ChatTools.NAME:
            msg = (
                f"{method}: server {ChatTools.NAME!r} is the chat itself and serves "
                "tools only; its server/discover declares no other capability"
            )
            raise MCPError(code=mt.METHOD_NOT_FOUND, message=msg)

        context = registration.context()
        subject = context.subject
        user = McpCaller(login=subject.login, roles=subject.roles)

        return await self._servers.port(registration.server, user)

    async def _app_server(
        self, registration: McpRegistration, context: CallContext
    ) -> McpToolServer:
        """Порт сервера регистрации, чьи приложения рисует чат: сервер
        профиля с секцией apps; приложения зовут его от личности самого
        чата.

        Ошибки:
        MCPError — сервер chat, сервер не из профиля треда или сервер без
            секции apps.
        McpClientError — сервер недоступен.
        """
        if registration.server not in registration.servers:
            msg = (
                f"mcp server {registration.server!r} is not a server of profile "
                f"{registration.profile!r} of the thread, its servers are "
                f"{sorted(registration.servers)}; an app may call only the server "
                "that gave it"
            )
            raise MCPError(code=mt.INVALID_PARAMS, message=msg)

        if registration.server == ChatTools.NAME:
            msg = (
                f"server {ChatTools.NAME!r} is the chat itself and has no apps; "
                "an app may call only the server that gave it"
            )
            raise MCPError(code=mt.INVALID_PARAMS, message=msg)

        subject = context.subject
        user = McpCaller(login=subject.login, roles=subject.roles)
        port = await self._servers.port(registration.server, user)
        if self._servers.apps(registration.server) is None:
            msg = (
                f"mcp server {registration.server!r} has no section "
                f"[mcp.servers.{registration.server}.apps], so the chat draws no "
                "apps of it and takes no app requests for it"
            )
            raise MCPError(code=mt.INVALID_PARAMS, message=msg)

        return port

    def _offered(
        self, tools: Sequence[mt.Tool], server: str, audience: Visibility
    ) -> Iterator[mt.Tool]:
        """Инструменты tools сервера server, видимые стороне audience: без
        _meta.ui.visibility — обеим, иначе — названной в нём. Инструмент,
        который сервер исполняет только задачей, не видит никто: задач
        клиент не объявляет.

        Ошибки:
        MCPError — _meta.ui инструмента не по форме MCP Apps: ошибка сервера.
        """
        for tool in tools:
            if self._task_only(tool, server):
                continue

            try:
                config = self._apps.of(tool)
            except ValidationError as exc:
                msg = (
                    f"mcp server {server!r}: tool {tool.name!r} carries _meta.ui "
                    "that is not an MCP Apps tool config, so its visibility to "
                    f"the {audience} is unknown: {ValidationText.of(exc)}"
                )
                raise MCPError(code=mt.INTERNAL_ERROR, message=msg) from exc

            if config is None:
                yield tool
                continue

            visibility = config.visibility
            if visibility is None:
                yield tool
                continue

            if audience in visibility:
                yield tool

    def _task_only(self, tool: mt.Tool, server: str) -> bool:
        """Сервер исполняет инструмент только задачей, а задач клиент не
        объявляет."""
        execution = tool.execution
        if execution is None:
            return False

        if execution.task_support != self.TASK_ONLY:
            return False

        logger.info(
            "mcp server %r: tool %r runs only as a task, which the client does "
            "not declare; it is offered to no one",
            server,
            tool.name,
        )

        return True
