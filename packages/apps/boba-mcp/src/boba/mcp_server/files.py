"""Файлы workspace пользователя и журналы вызовов на сервисе: потоковая
запись и чтение по HTTP, журналы — ещё и ресурсами MCP.

Байты файла идут отдельным маршрутом endpoint'а, а не вызовом MCP: тело
запроса читается чанками и сразу пишется в хранилище workspace, чтение
отдаёт окно чанков. Файл не оседает ни в памяти, ни на диске вне workspace.
Адрес файла — область (scope), каталог области и имя; владелец — вошедший
по токену, чужой workspace по адресу недостижим. DELETE по адресу самой
области удаляет область целиком — файлы её каталогов и журналы вызовов её
запусков: так клиент закрывает свою область. Каналы журнала вызова —
ресурсы MCP: окна читает resources/read, журналы идущих вызовов перечисляет
resources/list, рост и смену списка сообщают потоки subscriptions/listen.

Ошибки:
HTTPException 400 — адрес не называет файл каталога области или область.
HTTPException 401 — запрос без токена входа либо токен негоден.
HTTPException 404 — файла нет в workspace.
HTTPException 416 — диапазон начинается за концом файла.
HTTPException 500 — журналы вызовов удаляемой области не удалились.
HTTPException 507 — в workspace не осталось места.
InvalidResourceError (ValidationError fastmcp) — resources/read канала
    журнала: адрес не канал, канал не читается либо журнала вызова у
    вызывающего нет; fastmcp отвечает -32602 «ресурса нет».
MCPError — subscriptions/listen на uri, который не называет канал журнала,
    либо на журнал, которого у вызывающего нет (чужой запуск, нет канала).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, ClassVar

import mcp_types as mt
from fastmcp import FastMCP
from fastmcp.exceptions import ValidationError as InvalidResourceError
from fastmcp.resources import Resource, ResourceContent, ResourceResult
from fastmcp.server.auth import AccessToken
from fastmcp.server.providers import Provider
from fastmcp.utilities.components import FastMCPComponent
from fastmcp.utilities.versions import VersionSpec
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.context import ServerRequestContext
from mcp.server.lowlevel import Server
from mcp.server.subscriptions import (
    ListenHandler,
    ResourcesListChanged,
    ServerEvent,
    SubscriptionBus,
)
from mcp.shared.exceptions import MCPError
from pydantic import AnyUrl, BaseModel, ConfigDict, Field, ValidationError
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from boba.canvas.journal import StreamJournalError, StreamKey, StreamSlice
from boba.canvas.keys import ObjectKey, ScopeDir, WorkspaceMount
from boba.canvas.storage import StorageFullError, StorageNotFoundError
from boba.canvas.transfer import ContentDisposition, FileHeader, UploadPolicy
from boba.core.validation import ValidationText
from boba.identity.context import Scope, Subject
from boba.mcp_adapter import JournalUris
from boba.mcp_server.auth import TokenSubjects
from boba.runtime.served import StreamedFile
from boba.runtime.storage import (
    LocalStorageClient,
    LocalStorageConfig,
    StorageClient,
)
from boba.toolkit.channels import JournalChannels
from boba.toolkit.result import ContentMime
from boba.toolkit.wire import FilesFeature, JournalFeature, JournalRead
from boba.toolrun.streams import CallJournals

__all__ = [
    "CallerBus",
    "FileRoutes",
    "JournalListing",
    "JournalRoutes",
    "OpenJournal",
    "OpenJournals",
    "RouteCallers",
    "ServerRefusal",
]

logger = logging.getLogger(__name__)


class FilePart(StrEnum):
    """Параметры пути маршрута файлов и заголовки ответа."""

    SCOPE = "scope"
    DIR = "dir"
    NAME = "name"
    ETAG = "etag"
    AUTHORIZATION = "authorization"
    BEARER = "bearer "


class ServerRefusal(StrEnum):
    """Виды отказов сервера вызову: не дошёл до исполнителя либо остановлен
    вместе с запуском."""

    INVALID_REQUEST = "invalid_request"
    FORBIDDEN = "forbidden"
    NOT_FOUND = "not_found"
    RUN_LIMIT = "run_limit"
    STOPPED = "stopped"


class FileStored(BaseModel):
    """Ответ записи файла: путь, каким его видят инструменты, и размер."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str
    size: int = Field(ge=0)


class RouteCallers:
    """Вошедший по токену запроса к маршруту сервиса.

    Создаётся сервером endpoint'а для маршрутов файлов и журналов. Токен
    запроса уже проверен входом fastmcp всего приложения endpoint'а: итог
    лежит в самом запросе (scope["user"]). Наличие токена и область
    endpoint'а вход на чужих маршрутах не требует, поэтому их сверяет
    маршрут: без токена — 401, без области endpoint'а — 403.
    """

    def __init__(self, subjects: TokenSubjects, endpoint: str) -> None:
        self._subjects = subjects
        self._endpoint = endpoint

    async def of(self, request: Request) -> Subject:
        # вошедший берётся из этого запроса: get_access_token() без токена в
        # запросе отдаёт вошедшего прошлого запроса того же соединения
        user = request.scope.get("user")
        if not isinstance(user, AuthenticatedUser):
            msg = (
                f"{request.method} {request.url.path}: expected a valid bearer "
                "token of the sign-in, got none"
            )
            raise HTTPException(status_code=401, detail=msg)

        access = user.access_token
        if not isinstance(access, AccessToken):
            msg = (
                f"{request.method} {request.url.path}: the sign-in of "
                f"{access.client_id!r} expects a token of this service, got "
                f"{type(access).__name__}"
            )
            raise HTTPException(status_code=401, detail=msg)

        if self._endpoint not in access.scopes:
            msg = (
                f"{request.method} {request.url.path}: mcp endpoint "
                f"{self._endpoint!r} is not granted to {access.client_id!r}, "
                f"the token carries scopes {sorted(access.scopes)}"
            )
            raise HTTPException(status_code=403, detail=msg)

        return self._subjects.of(access)


class CallerBus(SubscriptionBus):
    """Реализация SubscriptionBus SDK mcp с адресатом-пользователем: шина
    подписок одного endpoint'а.

    Создаётся сервером endpoint'а; поток subscriptions/listen (ListenHandler
    SDK) подписывается на неё из своего запроса, и шина запоминает, чей это
    поток (TokenSubjects). Рост канала журнала (ResourceUpdated) уходит всем
    потокам: каждый уже отфильтрован своими проверенными uri. Смена списка
    ресурсов касается одного пользователя — его журналов идущих вызовов, —
    поэтому notify() доставляет её только потокам этого пользователя.
    """

    def __init__(self, subjects: TokenSubjects) -> None:
        self._subjects = subjects
        self._listeners: dict[object, tuple[str, Callable[[ServerEvent], None]]] = {}

    async def publish(self, event: ServerEvent) -> None:
        for _user, listener in list(self._listeners.values()):
            listener(event)

        # уступить ход: потоки подписки успевают отправить событие между
        # публикациями одной задачи
        await asyncio.sleep(0)

    def subscribe(self, listener: Callable[[ServerEvent], None]) -> Callable[[], None]:
        user = self._subjects.current().user_key
        token = object()
        self._listeners[token] = (user, listener)

        def unsubscribe() -> None:
            self._listeners.pop(token, None)

        return unsubscribe

    def notify(self, user: str, event: ServerEvent) -> None:
        """Событие потокам пользователя user."""
        for owner, listener in list(self._listeners.values()):
            if owner != user:
                continue

            listener(event)


@dataclass(frozen=True)
class OpenJournal:
    """Журнал идущего вызова: запуск и идентификатор вызова на сервере."""

    run: str
    node: str


class OpenJournals:
    """Журналы идущих вызовов endpoint'а по пользователям: то, что
    resources/list отдаёт вызывающему (JournalListing).

    Создаётся сервером endpoint'а. Ход вызова (CallProgress) называет здесь
    каждый журнал, едва тот открыт, и снимает журналы запуска, когда вызов
    кончился; каждая перемена — событие ResourcesListChanged потокам
    subscriptions/listen того же пользователя (CallerBus.notify).
    """

    def __init__(self, bus: CallerBus) -> None:
        self._bus = bus
        self._open: dict[str, list[OpenJournal]] = {}

    def opened(self, user: str, journal: OpenJournal) -> None:
        self._open.setdefault(user, []).append(journal)
        self._bus.notify(user, ResourcesListChanged())

    def closed(self, user: str, run: str) -> None:
        """Журналы запуска run больше не идут; запуск без журналов — ничего."""
        held = self._open.get(user, [])
        kept = list(self._kept(held, run))
        if len(kept) == len(held):
            return

        if kept:
            self._open[user] = kept
        else:
            self._open.pop(user, None)

        self._bus.notify(user, ResourcesListChanged())

    def of(self, user: str) -> Sequence[OpenJournal]:
        return tuple(self._open.get(user, ()))

    @staticmethod
    def _kept(held: Iterable[OpenJournal], run: str) -> Iterator[OpenJournal]:
        for journal in held:
            if journal.run == run:
                continue

            yield journal


class JournalListing(Provider):
    """Провайдер fastmcp: каналы журналов идущих вызовов вызывающего —
    ресурсы resources/list.

    Создаётся сервером endpoint'а над OpenJournals. Это путь ядра MCP для
    клиента без расширения журнала: каждый видимый канал открытого журнала —
    ресурс для человека (audience user) по шаблону канала. Читаются ресурсы
    шаблоном журнала (JournalRoutes.read), поэтому найти ресурс по uri
    провайдер не берётся; канал, в который тело ещё не писало, читается
    отказом «ресурса нет».
    """

    def __init__(self, journals: OpenJournals, subjects: TokenSubjects) -> None:
        super().__init__()
        self._journals = journals
        self._subjects = subjects
        self._uris = JournalUris(JournalRoutes.TEMPLATE)

    async def _list_resources(self) -> Sequence[Resource]:
        user = self._subjects.current().user_key

        return list(self._listed(self._journals.of(user)))

    async def _get_resource(
        self, uri: str, version: VersionSpec | None = None
    ) -> Resource | None:
        return None

    async def get_tasks(self) -> Sequence[FastMCPComponent]:
        """Фоновых задач у журналов нет."""
        return []

    def _listed(self, journals: Iterable[OpenJournal]) -> Iterator[Resource]:
        for journal in journals:
            for channel in JournalChannels.VISIBLE:
                read = JournalRead(run=journal.run, node=journal.node, channel=channel)
                yield Resource(
                    uri=AnyUrl(self._uris.uri(read)),
                    name=f"{journal.node}.{channel.value}.log",
                    title=f"{channel.value} of call {journal.node}",
                    mime_type=ContentMime.PLAIN.value,
                    annotations=JournalRoutes.FOR_USER,
                )


class JournalRoutes:
    """Адреса журнала вызова на endpoint'е: канал целым файлом по маршруту
    и ресурсом MCP.

    Создаётся сервером endpoint'а из журналов процесса. Окно канала с
    проверкой доступа (window) — общий путь resources/read и подписки;
    скачать канал целиком — маршрут `{base}/journals/{run}/{node}/{channel}`,
    потоком и с Range. Канал — ресурс по шаблону TEMPLATE: на него ведут
    ссылки итога вызова (без запроса), resources/read отдаёт окно канала от
    offset или перед before, координаты окна — в _meta содержимого под
    ключом JournalFeature.ID. О росте канала сервер сообщает потокам
    subscriptions/listen (listen поверх ListenHandler SDK и шины подписок
    endpoint'а, куда публикует ход вызова CallProgress), о смене списка
    журналов идущих вызовов — событием ResourcesListChanged. Других событий
    сервер не шлёт, и подтверждение потока их не называет. Журнал отдаётся и
    слушается только тем, кто вызывал инструмент: чужой запуск — «ресурса
    нет».
    """

    MIME: ClassVar[str] = "text/plain; charset=utf-8"
    RESOURCE: ClassVar[str] = f"boba-journal://{JournalFeature.ROUTE}"
    """Адрес канала журнала: по нему же стоит маршрут скачивания."""
    TEMPLATE: ClassVar[str] = f"{RESOURCE}{{?offset,before}}"
    """Шаблон uri ресурса канала журнала с окном (JournalFeature.resource)."""
    FOR_USER: ClassVar[mt.Annotations] = mt.Annotations(audience=["user"])
    """Журнал — для человека (панель вызова), а не для модели: так же
    помечены ссылки на каналы в итоге вызова."""

    def __init__(  # noqa: PLR0913 — маршрут собирается всеми входами endpoint'а
        self,
        journals: CallJournals,
        callers: RouteCallers,
        subjects: TokenSubjects,
        config: LocalStorageConfig,
        base: str,
        published: str,
        bus: SubscriptionBus,
    ) -> None:
        self._journals = journals
        self._callers = callers
        self._subjects = subjects
        self._config = config
        self._base = f"{base}/journals"
        self._published = f"{published}{self._base}"
        self._policy = UploadPolicy()
        self._files: dict[str, StreamedFile] = {}
        self._uris = JournalUris(self.TEMPLATE)
        self._listener = ListenHandler(bus)

    def path(self) -> str:
        """Путь маршрута журналов, каким его видит клиент (за прокси — с
        префиксом публикации): настройка расширения журнала."""
        return self._published

    def install(self, server: FastMCP, lowlevel: Server[Any]) -> None:
        path = f"{self._base}/{JournalFeature.ROUTE}"
        server.custom_route(path, methods=["GET"])(self.get)
        server.resource(
            self.TEMPLATE, mime_type=ContentMime.PLAIN.value, annotations=self.FOR_USER
        )(self.read)
        lowlevel.add_request_handler(
            "subscriptions/listen", mt.SubscriptionsListenRequestParams, self.listen
        )

    def close(self) -> None:
        """Штатный конец потоков subscriptions/listen: каждый поток досылает
        события и отвечает на свой запрос итогом."""
        self._listener.close()

    async def listen(
        self,
        ctx: ServerRequestContext[Any, Any],
        params: mt.SubscriptionsListenRequestParams,
    ) -> mt.SubscriptionsListenResult:
        """subscriptions/listen: поток событий роста каналов журнала и смены
        списка журналов идущих вызовов. Каждый uri подписки проверяется тем
        же путём, что resources/read (window): не канал журнала, чужой запуск
        или канала ещё нет — отказ до подтверждения. Смен списков инструментов
        и промптов сервер не шлёт: в подтверждение они не входят.

        Ошибки:
        MCPError — uri подписки не называет доступный вызывающему канал.
        """
        asked = params.notifications
        uris = asked.resource_subscriptions
        if uris is None:
            uris = []

        for uri in uris:
            read = self._uris.address(uri)
            if read is None:
                msg = (
                    f"subscriptions/listen {uri}: expected a journal channel "
                    f"resource {self.RESOURCE}, got another uri"
                )
                raise MCPError(mt.INVALID_PARAMS, msg, {"uri": uri})

            try:
                self.window(read)
            except InvalidResourceError as exc:
                msg = f"subscriptions/listen {uri}: {exc}"
                raise MCPError(mt.INVALID_PARAMS, msg, {"uri": uri}) from exc

        honored = mt.SubscriptionFilter(
            resources_list_changed=asked.resources_list_changed,
            resource_subscriptions=asked.resource_subscriptions,
        )
        supported = params.model_copy(update={"notifications": honored})

        return await self._listener(ctx, supported)

    async def read(
        self,
        run: str,
        node: str,
        channel: str,
        offset: int | None = None,
        before: int | None = None,
    ) -> ResourceResult:
        """resources/read канала журнала: окно от offset или перед before,
        без обоих — первое окно; канал целиком отдаёт маршрут журналов.

        Ошибки:
        InvalidResourceError — адрес канала негоден, канал не читается либо
            журнала нет у вызывающего: fastmcp отвечает -32602.
        """
        address = {
            "run": run,
            "node": node,
            "channel": channel,
            "offset": offset,
            "before": before,
        }
        try:
            request = JournalRead.model_validate(address)
        except ValidationError as exc:
            msg = (
                f"resources/read {run}/{node}/{channel}: expected a journal "
                f"channel address, got {ValidationText.of(exc)}"
            )
            raise self._absent(msg) from exc

        window = self.window(request)
        place = window.model_dump(mode="json", exclude={"text"})
        content = ResourceContent(
            window.text,
            mime_type=ContentMime.PLAIN.value,
            meta={JournalFeature.ID: place},
        )

        return ResourceResult([content])

    def window(self, request: JournalRead) -> StreamSlice:
        """Окно канала журнала для вызывающего: от offset (без него — от
        начала) либо перед before.

        Ошибки:
        InvalidResourceError — канал не читается, запуск чужой либо журнала
            нет.
        """
        address = f"journal of call {request.node!r} in run {request.run!r}"
        if not JournalChannels.visible(request.channel):
            msg = (
                f"{address}: channel {request.channel.value!r} is not readable, "
                "expected one of the visible journal channels"
            )
            raise self._absent(msg)

        user = self._subjects.current().user_key
        owner = self._journals.owner_of(request.run)
        if owner not in (None, user):
            msg = f"{address}: the run belongs to another user"
            raise self._absent(msg)

        piece = self._slice(request, user)
        if piece is None:
            msg = f"{address}: no journal on channel {request.channel.value!r}"
            raise self._absent(msg)

        return piece

    @staticmethod
    def _absent(message: str) -> InvalidResourceError:
        """Отказ «ресурса нет»: fastmcp отвечает на него -32602, как требует
        спецификация, а трассу пишет только в отладочный лог."""
        return InvalidResourceError(message, log_level=logging.DEBUG)

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

    async def get(self, request: Request) -> Response:
        subject = await self._callers.of(request)
        params = request.path_params
        run = str(params["run"])
        node = str(params["node"])
        store = self._journals.store
        if store is None:
            msg = f"GET {request.url.path}: the stream journal is disabled"
            raise HTTPException(status_code=404, detail=msg)

        channel = JournalChannels.parse_visible(str(params["channel"]))
        if channel is None:
            visible = ", ".join(str(item) for item in JournalChannels.VISIBLE)
            msg = (
                f"GET {request.url.path}: channel {params['channel']!r} is not "
                f"downloadable, visible channels: {visible}"
            )
            raise HTTPException(status_code=404, detail=msg)

        try:
            key = StreamKey(user_id=subject.user_key, run_id=run, call_id=node)
            root = store.vault_root(key.user_id)
        except (ValueError, StreamJournalError) as exc:
            msg = f"GET {request.url.path}: no journal of call {node!r}: {exc}"
            raise HTTPException(status_code=404, detail=msg) from exc

        rel_log = store.log_rel_path(key, channel)
        if rel_log is None:
            msg = (
                f"GET {request.url.path}: call {node!r} of run {run!r} has no "
                f"journal on channel {channel.value!r}"
            )
            raise HTTPException(status_code=404, detail=msg)

        name = f"{key.call_id}.{channel.value}.log"
        disposition = ContentDisposition.ATTACHMENT.header(name)

        return await self._files_for(root).respond(
            rel_log,
            mime=self.MIME,
            range_header=request.headers.get("range", ""),
            content_disposition=disposition,
        )

    def _files_for(self, root: str) -> StreamedFile:
        files = self._files.get(root)
        if files is not None:
            return files

        config = self._config.model_copy(update={"kind": "local", "files_dir": root})
        files = StreamedFile(LocalStorageClient(config), self._policy)
        self._files[root] = files

        return files


class FileRoutes:
    """Маршруты файлов workspace одного endpoint'а.

    Создаётся сервером endpoint'а (McpServer) из хранилища workspace и
    разбора вошедшего (RouteCallers); install() ставит маршруты в сервер
    fastmcp. feature() — настройки расширения FilesFeature, по которым
    клиент узнаёт адрес маршрута: путь с префиксом публикации published,
    под которым сервис стоит за прокси (без прокси — пустым).
    """

    def __init__(
        self,
        storage: StorageClient,
        journals: CallJournals,
        callers: RouteCallers,
        base: str,
        published: str,
    ) -> None:
        self._storage = storage
        self._journals = journals
        self._callers = callers
        self._base = f"{base}/files"
        self._published = f"{published}{self._base}"
        self._policy = UploadPolicy()
        self._files = StreamedFile(storage, self._policy)

    def feature(self, upload_tool: str) -> FilesFeature:
        """Настройки расширения файлов: маршрут, инструмент адреса загрузки
        upload_tool и каталог workspace глазами инструментов."""
        return FilesFeature(
            path=self._published,
            upload=upload_tool,
            workspace=WorkspaceMount.path(),
        )

    def address(self, scope: str, name: str) -> str:
        """Путь маршрута, куда клиент шлёт файл вложением области scope."""
        route = FilesFeature.ROUTE.format(
            scope=scope, dir=ScopeDir.UPLOAD.value, name=name
        )

        return f"{self._published}/{route}"

    def install(self, server: FastMCP) -> None:
        path = f"{self._base}/{FilesFeature.ROUTE}"
        # HEAD стоит раньше GET: маршрут GET у starlette отвечает и на HEAD
        server.custom_route(path, methods=["HEAD"])(self.head)
        server.custom_route(path, methods=["GET"])(self.get)
        server.custom_route(path, methods=["PUT"])(self.put)
        server.custom_route(path, methods=["DELETE"])(self.delete)
        area = f"{self._base}/{FilesFeature.AREA}"
        server.custom_route(area, methods=["DELETE"])(self.delete_scope)

    async def put(self, request: Request) -> Response:
        """Запись файла потоком: чанки тела запроса идут прямо в хранилище."""
        key = await self._key(request)
        counted = CountedBody(request.stream())
        try:
            await self._storage.upload_stream(
                key.render(), counted.chunks(), ContentMime.of_name(key.name)
            )
        except StorageFullError as exc:
            msg = f"PUT {request.url.path}: no space left in the workspace: {exc}"
            raise HTTPException(
                status_code=self._policy.no_space_status, detail=msg
            ) from exc

        logger.info("files: %s stored, %d bytes", key.render(), counted.size)
        stored = FileStored(path=key.in_workspace(), size=counted.size)

        return JSONResponse(stored.model_dump(mode="json"), status_code=201)

    async def get(self, request: Request) -> Response:
        """Чтение файла потоком; заголовок Range отвечает 206."""
        key = await self._key(request)

        return await self._files.respond(
            key.render(),
            mime=ContentMime.of_name(key.name),
            range_header=request.headers.get("range", ""),
            content_disposition="",
        )

    async def head(self, request: Request) -> Response:
        """Размер и версия файла без тела: по ним клиент следит за правкой."""
        key = await self._key(request)
        try:
            stat = await self._storage.stat(key.render())
        except StorageNotFoundError as exc:
            msg = f"HEAD {request.url.path}: no such file in the workspace: {exc}"
            raise HTTPException(status_code=404, detail=msg) from exc

        headers = {
            FileHeader.CONTENT_LENGTH.value: str(stat.size),
            FileHeader.ACCEPT_RANGES.value: StreamedFile.ACCEPT_RANGES,
            FilePart.ETAG.value: f'"{stat.revision}"',
        }

        return Response(
            status_code=200, headers=headers, media_type=ContentMime.of_name(key.name)
        )

    async def delete(self, request: Request) -> Response:
        key = await self._key(request)
        if not await self._storage.delete_file(key.render()):
            msg = f"DELETE {request.url.path}: no such file in the workspace"
            raise HTTPException(status_code=404, detail=msg)

        return Response(status_code=204)

    async def delete_scope(self, request: Request) -> Response:
        """Область вошедшего целиком: файлы её каталогов и журналы вызовов её
        запусков; область без файлов и журналов — успех."""
        subject = await self._callers.of(request)
        try:
            scope = Scope.caller(str(request.path_params[FilePart.SCOPE.value]))
        except ValidationError as exc:
            msg = (
                f"DELETE {request.url.path}: the address does not name a scope: "
                f"{ValidationText.of(exc)}"
            )
            raise HTTPException(status_code=400, detail=msg) from exc

        await self._storage.delete_scope(subject.user_key, scope.id)

        freed = 0
        if store := self._journals.store:
            try:
                freed = store.purge_scope(subject.user_key, scope.id)
            except StreamJournalError as exc:
                msg = (
                    f"DELETE {request.url.path}: files of scope {scope.id} are "
                    f"deleted, its call journals are not: {exc}"
                )
                raise HTTPException(status_code=500, detail=msg) from exc

        logger.info(
            "files: scope %s of %s deleted, %d bytes of call journals freed",
            scope.id,
            subject.user_key,
            freed,
        )

        return Response(status_code=204)

    async def _key(self, request: Request) -> ObjectKey:
        """Ключ файла в workspace вошедшего по адресу запроса."""
        subject = await self._callers.of(request)
        params = request.path_params
        try:
            scope = Scope.caller(str(params[FilePart.SCOPE.value]))

            return ObjectKey(
                user_id=subject.user_key,
                scope_id=scope.id,
                name=str(params[FilePart.NAME.value]),
                dir=ScopeDir(str(params[FilePart.DIR.value])),
            )
        except ValidationError as exc:
            msg = (
                f"{request.method} {request.url.path}: the address does not name "
                f"a file of a scope: {ValidationText.of(exc)}"
            )
            raise HTTPException(status_code=400, detail=msg) from exc
        except ValueError as exc:
            dirs = sorted(ScopeDir)
            msg = (
                f"{request.method} {request.url.path}: expected one of the scope "
                f"dirs {dirs}: {exc}"
            )
            raise HTTPException(status_code=400, detail=msg) from exc


class CountedBody:
    """Тело запроса чанками со счётом принятых байт. Создаётся маршрутом
    записи на один запрос; пустые чанки конца потока в хранилище не идут."""

    def __init__(self, source: AsyncIterator[bytes]) -> None:
        self._source = source
        self.size = 0

    async def chunks(self) -> AsyncIterator[bytes]:
        async for chunk in self._source:
            if not chunk:
                continue

            self.size += len(chunk)
            yield chunk
