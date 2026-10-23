"""MCP-сервер сервиса исполнения: инструменты реестра по ролям вызывающего.

Сервер собран на примитивах fastmcp. Список инструментов отдаёт провайдер
по ролям токена; вызов исполняет порт ToolServer — та же цепочка обвязок и
тот же исполнитель DAG, что в процессе чата; контекст вызова ставит
middleware из токена. Логики во входе нет: адаптер boba.mcp_adapter
(McpTool, McpToolProvider) разбирает запрос в вызов-узел DagNode и зовёт
порт вызывающего (RoleToolServers); итог порта уходит клиенту ответом
McpReplies — стандартными полями результата MCP (ResultWire). Тем же
компонентом отвечают операции сервиса и middleware: отказ сервера — тоже
итог с текстом отказа.

Во время вызова сервер пересылает клиенту отчёты тела о ходе работы
уведомлениями notifications/progress, а клиенту с расширением журнала — и
открытые вызовом журналы уведомлением расширения, оба в потоке ответа
самого вызова (CallProgress); каналы журнала вызова — ресурсы сервера:
журналы идущих вызовов перечисляет resources/list, окна читает
resources/read, итог несёт ссылки на каналы (JournalUris). Адрес
загрузки файла отдаёт операция file_upload (FileUploadTool), видная только
интерфейсу клиента. Клиентов сервер принимает обеих эпох — server/discover
(2026-07-28) и initialize прежних редакций — и объявляет возможности такими,
какие у него на их проводе есть (McpServer.on_discover, on_initialize).

Ошибки:
наружу уходит только итог вызова — сбой любого вида возвращается
    результатом с isError и текстом отказа в content.
TokenClaimsError — токен прошёл проверку подписи, но не несёт логина и ролей;
    вызов и список инструментов для него отказывают.
ProgressChannelError — тело написало в канал отчётов о ходе работы строку,
    которая не проходит модель ProgressReport; остаётся в логе сервиса,
    вызов от неё не срывается.
"""

from __future__ import annotations

import asyncio
import logging
from abc import abstractmethod
from collections.abc import AsyncGenerator, Iterator, Mapping, Sequence
from contextlib import AsyncExitStack, asynccontextmanager
from enum import StrEnum
from typing import Any, ClassVar, Protocol
from uuid import uuid4

import mcp_types as mt
from fastmcp import Context, FastMCP
from fastmcp.apps.config import AppConfig, app_config_to_meta_dict
from fastmcp.server.auth import AuthProvider
from fastmcp.server.dependencies import get_context
from fastmcp.server.http import StarletteWithLifespan
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.server.providers import Provider
from fastmcp.tools import Tool
from fastmcp.tools.base import ToolResult
from mcp.server.apps import EXTENSION_ID as APPS_EXTENSION
from mcp.server.caching import CacheHint
from mcp.server.subscriptions import ResourceUpdated, SubscriptionBus
from mcp.shared.jsonrpc_dispatcher import progress_token_from_params
from mcp_types.version import MODERN_PROTOCOL_VERSIONS
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, ValidationError
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import BaseRoute, Mount, Route
from starlette.types import ASGIApp, Receive, Send
from starlette.types import Scope as AsgiScope

from boba.access import (
    GrantCheck,
    ProfileGrant,
    RoleVisibility,
    ToolGrant,
    ToolGrants,
)
from boba.cancellation import RunCancellation, StopReason, ToolStopped
from boba.canvas.keys import ObjectKey
from boba.connection_broker.sealed import SealedConnectionParams
from boba.connections.sealed import OfferedSource, SealFeature
from boba.identity.context import (
    CallContext,
    HumanInitiator,
)
from boba.identity.run import Runs
from boba.identity.signin import ProfileCatalog
from boba.mcp_adapter import (
    CallerServers,
    JournalUris,
    McpReplies,
    McpTool,
    McpToolProvider,
    RoleTools,
)
from boba.mcp_server.auth import (
    CallScopeError,
    CallScopes,
    ServiceAuth,
    TokenSubjects,
)
from boba.mcp_server.files import (
    CallerBus,
    FileRoutes,
    JournalListing,
    JournalRoutes,
    OpenJournal,
    OpenJournals,
    RouteCallers,
    ServerRefusal,
)
from boba.runtime.storage import LocalStorageConfig, StorageFactory
from boba.toolkit.channels import JournalChannels, ToolChannel
from boba.toolkit.dag import CallDag, DagNode, ToolServer
from boba.toolkit.failure import ValidationText
from boba.toolkit.ports import ProgressReport
from boba.toolkit.result import ContentMime, ErrorResult
from boba.toolkit.wire import (
    FilesFeature,
    JournalFeature,
    JournalNotice,
    JournalRead,
    McpContent,
    ScopeFeature,
)
from boba.toolrun.call_id import CallFields
from boba.toolrun.hosted import DirectCalls
from boba.toolrun.registry import ToolRegistry
from boba.toolrun.streams import (
    CallJournals,
    StreamAppended,
    StreamFeed,
    StreamPumps,
    ToolStream,
)

__all__ = [
    "CallContextMiddleware",
    "CallProgress",
    "ConnectionOfferRule",
    "EndpointCatalog",
    "FileUploadTool",
    "McpEndpoint",
    "McpEndpoints",
    "McpServer",
    "OfferClient",
    "RoleToolServers",
    "RunLimitMiddleware",
    "RunLimits",
]

logger = logging.getLogger(__name__)


class OfferClient(StrEnum):
    """Признак клиента в правиле предложения соединений: SEALING — запрос
    объявил расширение запечатывания соединений (SealFeature), PLAIN — не
    объявил, ANY — неважно."""

    SEALING = "sealing"
    PLAIN = "plain"
    ANY = "any"

    def matches(self, sealing: bool) -> bool:
        """Подходит ли правило клиенту, который запечатывает (sealing) или нет."""
        if self is OfferClient.ANY:
            return True

        if sealing:
            return self is OfferClient.SEALING

        return self is OfferClient.PLAIN


class ConnectionOfferRule(RoleVisibility):
    """Правило предложения соединений [[mcp.endpoints.<имя>.connections]]:
    кому (roles, '*' — всем), какому клиенту (client) и какие источники
    соединений предложить (offer). Правила endpoint'а проверяет по порядку
    McpEndpoint.offer."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    client: OfferClient
    offer: list[OfferedSource] = Field(min_length=1)

    def matches(self, roles: frozenset[str], sealing: bool) -> bool:
        """Подходит ли правило вызывающему с ролями roles и признаком
        клиента sealing."""
        if not self.visible_for(roles):
            return False

        return self.client.matches(sealing)


class McpEndpoint(ProfileGrant):
    """Секция [mcp.endpoints.<имя>]: грант профиля (роли, инструменты) и
    правила предложения соединений. Правило пишется явно, у endpoint'а без
    соединений — пустым списком. Конфиг сервиса разбирает секцию на старте;
    EndpointCatalog берёт её грантом профиля, RoleToolServers — правилами."""

    connections: list[ConnectionOfferRule]

    def offer(self, roles: frozenset[str], sealing: bool) -> frozenset[OfferedSource]:
        """Источники соединений по первому подходящему правилу; ни одно не
        подошло — соединений нет."""
        for rule in self.connections:
            if rule.matches(roles, sealing):
                return frozenset(rule.offer)

        return frozenset()


# RoleTools первым: иначе __init__ протокола CallerServers молча пропустит его __init__
class RoleToolServers(RoleTools[tuple[frozenset[str], str, bool]], CallerServers):
    """Реализация CallerServers сервиса на базе RoleTools: инструменты
    вызывающего по его токену и признаку клиента из запроса.

    Создаётся сборкой сервера endpoint'а из реестра инструментов, субъектов
    токена, секции endpoint'а и соединений инструментов. Ключ кэша —
    «роли, профиль, клиент запечатывает»; порт (ToolServer) ключа содержит
    только инструменты, доступные этим ролям вне чата, — с
    параметрами-соединениями под предложение правил endpoint'а
    (SealedConnectionParams.offered); по ним же собирается инструмент
    workflow. Признак клиента — объявленное в запросе расширение
    запечатывания соединений (SealFeature).
    """

    def __init__(
        self,
        registry: ToolRegistry,
        subjects: TokenSubjects,
        endpoint: McpEndpoint,
        params: SealedConnectionParams,
    ) -> None:
        super().__init__()
        self._registry = registry
        self._subjects = subjects
        self._endpoint = endpoint
        self._params = params
        self._calls = DirectCalls()

    async def current(self) -> Mapping[str, McpTool]:
        """Инструменты вызывающего текущего запроса; без токена —
        TokenClaimsError."""
        subject = self._subjects.current()
        settings = get_context().client_extension_settings(SealFeature.ID)
        sealing = settings is not None

        return self.tools((subject.roles, subject.profile, sealing))

    def _server(self, key: tuple[frozenset[str], str, bool]) -> ToolServer:
        roles, profile, sealing = key
        tools = self._registry.for_headless(roles, profile)
        sources = self._endpoint.offer(roles, sealing)
        offered = self._params.offered(list(tools.values()), roles, sources)

        return self._registry.server(offered, self._calls)


class ProgressChannelError(Exception):
    """Тело инструмента написало в канал отчётов то, что отчётом не является."""


class ProgressShape(Protocol):
    """Как отчёт тела становится уведомлением прогресса запроса. Реализуют
    OwnProgress (вызов одного инструмента) и NodeProgress (связка)."""

    @abstractmethod
    def shaped(
        self, call_id: str, report: ProgressReport, token: mt.ProgressToken
    ) -> mt.ProgressNotificationParams | None:
        """Уведомление по отчёту вызова call_id под токеном прогресса
        запроса token; None — отчёт не уходит."""

    @abstractmethod
    def opened(
        self, call_id: str, run: str, token: mt.ProgressToken
    ) -> JournalNotice | None:
        """Уведомление об открытом журнале вызова call_id запуска run под
        токеном прогресса запроса token; None — вызов не назван."""


class OwnProgress(ProgressShape):
    """Реализация ProgressShape вызовом одного инструмента: числа и текст
    тела идут как есть. Значение прогресса запроса обязано расти, поэтому
    отчёт, в котором оно не выросло, не уходит."""

    def __init__(self) -> None:
        self._sent = -1.0
        """Последнее ушедшее значение; -1 — отчётов ещё не было: первым
        уходит и отчёт с done = 0."""

    def shaped(
        self, call_id: str, report: ProgressReport, token: mt.ProgressToken
    ) -> mt.ProgressNotificationParams | None:
        if report.done <= self._sent:
            return None

        self._sent = report.done

        return mt.ProgressNotificationParams(
            progress_token=token,
            progress=report.done,
            total=report.total,
            message=report.message,
        )

    def opened(
        self, call_id: str, run: str, token: mt.ProgressToken
    ) -> JournalNotice | None:
        return JournalNotice(
            progress_token=token,
            run=run,
            node=call_id,
            channels=JournalChannels.VISIBLE,
        )


class NodeProgress(ProgressShape):
    """Реализация ProgressShape связкой: отчёты нескольких узлов идут одним
    запросом. Каждый узел считает в своих единицах, поэтому значением
    прогресса запроса служит номер отчёта связки, а итог неизвестен; числа
    узла, если тот назвал итог, стоят в тексте. Чей это отчёт, говорит
    _meta уведомления: ключ узла от автора связки лежит под ключом,
    объявленным расширением вложенных вызовов. Создаётся с вызовом связки:
    по нему CallDag находит ключ узла по идентификатору его журнала."""

    def __init__(self, call: DagNode) -> None:
        self._call = call
        self._dags = CallDag()
        self._feature = self._dags.feature()
        self._count = 0

    def shaped(
        self, call_id: str, report: ProgressReport, token: mt.ProgressToken
    ) -> mt.ProgressNotificationParams | None:
        title = self._dags.title_of(self._call, call_id)
        if title is None:
            logger.warning(
                "workflow progress: report of call %s matches no node of the "
                "workflow, got %r",
                call_id,
                report.message,
            )
            return None

        self._count += 1
        text = report.message
        if report.total is not None:
            text = f"{text} ({report.done:g} of {report.total:g})"

        return mt.ProgressNotificationParams(
            progress_token=token,
            progress=self._count,
            message=text,
            _meta={self._feature.progress_key: title},
        )

    def opened(
        self, call_id: str, run: str, token: mt.ProgressToken
    ) -> JournalNotice | None:
        title = self._dags.title_of(self._call, call_id)
        if title is None:
            logger.warning(
                "workflow journal: call %s matches no node of the workflow", call_id
            )
            return None

        return JournalNotice(
            progress_token=token,
            run=run,
            node=call_id,
            channels=JournalChannels.VISIBLE,
            key=title,
        )


class CallProgress(StreamFeed):
    """Ход работы вызова уведомлениями notifications/progress, открытые им
    журналы уведомлениями расширения журнала и рост журнала событиями
    подписки на ресурсы. Реализация StreamFeed: за журналами запуска следят
    насосы StreamPumps.

    Создаётся CallContextMiddleware на каждый вызов инструмента и следит за
    журналами запуска (CallJournals.following зовёт opened). Об открытом
    журнале клиент, объявивший расширение журнала, узнаёт уведомлением
    JournalFeature.OPENED в потоке ответа своего вызова (ProgressShape.opened:
    адрес журнала и ключ узла связки). Для клиента без расширения журнал
    становится ресурсом resources/list вызывающего (OpenJournals шлёт его
    потокам subscriptions/listen смену списка); close() снимает журналы
    запуска со списка. Рост видимого канала уходит событием ResourceUpdated
    по uri канала в шину подписок сервера (bus): его получают потоки
    subscriptions/listen этого uri. Тело пишет отчёты о ходе работы в канал
    ToolChannel.PROGRESS; отсюда они уходят клиенту стандартным прогрессом
    запроса: числа done и total и текст отчёта. token — токен прогресса из
    _meta запроса; None — клиент о ходе вызова не просил, и уведомлений нет.
    Уведомления ждут в очереди, а отправляет их задача, заведённая start()
    внутри запроса: уведомление привязано к запросу, и из чужой задачи оно
    не уходит. Каким отчёт уходит клиенту, решает shape: у вызова одного
    инструмента — как есть, у связки — с ключом узла в _meta.
    """

    def __init__(  # noqa: PLR0913 — ход вызова привязан к запросу, журналам, запуску и токену
        self,
        context: Context,
        journals: CallJournals,
        user: str,
        run: str,
        shape: ProgressShape,
        uris: JournalUris,
        bus: SubscriptionBus,
        listed: OpenJournals,
        token: mt.ProgressToken | None,
    ) -> None:
        self._context = context
        self._journals = journals
        self._user = user
        self._run = run
        self._shape = shape
        self._uris = uris
        self._bus = bus
        self._listed = listed
        self._token = token
        self._noticed = context.client_extension_settings(JournalFeature.ID) is not None
        """Клиент объявил расширение журнала: открытые журналы — ему
        уведомлением."""
        self._queue: asyncio.Queue[mt.JSONRPCNotification | None]
        self._queue = asyncio.Queue()
        self._sender: asyncio.Task[None] | None = None
        self._pumps = StreamPumps(self)
        self._offsets: dict[str, int] = {}
        """Вызов → байт, до которого прочитан его канал отчётов."""
        self._journaled: list[str] = []

    def start(self) -> None:
        token = self._token
        if token is None:
            return

        self._sender = asyncio.create_task(self._send(), name=f"progress:{self._run}")

    def opened(self, call_id: str, stream: ToolStream) -> None:
        """Наблюдатель CallJournals: журнал вызова открыт — вызов попадает в
        список журналов запуска, за журналом следит насос, журнал становится
        ресурсом resources/list вызывающего, а клиенту с расширением журнала
        и токеном прогресса уходит уведомление о нём."""
        self._journaled.append(call_id)
        self._pumps.opened(call_id, stream)
        self._listed.opened(self._user, OpenJournal(run=self._run, node=call_id))
        if not self._noticed:
            return

        token = self._token
        if token is None:
            return

        notice = self._shape.opened(call_id, self._run, token)
        if notice is None:
            return

        self._queued(JournalFeature.OPENED, notice)

    def journaled(self) -> Sequence[str]:
        """Вызовы запуска с журналом в порядке их открытия."""
        return list(self._journaled)

    async def stream_appended(self, message: StreamAppended) -> None:
        """Канал отчётов — уведомлениями прогресса, если клиент о нём просил;
        видимый канал — событием роста ресурса журнала."""
        if message.channel is ToolChannel.PROGRESS:
            token = self._token
            if token is None:
                return

            offset = self._offsets.get(message.call_id, 0)
            self._offsets[message.call_id] = self._take(message.call_id, offset, token)
            return

        if not JournalChannels.visible(message.channel):
            return

        read = JournalRead(run=self._run, node=message.call_id, channel=message.channel)
        await self._bus.publish(ResourceUpdated(uri=self._uris.uri(read)))

    async def close(self) -> None:
        """Дожидается насосов и отправки всех отчётов: после ответа на вызов
        уведомления теряются без ошибки; журналы запуска уходят из списка
        ресурсов."""
        await self._pumps.close()
        self._listed.closed(self._user, self._run)

        sender = self._sender
        if sender is None:
            return

        self._queue.put_nowait(None)
        await sender

    def _take(self, call_id: str, offset: int, token: mt.ProgressToken) -> int:
        """Уведомления по отчётам канала с байта offset — в очередь; итог —
        байт, до которого канал прочитан."""
        while True:
            piece = self._journals.recorded_slice(
                self._user, self._run, call_id, offset, ToolChannel.PROGRESS
            )
            if piece is None:
                return offset

            if piece.end <= offset:
                return offset

            for line in piece.text.splitlines():
                report = self._report_of(call_id, line)
                if shaped := self._shape.shaped(call_id, report, token):
                    notification = mt.ProgressNotification(params=shaped)
                    self._queued(notification.method, shaped)

            offset = piece.end

    def _queued(self, method: str, params: BaseModel) -> None:
        """Уведомление method с параметрами params — в очередь отправки."""
        wire = params.model_dump(by_alias=True, mode="json", exclude_none=True)
        notification = mt.JSONRPCNotification(jsonrpc="2.0", method=method, params=wire)
        self._queue.put_nowait(notification)

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

    async def _send(self) -> None:
        # типизированная отправка SDK знает только уведомления ядра, а
        # уведомление расширения уходит тем же каналом запроса в обход неё
        outbound = self._context.session._request_outbound
        while True:
            notification = await self._queue.get()
            if notification is None:
                return

            await outbound.notify(notification.method, notification.params)


class FileUploadRequest(BaseModel):
    """Аргументы file_upload: имя файла в каталоге вложений области. Схему
    вызова и проверку аргументов FileUploadTool собирает из неё с
    параметром области вызова (CallFields.scoped)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(
        min_length=1,
        description="Имя файла, под которым он ляжет в каталог вложений workspace.",
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
        "Адрес для загрузки файла в workspace:\n"
        "   - name — имя файла в каталоге вложений workspace\n"
        "Тело файла клиент шлёт HTTP PUT на полученный путь этого сервера тем "
        "же bearer-токеном; инструменты затем читают файл по полученному пути "
        "в workspace.\n"
    )

    _request: type[BaseModel] = PrivateAttr()
    _routes: FileRoutes = PrivateAttr()
    _subjects: TokenSubjects = PrivateAttr()
    _scopes: CallScopes = PrivateAttr()
    _replies: McpReplies = PrivateAttr()

    def __init__(self, routes: FileRoutes, subjects: TokenSubjects) -> None:
        scoped = CallFields().scoped(FileUploadRequest)
        super().__init__(
            name=self.NAME,
            description=self.DESCRIPTION,
            parameters=scoped.model_json_schema(),
            meta=dict(McpEndpoints.OPERATION_META),
        )
        self._request = scoped
        self._routes = routes
        self._subjects = subjects
        self._scopes = CallScopes()
        self._replies = McpReplies()

    async def run(self, arguments: dict[str, object]) -> ToolResult:
        try:
            checked = self._request.model_validate(arguments)
            request = FileUploadRequest.model_construct(**checked.model_dump())
            subject = self._subjects.current()
            scope = self._scopes.of(subject, arguments)
            key = ObjectKey(
                user_id=subject.user_key, scope_id=scope.id, name=request.name
            )
        except ValidationError as exc:
            return self._refused(
                f"file_upload: the arguments do not name a file: "
                f"{ValidationText.of(exc)}"
            )
        except CallScopeError as exc:
            return self._refused(f"file_upload: {exc}")

        address = self._routes.address(key.scope_id, key.name)
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
    (CallProgress), в конце итога — ссылки на каналы журналов вызовов
    запуска (JournalUris). Клиент закрыл
    соединение — отмена помечается, DAG гаснет. Операции сервиса
    (operations) проходят мимо: запуска у них нет.
    """

    PARAMS_META: ClassVar[str] = "_meta"
    """Ключ блока _meta в параметрах запроса: fastmcp отдаёт сам блок, а
    разбор токена прогресса SDK ждёт параметры целиком."""

    def __init__(  # noqa: PLR0913 — middleware собирается всеми входами endpoint'а
        self,
        runs: Runs,
        journals: CallJournals,
        subjects: TokenSubjects,
        operations: frozenset[str],
        bus: SubscriptionBus,
        listed: OpenJournals,
    ) -> None:
        self._runs = runs
        self._journals = journals
        self._subjects = subjects
        self._operations = operations
        self._bus = bus
        self._listed = listed
        self._replies = McpReplies()
        self._uris = JournalUris(JournalRoutes.TEMPLATE)
        self._scopes = CallScopes()

    async def on_call_tool(
        self,
        context: MiddlewareContext[mt.CallToolRequestParams],
        call_next: CallNext[mt.CallToolRequestParams, ToolResult],
    ) -> ToolResult:
        if context.message.name in self._operations:
            return await call_next(context)

        subject = self._subjects.current()

        arguments: Mapping[str, Any] = {}
        if sent := context.message.arguments:
            arguments = sent

        try:
            scope = self._scopes.of(subject, arguments)
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

        shape: ProgressShape = OwnProgress()
        if context.message.name == CallDag.WORKFLOW:
            shape = NodeProgress(
                DagNode(key=run_id, tool=context.message.name, args=dict(arguments))
            )

        progress = CallProgress(
            get_context(),
            self._journals,
            subject.user_key,
            run_id,
            shape,
            self._uris,
            self._bus,
            self._listed,
            self._progress_token(),
        )
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

        links = McpContent(run_id)
        for address in self._recorded(progress, subject.user_key, run_id):
            uri = self._uris.uri(address)
            links.link(uri, address.channel.value, ContentMime.PLAIN, False)

        return self._replies.journaled(result, links.blocks())

    def _progress_token(self) -> mt.ProgressToken | None:
        """Токен прогресса из _meta запроса разбором SDK; None — клиент о
        ходе вызова не просил."""
        request = get_context().request_context
        if request is None:
            return None

        return progress_token_from_params({self.PARAMS_META: request.meta})

    def _recorded(
        self, progress: CallProgress, user: str, run_id: str
    ) -> Iterator[JournalRead]:
        """Каналы с записью журналов вызовов запуска run_id."""
        for node in progress.journaled():
            channels = self._journals.recorded_channels(user, run_id, node)
            for channel in channels:
                yield JournalRead(run=run_id, node=node, channel=channel)


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


class McpServer(Middleware):
    """Сборка MCP-сервера одного endpoint'а сервиса исполнения и его
    middleware fastmcp, отвечающий на согласование протокола.

    Создаётся сборкой endpoint'ов (McpEndpoints) на endpoint конфига сервиса:
    из реестра инструментов, реестра запусков, журналов, проверяющего
    токены, имени профиля и общего на процесс предела запусков. Инструменты
    endpoint'а — те, что профиль и роли вошедшего разрешают; mcp() отдаёт
    готовый сервер fastmcp. При активных журналах каналы журнала — ресурсы
    сервера: шаблон и окна (JournalRoutes), журналы идущих вызовов
    вызывающего (JournalListing над OpenJournals), рост и смена списка —
    потоками subscriptions/listen над шиной CallerBus. Ответы на
    согласование (эпоху выбирает SDK тем, какой крючок зовёт) объявляют
    ровно это: список инструментов не меняется (он зависит только от ролей
    токена, а реестр собран на старте), промптов, логов и подсказок нет.
    Подсказки кэша — по природе ответа: описание зависит только от конфига
    (public), список инструментов — от ролей токена и объявленного клиентом
    расширения (private), оба свежи HINT_TTL_MS; журналы идущих вызовов и их
    окна меняются всё время и идут с умолчанием SDK — 0 и private.
    close() штатно закрывает потоки подписки при остановке процесса.
    """

    NAME: ClassVar[str] = "boba-mcp"
    HINT_TTL_MS: ClassVar[int] = 300_000
    """Срок свежести описания и списка инструментов: реестр собран на старте
    процесса, и меняет их только перезапуск сервиса."""

    def __init__(  # noqa: PLR0913 — сервер собирается всеми своими входами
        self,
        registry: ToolRegistry,
        runs: Runs,
        journals: CallJournals,
        auth: AuthProvider,
        profile: str,
        endpoint: McpEndpoint,
        params: SealedConnectionParams,
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
        bus = CallerBus(self._subjects)
        listed = OpenJournals(bus)
        self._files = FileRoutes(
            StorageFactory.create(storage), journals, callers, path, published
        )
        self._journal_files = JournalRoutes(
            journals, callers, self._subjects, storage, path, published, bus
        )
        self._listing = JournalListing(listed, self._subjects)
        upload = FileUploadTool(self._files, self._subjects)
        self._features = self._features_of(registry)
        self._features[ScopeFeature.ID] = CallFields().feature().settings()
        self._features[FilesFeature.ID] = self._files.feature(upload.name).settings()
        self._journaled = journals.active()
        if self._journaled:
            journal = JournalFeature(
                path=self._journal_files.path(), resource=JournalRoutes.TEMPLATE
            )
            self._features[JournalFeature.ID] = journal.settings()

        self._provider = McpToolProvider(
            RoleToolServers(registry, self._subjects, endpoint, params), [upload]
        )
        self._contexts = CallContextMiddleware(
            runs, journals, self._subjects, McpEndpoints.OPERATIONS, bus, listed
        )

    @staticmethod
    def _features_of(registry: ToolRegistry) -> dict[str, dict[str, Any]]:
        """Возможности сервера для объявления при подключении клиента: те,
        что объявляет порт над всеми инструментами реестра (инструмент-связка,
        ключ запечатывания соединений); область вызова, файлы и журнал
        endpoint добавляет сам."""
        declared: dict[str, dict[str, Any]] = {}
        port = registry.server(registry.tools, DirectCalls())
        for feature, settings in port.features().items():
            declared[feature] = dict(settings)

        return declared

    def mcp(self) -> FastMCP:
        providers: list[Provider] = [self._provider]
        if self._journaled:
            providers.append(self._listing)

        server = FastMCP(
            self._name,
            auth=self._auth,
            providers=providers,
            middleware=[self, self._limits, self._contexts],
            dereference_schemas=False,
        )
        # сбой списка инструментов — ошибка, а не «у вызывающего нет инструментов»
        server.provider_error_strategy = "raise"
        # подсказка fastmcp одна на все методы, а журналы свежими не бывают
        server._mcp_server.cache_hints = {
            "tools/list": CacheHint(ttl_ms=self.HINT_TTL_MS, scope="private")
        }
        self._files.install(server)
        if self._journaled:
            # закрытое имя fastmcp: спец-методы MCP в ServerExtension запрещены
            self._journal_files.install(server, server._mcp_server)

        return server

    async def on_discover(
        self,
        context: MiddlewareContext[mt.DiscoverRequest],
        call_next: CallNext[mt.DiscoverRequest, mt.DiscoverResult | dict[str, Any]],
    ) -> mt.DiscoverResult | dict[str, Any]:
        """Ресурсы журналов с подпиской и сменой списка, расширения boba и
        MCP Apps, по которому операции сервиса видны только интерфейсу."""
        extensions: dict[str, dict[str, Any]] = {APPS_EXTENSION: {}}
        extensions.update(self._features)
        capabilities = self._capabilities(listened=True)

        return mt.DiscoverResult(
            supported_versions=list(MODERN_PROTOCOL_VERSIONS),
            capabilities=capabilities.model_copy(update={"extensions": extensions}),
            ttl_ms=self.HINT_TTL_MS,
            cache_scope="public",
        )

    async def on_initialize(
        self,
        context: MiddlewareContext[mt.InitializeRequest],
        call_next: CallNext[mt.InitializeRequest, mt.InitializeResult | None],
    ) -> mt.InitializeResult | None:
        """Прежние редакции — без сессии, уведомлениям идти некуда, а
        расширений их провод не несёт."""
        initialized = await call_next(context)
        if initialized is None:
            return None

        capabilities = self._capabilities(listened=False)

        return initialized.model_copy(update={"capabilities": capabilities})

    def _capabilities(self, listened: bool) -> mt.ServerCapabilities:
        """Инструменты без смены списка и ресурсы журналов; подписка и смена
        списка — только с потоками subscriptions/listen (listened)."""
        resources: mt.ResourcesCapability | None = None
        if self._journaled:
            resources = mt.ResourcesCapability(
                subscribe=listened, list_changed=listened
            )

        return mt.ServerCapabilities(
            tools=mt.ToolsCapability(list_changed=False), resources=resources
        )

    def close(self) -> None:
        """Штатный конец потоков subscriptions/listen endpoint'а."""
        self._journal_files.close()


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

    Создаётся сборкой процесса. Endpoint — набор инструментов, роли,
    которым он выдан, и правила предложения соединений; каждый отвечает
    отдельным MCP-сервером на пути `{base_path}/{имя}`. Токен без области
    endpoint'а получает 403 от входа fastmcp (EndpointTokens). Реестр
    инструментов, соединения инструментов, запуски, журналы и предел запусков
    у endpoint'ов общие. app() — приложение ASGI со всеми endpoint'ами,
    маршрутами входа и пробой готовности.
    """

    HEALTH: ClassVar[str] = "/health"
    """Путь пробы готовности: без входа, отдаёт пути endpoint'ов."""

    OPERATIONS: ClassVar[frozenset[str]] = frozenset({FileUploadTool.NAME})
    """Операции сервиса: запуска не открывают и в предел запусков не входят."""

    OPERATION_META: ClassVar[Mapping[str, Any]] = {
        "ui": app_config_to_meta_dict(AppConfig(visibility=["app"]))
    }
    """_meta операций сервиса: их зовёт интерфейс клиента, а не модель
    (visibility ["app"] расширения MCP Apps)."""

    def __init__(  # noqa: PLR0913 — endpoint'ы собираются всеми входами процесса
        self,
        registry: ToolRegistry,
        runs: Runs,
        journals: CallJournals,
        auth: ServiceAuth,
        endpoints: Mapping[str, McpEndpoint],
        params: SealedConnectionParams,
        limits: RunLimits,
        base_path: str,
        storage: LocalStorageConfig,
    ) -> None:
        # маршруты входа стоят в корне: сервер авторизации — вне endpoint'ов
        self._routes: list[BaseRoute] = list(auth.routes())
        self._endpoints: list[EndpointApp] = []
        self._servers: list[McpServer] = []
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
                endpoints[profile],
                params,
                slots,
                storage,
                path,
                auth.published(),
            )
            served = server.mcp().http_app(path=path, stateless_http=True)
            self._servers.append(server)
            self._endpoints.append(EndpointApp(path, served, served))
            # метаданные ресурса клиент ищет от корня хоста, а не под endpoint'ом
            self._routes.extend(provider.get_well_known_routes(path))

    def end_subscriptions(self) -> None:
        """Штатный конец потоков subscriptions/listen всех endpoint'ов: при
        остановке процесса клиенты получают итог подписки, а не обрыв."""
        for server in self._servers:
            server.close()

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
    async def _lifespan(self, app: Starlette) -> AsyncGenerator[None, None]:
        """Жизненный цикл endpoint'ов: у каждого свой менеджер сессий MCP."""
        async with AsyncExitStack() as stack:
            for endpoint in self._endpoints:
                served = endpoint.lifespan
                await stack.enter_async_context(served.router.lifespan_context(served))

            yield
