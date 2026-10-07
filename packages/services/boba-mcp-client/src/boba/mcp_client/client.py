"""Порт сервера инструментов над MCP-сервером.

Клиент работает с любым сервером стандарта MCP: берёт список инструментов
(tools/list), строит по их схемам карточки инструментов для модели и
исполняет вызовы (tools/call). Транспорт — streamable HTTP, SSE или stdio. Итог вызова
сервера, который не знает о семействе результатов boba, — текст его content;
сервер boba-mcp присылает ещё и модель результата, и она оживает как есть.
Сервер с авторизацией proxy (сервис boba) подключается от имени пользователя
сессии: клиент называет его логин и роли заголовками под подписью и получает
токен сервера.

Ошибки:
McpClientError — сервер недоступен, не прошёл инициализацию, не отдал список
    инструментов либо порт позвали до open().
Сбой самого вызова наружу не выходит: он возвращается итогом-ошибкой вызова.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from abc import abstractmethod
from collections import OrderedDict
from collections.abc import (
    AsyncGenerator,
    AsyncIterator,
    Awaitable,
    Callable,
    Iterable,
    Mapping,
    Sequence,
)
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from enum import StrEnum
from functools import partial
from typing import Annotated, Any, ClassVar, Literal, Protocol, TypeVar
from urllib.parse import quote
from uuid import uuid4

import httpx2
import mcp.types as mt
from mcp import Client, ClientSession, StdioServerParameters
from mcp.client.auth import TokenStorage
from mcp.client.auth.extensions.identity_assertion import (
    IdentityAssertionOAuthProvider,
)
from mcp.client.extension import ClientExtension, advertise
from mcp.client.sse import sse_client
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client
from mcp.shared._stream_protocols import ReadStream, WriteStream
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from mcp.shared.message import SessionMessage
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    SecretStr,
    ValidationError,
)

from boba.auth.proxy import ProxyAssertions
from boba.canvas.journal import StreamSlice
from boba.canvas.keys import ObjectKey, WorkspaceMount
from boba.canvas.storage import (
    FileStat,
    OpenedStream,
    StorageError,
    StorageFullError,
    StorageNotFoundError,
)
from boba.identity.context import CallContexts
from boba.toolkit.calls import CallViews
from boba.toolkit.chain import StreamPlanError
from boba.toolkit.dag import (
    CallDag,
    DagNode,
    NodeCalls,
    NodeOutcome,
    NodeOutcomes,
    ToolCard,
    ToolServer,
    WorkflowNodeResult,
    WorkflowResult,
)
from boba.toolkit.result import (
    ErrorResult,
    MarkdownResult,
)
from boba.toolkit.wire import (
    FilesFeature,
    JournalFeature,
    JournalRead,
    ResultWire,
    WireResult,
)
from boba.workspace.launcher import ReadWindow

__all__ = [
    "BearerAuth",
    "BlockFiles",
    "CallSignals",
    "DroppedSignals",
    "HttpEndpoint",
    "HttpLocation",
    "McpCaller",
    "McpClientError",
    "McpServerConfig",
    "McpServers",
    "McpServersConfig",
    "McpToolServer",
    "NamedBlocks",
    "NoAuth",
    "ProgressListener",
    "ProxyAuth",
    "SseEndpoint",
    "StdioCommand",
]

logger = logging.getLogger(__name__)

FeatureModel = TypeVar("FeatureModel", bound=BaseModel)
"""Модель настроек расширения сервера."""

Streams = tuple[ReadStream[SessionMessage | Exception], WriteStream[SessionMessage]]
"""Пара потоков сообщений транспорта MCP: чтение и запись."""


class McpClientError(Exception):
    """С MCP-сервером не удалось договориться: соединение, инициализация,
    список инструментов."""


class McpFailure(StrEnum):
    """Виды отказа вызова, которые собирает сам клиент."""

    TOOL_ERROR = "mcp_tool_error"
    TRANSPORT = "mcp_transport_error"


@dataclass(frozen=True)
class McpCaller:
    """От чьего имени сессия клиента ходит на сервер: логин пользователя
    (его identifier в клиенте) и роли, с которыми он вошёл в клиент. Сервер
    с авторизацией proxy получает их при входе и по ним решает, какие его
    endpoint'ы пользователю доступны."""

    login: str
    roles: frozenset[str]

    def __post_init__(self) -> None:
        if not self.login:
            msg = "mcp caller: login expects a non-empty string, got an empty one"
            raise ValueError(msg)


class HttpLocation(BaseModel):
    """Адрес HTTP частями, как у HTTP-соединений."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    scheme: Literal["http", "https"]
    host: str = Field(min_length=1)
    port: int | None = Field(default=None, gt=0, lt=65536)
    path: str = Field(min_length=1)

    def url(self) -> str:
        return str(
            httpx2.URL(
                scheme=self.scheme, host=self.host, port=self.port, path=self.path
            )
        )


class NoAuth(BaseModel):
    """Сервер без авторизации."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    PERSONAL: ClassVar[bool] = False

    auth: Literal["none"] = "none"

    def httpx_auth(self, caller: McpCaller | None, resource: str) -> httpx2.Auth | None:
        return None


class BearerToken(httpx2.Auth):
    """Готовый токен заголовком Authorization каждого запроса."""

    def __init__(self, token: SecretStr) -> None:
        self._token = token

    def auth_flow(self, request: httpx2.Request) -> Any:
        request.headers["Authorization"] = f"Bearer {self._token.get_secret_value()}"
        yield request


class BearerAuth(BaseModel):
    """Готовый токен доступа из конфига: один на всех пользователей клиента."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    PERSONAL: ClassVar[bool] = False

    auth: Literal["bearer"] = "bearer"
    token: SecretStr

    def httpx_auth(self, caller: McpCaller | None, resource: str) -> httpx2.Auth | None:
        return BearerToken(self.token)


class ProxyAuth(BaseModel):
    """Вход proxy сервиса boba от имени пользователя сессии.

    Клиент — доверенный бэкенд: он уже проверил пользователя и утверждает
    серверу авторизации issuer его логин (identifier пользователя в клиенте)
    и роли, с которыми тот вошёл в клиент, — подписанным JWT под общим
    ключом secret ([auth.proxy].secret сервера). Утверждение меняется на
    токен доступа штатным обменом OAuth (grant jwt-bearer); клиента сервер
    узнаёт по client_id и client_secret ([mcp.clients] сервера). Токен свой
    у каждого пользователя, поэтому и подключение к серверу у каждого своё.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    PERSONAL: ClassVar[bool] = True

    auth: Literal["proxy"] = "proxy"
    issuer: HttpLocation
    client_id: str = Field(min_length=1)
    client_secret: SecretStr
    secret: SecretStr

    def httpx_auth(self, caller: McpCaller | None, resource: str) -> httpx2.Auth | None:
        if caller is None:
            msg = (
                f"mcp auth proxy for {resource}: the connection is opened "
                "without a caller, expected the login and roles of the session user"
            )
            raise McpClientError(msg)

        assertions = CallerAssertions(
            ProxyAssertions(self.secret.get_secret_value(), 0), self.client_id, caller
        )

        return IdentityAssertionOAuthProvider(
            server_url=resource,
            storage=HeldTokens(),
            client_id=self.client_id,
            client_secret=self.client_secret.get_secret_value(),
            issuer=self.issuer.url(),
            assertion_provider=assertions.issue,
        )


class CallerAssertions:
    """Утверждения о пользователе сессии для обмена на токен сервера.

    Создаётся ProxyAuth на подключение пользователя; штатный клиент обмена
    (IdentityAssertionOAuthProvider) зовёт issue() перед каждым обменом и
    получает свежее утверждение.
    """

    def __init__(
        self, assertions: ProxyAssertions, client_id: str, caller: McpCaller
    ) -> None:
        self._assertions = assertions
        self._client_id = client_id
        self._caller = caller

    async def issue(self, audience: str, resource: str) -> str:
        roles = ",".join(sorted(self._caller.roles))

        return self._assertions.issue(
            self._client_id, self._caller.login, roles, audience, resource
        )


class HeldTokens(TokenStorage):
    """Токен сервера одного подключения в памяти: реализация TokenStorage
    клиента mcp. Подключение своё у каждого пользователя, переживать процесс
    токену незачем — обмен утверждения повторяется сам."""

    def __init__(self) -> None:
        self._tokens: OAuthToken | None = None
        self._client: OAuthClientInformationFull | None = None

    async def get_tokens(self) -> OAuthToken | None:
        return self._tokens

    async def set_tokens(self, tokens: OAuthToken) -> None:
        self._tokens = tokens

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        return self._client

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        self._client = client_info


McpAuth = Annotated[NoAuth | BearerAuth | ProxyAuth, Field(discriminator="auth")]


class Transport(Protocol):
    """Транспорт MCP-сервера: открывает потоки сообщений сессии."""

    @abstractmethod
    def opened(self, caller: McpCaller | None) -> AbstractAsyncContextManager[Streams]:
        """Потоки сессии; caller — пользователь, от чьего имени она идёт
        (None — подключение общее для всех сессий)."""

    @abstractmethod
    def label(self) -> str:
        """Адрес сервера для сообщений об ошибках и логов."""


class HttpRefusals:
    """Отказы HTTP одного подключения к серверу.

    Создаётся транспортом HttpEndpoint на каждое открытие и ставится
    наблюдателем ответов его HTTP-клиента. Библиотека mcp заменяет ответ с
    отказом (401, 403, 502) безликой ошибкой протокола; по последнему
    отказу запроса POST транспорт возвращает в текст ошибки адрес и статус.
    Отказ на GET не считается: им сервер без потока уведомлений отвечает
    исправной сессии.
    """

    FIRST_REFUSAL: ClassVar[int] = 400
    REQUEST: ClassVar[str] = "POST"

    def __init__(self) -> None:
        self._last: str | None = None

    async def seen(self, response: httpx2.Response) -> None:
        request = response.request
        if request.method != self.REQUEST:
            return

        if response.status_code < self.FIRST_REFUSAL:
            self._last = None
            return

        self._last = (
            f"{request.method} {request.url}: expected a reply of the MCP server, "
            f"got HTTP {response.status_code} {response.reason_phrase}"
        )

    def last(self) -> str | None:
        return self._last


class HttpAddress(HttpLocation):
    """Адрес HTTP-сервера MCP: место и способ авторизации."""

    auth: McpAuth = NoAuth()

    def personal(self) -> bool:
        """Подключение своё у каждого пользователя: токен выдаётся ему."""
        return self.auth.PERSONAL


class HttpEndpoint(HttpAddress):
    """Сервер по streamable HTTP. Модель конфига; транспорт — она же
    (протокол Transport наследовать нельзя: метакласс pydantic)."""

    transport: Literal["streamable-http"] = "streamable-http"

    def label(self) -> str:
        return self.url()

    @asynccontextmanager
    async def opened(self, caller: McpCaller | None) -> AsyncIterator[Streams]:
        auth = self.auth.httpx_auth(caller, self.url())
        refusals = HttpRefusals()
        async with create_mcp_http_client(auth=auth) as http:
            http.event_hooks["response"].append(refusals.seen)
            async with streamable_http_client(self.url(), http_client=http) as streams:
                try:
                    yield streams
                except Exception as exc:
                    refused = refusals.last()
                    if refused is None:
                        raise

                    raise McpClientError(refused) from exc


class SseEndpoint(HttpAddress):
    """Сервер по SSE (прежний HTTP-транспорт MCP). Модель конфига; транспорт
    — она же (протокол Transport)."""

    transport: Literal["sse"] = "sse"

    def label(self) -> str:
        return self.url()

    @asynccontextmanager
    async def opened(self, caller: McpCaller | None) -> AsyncIterator[Streams]:
        auth = self.auth.httpx_auth(caller, self.url())
        async with sse_client(self.url(), auth=auth) as streams:
            yield streams


class StdioCommand(BaseModel):
    """Сервер-субпроцесс со стандартным вводом и выводом. Модель конфига;
    транспорт — она же (протокол Transport)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    transport: Literal["stdio"] = "stdio"
    command: str = Field(min_length=1)
    args: tuple[str, ...] = ()
    env: Mapping[str, str] = Field(default_factory=dict)
    cwd: str | None = None

    def label(self) -> str:
        return " ".join((self.command, *self.args))

    def personal(self) -> bool:
        return False

    @asynccontextmanager
    async def opened(self, caller: McpCaller | None) -> AsyncIterator[Streams]:
        env: dict[str, str] | None = None
        if self.env:
            env = dict(self.env)

        params = StdioServerParameters(
            command=self.command, args=list(self.args), env=env, cwd=self.cwd
        )
        async with stdio_client(params) as streams:
            yield streams


McpEndpoint = Annotated[
    HttpEndpoint | SseEndpoint | StdioCommand, Field(discriminator="transport")
]


class McpServerConfig(BaseModel):
    """Один MCP-сервер в конфиге клиента: секция [mcp.servers.<имя>].

    Адрес сервера (endpoint) и способ авторизации в нём. Сервер подключён
    тем сессиям, чей профиль клиента его называет; набор инструментов решает
    сам сервер. prefix добавляется к именам инструментов: им разводят
    серверы с совпавшими именами. Сроки — на подключение с инициализацией
    и на один вызов.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    endpoint: McpEndpoint
    prefix: str = ""
    connect_timeout_sec: float = Field(gt=0)
    call_timeout_sec: float = Field(gt=0)


class McpServersConfig(BaseModel):
    """Секция [mcp]: MCP-серверы клиента по именам."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    servers: Mapping[str, McpServerConfig]


class ErrorCauses:
    """Причины сбоя одной строкой: группа исключений anyio раскрывается до
    своих листьев, иначе в тексте остаётся «unhandled errors in a TaskGroup».
    Создаётся соединением и переводчиком результатов."""

    def text(self, error: BaseException) -> str:
        return "; ".join(self._leaves(error))

    def _leaves(self, error: BaseException) -> list[str]:
        if isinstance(error, BaseExceptionGroup):
            leaves: list[str] = []
            for inner in error.exceptions:
                leaves.extend(self._leaves(inner))

            return leaves

        return [f"{type(error).__name__}: {error}"]


class McpConnection:
    """Живая сессия MCP с одним сервером.

    Создаётся портом McpToolServer. Транспорт и сессия библиотеки mcp
    открываются и закрываются в одной собственной задаче — их области отмены
    привязаны к задаче, которая их открыла; запросы идут из задач вызовов.
    """

    CLIENT: ClassVar[mt.Implementation] = mt.Implementation(
        name="boba-chat", version="1"
    )

    EXTENSIONS: ClassVar[Sequence[ClientExtension]] = (advertise(JournalFeature.ID),)
    """Расширения, которые клиент объявляет серверу: журнал вызовов он
    читает окнами и слушает сигналы его роста."""

    def __init__(
        self,
        transport: Transport,
        caller: McpCaller | None,
        connect_timeout_sec: float,
    ) -> None:
        self._transport = transport
        self._caller = caller
        self._connect_timeout_sec = connect_timeout_sec
        self._causes = ErrorCauses()
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._ready: asyncio.Future[ClientSession] | None = None

    async def open(self) -> ClientSession:
        """Сессия с согласованным протоколом; повторный вызов отдаёт уже
        открытую."""
        if self._ready is None:
            self._stop = asyncio.Event()
            self._ready = asyncio.get_running_loop().create_future()
            self._task = asyncio.create_task(
                self._serve(self._ready), name=f"mcp:{self._transport.label()}"
            )

        try:
            return await asyncio.wait_for(
                asyncio.shield(self._ready), self._connect_timeout_sec
            )
        except TimeoutError as exc:
            await self.close()
            msg = (
                f"mcp {self._transport.label()}: the protocol was not negotiated "
                f"within {self._connect_timeout_sec}s"
            )
            raise McpClientError(msg) from exc
        except McpClientError:
            # неудачная попытка не запоминается: следующий open() подключается заново
            await self.close()
            raise

    async def close(self) -> None:
        task = self._task
        self._task = None
        self._ready = None
        self._stop.set()
        if task is None:
            return

        try:
            await task
        except Exception as exc:
            logger.info(
                "mcp %s: the session closed with %s: %s",
                self._transport.label(),
                type(exc).__name__,
                exc,
            )

    async def _serve(self, ready: asyncio.Future[ClientSession]) -> None:
        """Сессия живёт в этой задаче. Протокол согласует библиотека: сервер
        новой спецификации отвечает на server/discover, прежний — на
        initialize."""
        stop = self._stop
        try:
            client = Client(
                self._transport.opened(self._caller),
                client_info=self.CLIENT,
                extensions=self.EXTENSIONS,
                cache=None,
            )
            async with client:
                ready.set_result(client.session)
                await stop.wait()
        except Exception as exc:
            if not ready.done():
                msg = (
                    f"mcp {self._transport.label()}: connecting and initializing "
                    f"failed: {self._causes.text(exc)}"
                )
                ready.set_exception(McpClientError(msg))
                return

            raise


@dataclass(frozen=True)
class RelayedFile:
    """Ответ сервера на чтение файла как он есть: статус, заголовки тела и
    чанки. Так клиент отдаёт файл дальше, не разбирая его; release закрывает
    соединение с сервером."""

    status: int
    headers: Mapping[str, str]
    chunks: AsyncGenerator[bytes, None]
    release: Callable[[], Awaitable[None]]


class McpFiles:
    """Файлы пользователя на сервере boba-mcp под одним маршрутом: файлы
    workspace либо журналы вызовов.

    Создаётся портом McpToolServer, когда сервер объявил расширение
    (FilesFeature, JournalFeature), из адреса сервера, пути маршрута и
    пользователя, чьим именем идёт подключение. Байты идут потоком в обе стороны: запись
    шлёт чанки источника телом PUT, чтение отдаёт чанки ответа GET окном
    Range. Владельца workspace сервер берёт из токена входа, поэтому из
    ключа файла на сервер уходят только область, каталог и имя.

    Ошибки те же, что у хранилища: StorageNotFoundError — файла нет,
    StorageFullError — в workspace нет места, StorageError — прочий отказ.
    """

    BODY_HEADERS: ClassVar[tuple[str, ...]] = (
        "content-type",
        "content-length",
        "content-range",
        "accept-ranges",
        "content-disposition",
    )
    """Заголовки ответа сервера, описывающие тело файла."""

    NOT_FOUND: ClassVar[int] = 404
    BAD_RANGE: ClassVar[int] = 416
    NO_SPACE: ClassVar[int] = 507
    FIRST_REFUSAL: ClassVar[int] = 400
    CHUNK_BYTES: ClassVar[int] = 1024 * 1024

    def __init__(
        self, server: str, endpoint: HttpAddress, caller: McpCaller | None, path: str
    ) -> None:
        self._server = server
        self._root = HttpLocation(
            scheme=endpoint.scheme, host=endpoint.host, port=endpoint.port, path=path
        )
        auth = endpoint.auth.httpx_auth(caller, endpoint.url())
        self._http = create_mcp_http_client(auth=auth)
        # сжатый прокси ответ идёт без Content-Length и не тем окном Range
        self._http.headers["Accept-Encoding"] = "identity"

    async def close(self) -> None:
        await self._http.aclose()

    async def upload(self, key: ObjectKey, source: AsyncIterator[bytes]) -> int:
        """Пишет файл потоком; возвращает число принятых сервером байт."""
        url = self._url(key)
        # вход клиента получает токен по отказу 401, а тело потока второй раз
        # не отправить: токен берёт запрос без тела перед записью
        await self._http.head(url)
        reply = await self._http.put(url, content=source)
        self._check("PUT", url, reply)
        try:
            return FileStored.model_validate(reply.json()).size
        except (ValidationError, ValueError) as exc:
            msg = (
                f"PUT {url}: expected the stored file as JSON with path and size, "
                f"got {reply.text[:200]!r}: {exc}"
            )
            raise StorageError(msg) from exc

    async def stat(self, key: ObjectKey) -> FileStat:
        url = self._url(key)
        reply = await self._http.head(url)
        self._check("HEAD", url, reply)

        return FileStat(size=self._length(url, reply), revision=self._revision(reply))

    async def open(self, key: ObjectKey, window: ReadWindow) -> OpenedStream:
        """Открывает файл на чтение окном: размер известен до первого байта."""
        url = self._url(key)
        request = self._http.build_request("GET", url, headers=self._range(window))
        reply = await self._http.send(request, stream=True)
        if reply.status_code == self.BAD_RANGE:
            size = self._total(url, reply)
            await reply.aclose()

            return OpenedStream(
                stat=FileStat(size=size), chunks=self._nothing(), release=self._idle
            )

        if reply.status_code >= self.FIRST_REFUSAL:
            await reply.aread()
            await reply.aclose()
            self._check("GET", url, reply)

        stat = FileStat(size=self._total(url, reply))

        return OpenedStream(stat=stat, chunks=self._chunks(reply), release=reply.aclose)

    async def relay(self, rel: str, range_header: str) -> RelayedFile:
        """Файл по пути rel под маршрутом как ответ сервера: статус 200, 206
        или 416 и заголовки тела уходят дальше как есть."""
        url = "/".join((self._root.url(), quote(rel)))
        headers: dict[str, str] = {}
        if range_header:
            headers["Range"] = range_header

        request = self._http.build_request("GET", url, headers=headers)
        reply = await self._http.send(request, stream=True)
        refused = reply.status_code >= self.FIRST_REFUSAL
        if refused and reply.status_code != self.BAD_RANGE:
            await reply.aread()
            await reply.aclose()
            self._check("GET", url, reply)

        passed: dict[str, str] = {}
        for name in self.BODY_HEADERS:
            value = reply.headers.get(name)
            if value is not None:
                passed[name] = value

        return RelayedFile(
            status=reply.status_code,
            headers=passed,
            chunks=self._chunks(reply),
            release=reply.aclose,
        )

    async def delete(self, key: ObjectKey) -> bool:
        url = self._url(key)
        reply = await self._http.delete(url)
        if reply.status_code == self.NOT_FOUND:
            return False

        self._check("DELETE", url, reply)

        return True

    def _url(self, key: ObjectKey) -> str:
        return "/".join((self._root.url(), quote(key.in_thread())))

    def _check(self, method: str, url: str, reply: httpx2.Response) -> None:
        if reply.status_code < self.FIRST_REFUSAL:
            return

        text = (
            f"{method} {url} of mcp server {self._server!r}: got HTTP "
            f"{reply.status_code}: {reply.text[:200]}"
        )
        if reply.status_code == self.NOT_FOUND:
            raise StorageNotFoundError(text)

        if reply.status_code == self.NO_SPACE:
            raise StorageFullError(text)

        raise StorageError(text)

    @staticmethod
    def _range(window: ReadWindow) -> dict[str, str]:
        if window.length is None:
            if window.offset == 0:
                return {}

            return {"Range": f"bytes={window.offset}-"}

        last = window.offset + max(window.length, 1) - 1

        return {"Range": f"bytes={window.offset}-{last}"}

    def _length(self, url: str, reply: httpx2.Response) -> int:
        declared = reply.headers.get("content-length", "")
        if not declared.isdigit():
            msg = f"{url}: expected Content-Length as a number, got {declared!r}"
            raise StorageError(msg)

        return int(declared)

    def _total(self, url: str, reply: httpx2.Response) -> int:
        """Полный размер файла: из Content-Range окна, иначе длина тела."""
        ranged = reply.headers.get("content-range", "")
        _span, slash, total = ranged.rpartition("/")
        if slash and total.isdigit():
            return int(total)

        return self._length(url, reply)

    @staticmethod
    def _revision(reply: httpx2.Response) -> int:
        tag = reply.headers.get("etag", "").strip('"')
        if not tag.isdigit():
            return 0

        return int(tag)

    async def _chunks(self, reply: httpx2.Response) -> AsyncGenerator[bytes, None]:
        async for chunk in reply.aiter_bytes(self.CHUNK_BYTES):
            yield chunk

    @staticmethod
    async def _nothing() -> AsyncGenerator[bytes, None]:
        """Тело окна за концом файла: чанков нет."""
        empty: tuple[bytes, ...] = ()
        for chunk in empty:
            yield chunk

    @staticmethod
    async def _idle() -> None:
        return


class FileStored(BaseModel):
    """Ответ сервера на запись файла: путь в workspace и принятый размер."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    path: str
    size: int = Field(ge=0)


class McpToolStubs:
    """Инструменты модели по списку инструментов MCP-сервера.

    Создаётся портом McpToolServer. Карточка несёт имя инструмента у
    модели (с приставкой сервера), описание, схему аргументов сервера как
    есть и вид аргументов из метки схемы (CallViews); вызов исполняет порт
    запросом к серверу.
    """

    def __init__(self, prefix: str) -> None:
        self._prefix = prefix
        self._views = CallViews()

    def name_of(self, tool: mt.Tool) -> str:
        return f"{self._prefix}{tool.name}"

    def remote_name(self, name: str) -> str:
        """Имя инструмента на сервере по его имени у модели."""
        return name.removeprefix(self._prefix)

    def local_name(self, remote: str) -> str:
        """Имя инструмента у модели по его имени на сервере."""
        return f"{self._prefix}{remote}"

    def card(self, tool: mt.Tool) -> ToolCard:
        """Ошибки:
        McpClientError — вид аргументов в схеме инструмента не проходит
            модель FieldView.
        """
        description = tool.description
        if not description:
            description = tool.name

        try:
            views = self._views.read(tool.input_schema)
        except ValidationError as exc:
            msg = (
                f"mcp tool {self.name_of(tool)!r}: the call view in its schema "
                f"(key {CallViews.MARK!r}) expects placement and display per "
                f"argument: {exc}"
            )
            raise McpClientError(msg) from exc

        return ToolCard(
            name=self.name_of(tool),
            description=description,
            parameters=dict(tool.input_schema),
            views=views,
        )


class BlockFiles(Protocol):
    """Куда клиент кладёт нетекстовые блоки результата: картинки, звук,
    вложенные файлы.

    Реализует хозяин клиента: чат сохраняет блок файлом в workspace треда и
    показывает вложением. Итог — строка для модели о том, где теперь файл.
    """

    @abstractmethod
    async def attached(self, call: DagNode, index: int, mime: str, data: bytes) -> str:
        """Принять блок номер index результата вызова call."""


class NamedBlocks(BlockFiles):
    """Реализация BlockFiles без хранилища: блок только называется в тексте
    результата. Ей пользуется клиент, которому некуда класть файлы."""

    async def attached(self, call: DagNode, index: int, mime: str, data: bytes) -> str:
        return f"[{mime}, {len(data)} bytes]"


class LiveCall:
    """Вызов, чьё тело сейчас исполняется: сам вызов и связка, в которой
    он идёт. Создаётся портом на время вызова."""

    def __init__(self, call: DagNode, seat: LinkedSeat | None) -> None:
        self.call = call
        self.seat = seat
        self.joined = False
        """Тело вызова присоединилось к связке."""


@dataclass(frozen=True)
class LinkedSeat:
    """Место вызова в связке: сама связка, номер узла по порядку описания и
    имя узла, каким его назвал автор связки."""

    group: LinkedCalls
    index: int
    key: str


@dataclass(frozen=True)
class LinkedNode:
    """Вызов связки, готовый к отправке: имя инструмента на сервере,
    аргументы и приёмник сигналов его журнала."""

    call: DagNode
    remote: str
    arguments: Mapping[str, JsonValue]
    listener: ProgressListener
    index: int
    key: str


@dataclass(frozen=True)
class LinkedOutcome:
    """Итог связки: итог её вызова на сервере и результаты узлов по
    идентификаторам их вызовов. Вызов, которого среди узлов нет, получает
    общий итог связки. Запуск у всех вызовов связки один, а журнал у узла
    свой: сервер ведёт его под идентификатором вызова узла, который клиент
    назвал в описании связки."""

    shared: NodeOutcome
    nodes: Mapping[str, WorkflowNodeResult]

    def of(self, call: DagNode) -> NodeOutcome:
        """Итог вызова call, шедшего в связке."""
        own = self.nodes.get(call.key)
        if own is None:
            return self.shared.model_copy(update={"key": call.key, "tool": call.tool})

        journal = self.shared.journal
        if journal is not None:
            journal = journal.model_copy(update={"call": call.key})

        return self.shared.model_copy(
            update={
                "key": call.key,
                "tool": call.tool,
                "content": own.content,
                "artifact": own.result,
                "errored": own.errored,
                "journal": journal,
            }
        )


LinkedSend = Callable[[Sequence[LinkedNode]], Awaitable[LinkedOutcome]]
"""Отправка связки серверу одним вызовом."""


class LinkedCalls:
    """Вызовы инструментов, уходящие серверу одной связкой.

    Создаётся портом McpToolServer на узлы вызова инструмента-связки.
    Потоковым инструментам нужны партнёры на другом конце канала; сервер
    связывает их только внутри одного вызова инструмента-связки. Каждый
    вызов связки идёт путём вызова узла (NodeCalls) до своего тела — у него
    свой шаг ленты и свой журнал, — там присоединяется к связке и ждёт её
    итога; связка уходит серверу, когда присоединились все, кто не выбыл
    раньше.
    """

    def __init__(self, size: int, send: LinkedSend) -> None:
        self._size = size
        self._send = send
        self._nodes: list[LinkedNode] = []
        self._left = 0
        self._done: asyncio.Future[LinkedOutcome]
        self._done = asyncio.get_running_loop().create_future()
        self._sent = False

    async def joined(self, node: LinkedNode) -> LinkedOutcome:
        """Присоединяет вызов и ждёт итога связки."""
        self._nodes.append(node)
        self._fire()

        return await asyncio.shield(self._done)

    def left(self) -> None:
        """Вызов кончился, не дойдя до тела: связка его больше не ждёт."""
        self._left += 1
        self._fire()

    async def outcome(self) -> LinkedOutcome:
        """Итог связки для того, кто её собрал.

        Ошибки:
        McpClientError — до тела не дошёл ни один вызов связки.
        """
        return await asyncio.shield(self._done)

    def _fire(self) -> None:
        if self._sent:
            return

        if len(self._nodes) + self._left < self._size:
            return

        self._sent = True
        if not self._nodes:
            msg = (
                f"linked call of {self._size} tools: none of the calls reached "
                "its body, nothing is sent to the server"
            )
            self._done.set_exception(McpClientError(msg))
            return

        ordered = sorted(self._nodes, key=self._index_of)
        task = asyncio.ensure_future(self._send(tuple(ordered)))
        task.add_done_callback(self._settle)

    @staticmethod
    def _index_of(node: LinkedNode) -> int:
        return node.index

    def _settle(self, task: asyncio.Future[LinkedOutcome]) -> None:
        if task.cancelled():
            self._done.cancel()
            return

        error = task.exception()
        if error is not None:
            self._done.set_exception(error)
            return

        self._done.set_result(task.result())


class LinkedProgress:
    """Уведомления прогресса связки её вызовам: связка идёт одним запросом,
    и чей это отчёт, уведомление не называет — его получают приёмники всех
    вызовов связки. Создаётся портом McpToolServer на отправку связки."""

    def __init__(self, listeners: Sequence[ProgressListener]) -> None:
        self._listeners = tuple(listeners)

    async def __call__(
        self, progress: float, total: float | None, message: str | None
    ) -> None:
        text = ""
        if message is not None:
            text = message

        for listener in self._listeners:
            await listener.progressed(progress, total, text)


class ProgressListener(Protocol):
    """Приёмник отчётов сервера о ходе работы одного вызова."""

    @abstractmethod
    async def progressed(self, done: float, total: float | None, text: str) -> None:
        """Сервер сообщил: сделано done из total (None — итог неизвестен),
        text — что сейчас происходит; зовётся из задачи сессии MCP."""


class CallSignals(Protocol):
    """Куда клиент отдаёт отчёты серверов о ходе работы вызовов.

    Реализует хозяин клиента: чат показывает отчёт в шаге вызова.
    listener() зовётся в контексте вызова до отправки запроса; отчёты
    приходят в задаче сессии MCP, где контекста вызова уже нет.
    """

    @abstractmethod
    def listener(self, server: str, call: DagNode) -> ProgressListener:
        """Приёмник отчётов о ходе вызова call сервера server."""


class DroppedSignals(CallSignals, ProgressListener):
    """Реализация CallSignals без получателя: клиенту без ленты отчёты о
    ходе работы показывать негде."""

    def listener(self, server: str, call: DagNode) -> ProgressListener:
        return self

    async def progressed(self, done: float, total: float | None, text: str) -> None:
        return


class CallProgress:
    """Уведомления notifications/progress одного вызова — приёмнику вызова.

    Создаётся портом McpToolServer на каждый вызов: с ним в запросе уходит
    токен прогресса, и сервер, которому есть что сообщить, шлёт отчёты о
    ходе работы.
    """

    def __init__(self, listener: ProgressListener) -> None:
        self._listener = listener

    async def __call__(
        self, progress: float, total: float | None, message: str | None
    ) -> None:
        text = ""
        if message is not None:
            text = message

        await self._listener.progressed(progress, total, text)


class McpResults:
    """Итог вызова MCP итогом порта NodeOutcome.

    Создаётся портом McpToolServer с именем, под которым клиент знает
    сервер. Результат со сведениями о вызове и результатом известного вида
    в structuredContent — итог сервера boba-mcp: он оживает тем же итогом,
    каким его отдал порт сервера (ResultWire.revived), с адресом журнала
    вызова. Результат любого другого сервера — текст его
    content: текстовые блоки подряд, блоки-файлы (картинки, звук, вложенные
    ресурсы) уходят хозяину клиента (BlockFiles) и остаются в тексте его
    строкой, structuredContent без текста — JSON.
    """

    def __init__(self, files: BlockFiles, server: str) -> None:
        self._files = files
        self._server = server
        self._wire = ResultWire()
        self._outcomes = NodeOutcomes()
        self._causes = ErrorCauses()

    async def outcome(self, call: DagNode, result: mt.CallToolResult) -> NodeOutcome:
        """Итог вызова call по ответу сервера result."""
        text = await self._text(call, result)
        failed = bool(result.is_error)
        own = self._own(call, result, text, failed)
        if own is not None:
            return own

        if not failed:
            return self._outcomes.of(call, MarkdownResult(text=text), False)

        message = text
        if not message:
            message = "the mcp server reported an error without a text"

        failure = ErrorResult(message=message, error_kind=McpFailure.TOOL_ERROR)

        return self._outcomes.refused(call, failure)

    def failed(self, call: DagNode, error: Exception) -> NodeOutcome:
        """Итог вызова, который до сервера не дошёл или остался без ответа."""
        failure = ErrorResult(
            message=(
                f"tool {call.tool!r} got no result from its mcp server: "
                f"{self._causes.text(error)}"
            ),
            error_kind=McpFailure.TRANSPORT,
        )

        return self._outcomes.refused(call, failure)

    def _own(
        self, call: DagNode, result: mt.CallToolResult, text: str, failed: bool
    ) -> NodeOutcome | None:
        """Итог сервера boba-mcp; None — сервер его не прислал либо прислал
        вид результата, которого клиент не знает."""
        wire = WireResult(
            content=text, structured=result.structured_content, is_error=failed
        )
        try:
            return self._wire.revived(wire, call, self._server)
        except ValidationError as exc:
            logger.warning(
                "mcp result of a known kind does not match its model, shown as "
                "text: %s",
                exc,
            )
            return None

    async def _text(self, call: DagNode, result: mt.CallToolResult) -> str:
        parts: list[str] = []
        files = 0
        for block in result.content:
            payload = self._payload(block)
            if payload is None:
                parts.append(self._block_text(block))
                continue

            mime, data = payload
            parts.append(await self._files.attached(call, files, mime, data))
            files += 1

        text = "\n".join(part for part in parts if part)
        if text:
            return text

        if result.structured_content is not None:
            return json.dumps(result.structured_content, ensure_ascii=False, indent=2)

        return ""

    @staticmethod
    def _payload(block: mt.ContentBlock) -> tuple[str, bytes] | None:
        """Тип и байты блока-файла; None — блок текстовый или ссылка."""
        if isinstance(block, mt.ImageContent | mt.AudioContent):
            return block.mime_type, base64.b64decode(block.data)

        if not isinstance(block, mt.EmbeddedResource):
            return None

        resource = block.resource
        if not isinstance(resource, mt.BlobResourceContents):
            return None

        mime = resource.mime_type
        if not mime:
            mime = "application/octet-stream"

        return mime, base64.b64decode(resource.blob)

    @staticmethod
    def _block_text(block: mt.ContentBlock) -> str:
        if isinstance(block, mt.TextContent):
            return block.text

        if isinstance(block, mt.EmbeddedResource):
            resource = block.resource
            if isinstance(resource, mt.TextResourceContents):
                return resource.text

        if isinstance(block, mt.ResourceLink):
            return f"[resource link {block.uri}]"

        return f"[{type(block).__name__}]"


class McpToolServer(ToolServer):
    """Реализация порта ToolServer над одним MCP-сервером.

    Создаётся сборкой клиента из конфига сервера. open() подключается,
    проходит инициализацию и один раз читает список инструментов; после
    него tools() отдаёт карточки для модели, submit() шлёт вызовы серверу.
    Каждый вызов идёт к запросу серверу путём conduct (NodeCalls): хозяин
    клиента с лентой узнаёт так о начале и конце вызова.
    Возможности boba (features) берутся из экспериментальных возможностей,
    объявленных сервером при инициализации: у стороннего сервера их нет.
    Оборванная сессия открывается заново следующим вызовом. caller — чьим
    именем идёт подключение; None — оно общее для всех сессий клиента.
    """

    FEATURE_PREFIX: ClassVar[str] = "com.boba/"

    def __init__(  # noqa: PLR0913 — порт собирается всеми своими зависимостями
        self,
        name: str,
        config: McpServerConfig,
        files: BlockFiles,
        signals: CallSignals,
        contexts: CallContexts,
        caller: McpCaller | None,
        conduct: NodeCalls,
    ) -> None:
        self._name = name
        self._conduct = conduct
        self._signals = signals
        self._contexts = contexts
        self._config = config
        self._connection = McpConnection(
            config.endpoint, caller, config.connect_timeout_sec
        )
        self._stubs = McpToolStubs(config.prefix)
        self._outcomes = NodeOutcomes()
        self._by_name: dict[str, ToolCard] = {}
        """Имя инструмента у модели -> его карточка."""
        self._results = McpResults(files, name)
        self._tools: list[ToolCard] | None = None
        self._features: dict[str, Mapping[str, object]] = {}
        self._dags = CallDag()
        self._journal: JournalFeature | None = None
        """Расширение журнала вызовов, объявленное сервером; None — его нет."""
        self._files_feature: FilesFeature | None = None
        """Расширение файлов workspace, объявленное сервером; None — его нет."""
        self._caller = caller
        self._files: McpFiles | None = None
        self._journal_files: McpFiles | None = None

    async def open(self) -> None:
        """Ошибки:
        McpClientError — сервер недоступен, не отдал список инструментов,
            настройки объявленного им расширения либо вид аргументов в схеме
            инструмента не проходят модель.
        """
        session = await self._connection.open()
        self._features = self._declared(session)
        self._journal = self._feature(JournalFeature.ID, JournalFeature)
        self._files_feature = self._feature(FilesFeature.ID, FilesFeature)

        try:
            listed = await self._list(session)
        except Exception as exc:
            msg = (
                f"mcp server {self._name!r} ({self._config.endpoint.label()}): "
                f"tools/list failed: {type(exc).__name__}: {exc}"
            )
            raise McpClientError(msg) from exc

        client_side = self._client_side()
        tools: list[ToolCard] = []
        remote: dict[str, ToolCard] = {}
        for tool in listed:
            if tool.name in client_side:
                continue

            stub = self._stubs.card(tool)
            tools.append(stub)
            remote[stub.name] = stub

        self._tools = tools
        self._by_name = remote
        await self._open_files()
        await self._open_journal_files()
        logger.info(
            "mcp server %s: %d tools, features %s",
            self._name,
            len(tools),
            sorted(self._features),
        )

    def _links(self) -> bool:
        """Сервер boba-mcp с инструментом-связкой: её узлы — вызовы его же
        инструментов. У стороннего сервера одноимённый инструмент — обычный."""
        if not self._features:
            return False

        return self._stubs.local_name(self._dags.WORKFLOW) in self._by_name

    def _feature(
        self, identifier: str, model: type[FeatureModel]
    ) -> FeatureModel | None:
        """Настройки расширения identifier моделью; None — сервер его не
        объявил.

        Ошибки:
        McpClientError — настройки расширения не проходят его модель.
        """
        settings = self._features.get(identifier)
        if settings is None:
            return None

        try:
            return model.model_validate(settings)
        except ValidationError as exc:
            msg = (
                f"mcp server {self._name!r} ({self._config.endpoint.label()}): "
                f"extension {identifier} expects the settings of "
                f"{model.__name__}, got {dict(settings)!r}: {exc}"
            )
            raise McpClientError(msg) from exc

    def files(self) -> McpFiles | None:
        """Файлы workspace на сервере; None — сервер их не объявил."""
        return self._files

    def journal_files(self) -> McpFiles | None:
        """Журналы вызовов сервера целыми файлами: канал журнала лежит по
        пути `{run}/{node}/{channel}`; None — сервер такого не объявил."""
        return self._journal_files

    async def _open_journal_files(self) -> None:
        if self._journal_files is not None:
            await self._journal_files.close()
            self._journal_files = None

        declared = self._journal
        if declared is None:
            return

        endpoint = self._config.endpoint
        if not isinstance(endpoint, HttpAddress):
            return

        self._journal_files = McpFiles(
            self._name, endpoint, self._caller, declared.path
        )

    async def _open_files(self) -> None:
        """Клиент файлов по расширению FilesFeature; прежний закрывается."""
        if self._files is not None:
            await self._files.close()
            self._files = None

        declared = self._files_feature
        if declared is None:
            return

        endpoint = self._config.endpoint
        if not isinstance(endpoint, HttpAddress):
            return

        # пути файлов в результатах инструментов сервера начинаются с его
        # каталога workspace: по нему клиент разбирает их в ключи файлов
        WorkspaceMount.configure(declared.workspace)
        self._files = McpFiles(self._name, endpoint, self._caller, declared.path)

    def _client_side(self) -> frozenset[str]:
        """Операции сервера, которые исполняет сам клиент, а не модель: файл
        он шлёт потоком на маршрут файлов, журнал вызова читает окнами для
        панели живого вывода."""
        names: set[str] = set()
        if self._files_feature is not None:
            names.add(self._files_feature.upload)

        if self._journal is not None:
            names.add(self._journal.read)

        return frozenset(names)

    async def close(self) -> None:
        if self._files is not None:
            await self._files.close()
            self._files = None

        if self._journal_files is not None:
            await self._journal_files.close()
            self._journal_files = None

        await self._connection.close()

    @property
    def opened(self) -> bool:
        """Список инструментов прочитан: порт можно отдавать модели."""
        return self._tools is not None

    def tools(self) -> Sequence[ToolCard]:
        if self._tools is None:
            msg = (
                f"mcp server {self._name!r}: tools() is called before open(), "
                "the tool list is not read yet"
            )
            raise McpClientError(msg)

        return self._tools

    def features(self) -> Mapping[str, Mapping[str, object]]:
        return self._features

    async def submit(
        self, calls: Sequence[DagNode]
    ) -> Sequence[asyncio.Future[NodeOutcome]]:
        pending: list[asyncio.Future[NodeOutcome]] = []
        for call in calls:
            if self._nodes_of(call) is not None:
                pending.append(asyncio.ensure_future(self._nodes_called(call)))
                continue

            pending.append(asyncio.ensure_future(self._called(call, None)))

        return pending

    def _nodes_of(self, call: DagNode) -> Sequence[DagNode] | None:
        """Узлы вызова инструмента-связки вызовами своих инструментов; None —
        вызов не связка либо её узлы клиент разложить не может, и вызов
        уходит серверу как есть."""
        if not self._links():
            return None

        if self._stubs.remote_name(call.tool) != self._dags.WORKFLOW:
            return None

        try:
            nodes = self._dags.nodes_of(call)
        except StreamPlanError:
            return None

        for node in nodes:
            if self._stubs.local_name(node.tool) not in self._by_name:
                return None

        return nodes

    async def _nodes_called(self, call: DagNode) -> NodeOutcome:
        """Вызов инструмента-связки как вызовы его узлов.

        Связка — несколько инструментов, запущенных вместе: каждый узел идёт
        обычным вызовом своего инструмента под идентификатором вызова связки
        с номером узла, серверу они уходят одной связкой. Итог вызова — итог
        связки от сервера, с результатами узлов под теми же идентификаторами.
        """
        nodes = self._nodes_of(call)
        if nodes is None:
            return await self._called(call, None)

        group = LinkedCalls(len(nodes), self._send_linked)
        running: list[asyncio.Future[NodeOutcome]] = []
        for index, node in enumerate(nodes):
            local = self._stubs.local_name(node.tool)
            node_call = node.model_copy(update={"tool": local})
            seat = LinkedSeat(group, index, node.title)
            running.append(asyncio.ensure_future(self._called(node_call, seat)))

        await asyncio.gather(*running)
        try:
            outcome = await group.outcome()
        except McpClientError as exc:
            failure = ErrorResult(message=str(exc), error_kind=McpFailure.TOOL_ERROR)

            return self._outcomes.refused(call, failure)

        return outcome.of(call)

    async def _send_linked(self, nodes: Sequence[LinkedNode]) -> LinkedOutcome:
        """Связка одним вызовом инструмента-связки сервера: узел на вызов,
        идентификатор вызова узла — идентификатор вызова модели."""
        tool = self._dags.WORKFLOW
        if not self._links():
            msg = (
                f"mcp server {self._name!r}: a linked call of {len(nodes)} tools "
                f"is sent, but the server offers no tool {tool!r}"
            )
            raise McpClientError(msg)

        described: list[DagNode] = []
        listeners: list[ProgressListener] = []
        for node in nodes:
            call_id = node.call.key
            described.append(
                DagNode(
                    key=node.key,
                    tool=node.remote,
                    args=node.arguments,
                    call_id=call_id,
                )
            )
            listeners.append(node.listener)

        linked = DagNode(key=f"linked_{uuid4().hex}", tool=tool)
        whole = self._dags.with_nodes(linked, described)
        arguments = whole.args
        try:
            session = await self._connection.open()
            result = await session.send_request(
                self._request(tool, whole, arguments),
                mt.CallToolResult,
                request_read_timeout_seconds=self._config.call_timeout_sec,
                progress_callback=LinkedProgress(listeners),
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "mcp server %s: linked call of %d tools got no result: %s: %s",
                self._name,
                len(nodes),
                type(exc).__name__,
                exc,
            )
            await self._connection.close()

            return LinkedOutcome(self._results.failed(whole, exc), {})

        shared = await self._results.outcome(whole, result)
        results: dict[str, WorkflowNodeResult] = {}
        artifact = shared.artifact
        if isinstance(artifact, WorkflowResult):
            for done in artifact.nodes:
                results[done.call_id] = done

        return LinkedOutcome(shared, results)

    async def _called(self, call: DagNode, seat: LinkedSeat | None) -> NodeOutcome:
        """Вызов инструмента путём вызова узла (NodeCalls): о его начале и
        конце узнаёт хозяин клиента. Итог тела — итог вызова: результат либо
        отказ, с адресом журнала."""
        card = self._by_name.get(call.tool)
        if card is None:
            failure = ErrorResult(
                message=f"mcp server {self._name!r} has no tool {call.tool!r}",
                error_kind=McpFailure.TOOL_ERROR,
            )

            return self._outcomes.refused(call, failure)

        live = LiveCall(call, seat)
        try:
            return await self._conduct.conducted(card, call, partial(self._body, live))
        finally:
            if seat is not None and not live.joined:
                seat.group.left()

    async def _body(self, live: LiveCall, node: DagNode) -> NodeOutcome:
        """Тело узла: запрос tools/call серверу от имени вызова live. Итог —
        тот, что отдал порт сервера, с адресом журнала запуска, которым
        сервер исполнил вызов."""
        call = live.call
        remote = self._stubs.remote_name(call.tool)
        arguments = call.args
        seat = live.seat
        if seat is not None:
            linked = LinkedNode(
                call,
                remote,
                arguments,
                self._signals.listener(self._name, call),
                seat.index,
                seat.key,
            )
            live.joined = True
            outcome = await seat.group.joined(linked)

            return outcome.of(call)

        try:
            session = await self._connection.open()
            result = await session.send_request(
                self._request(remote, call, arguments),
                mt.CallToolResult,
                request_read_timeout_seconds=self._config.call_timeout_sec,
                progress_callback=CallProgress(
                    self._signals.listener(self._name, call)
                ),
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "mcp server %s: tool %s got no result: %s: %s",
                self._name,
                remote,
                type(exc).__name__,
                exc,
            )
            await self._connection.close()

            return self._results.failed(call, exc)

        return await self._results.outcome(call, result)

    async def journal(self, request: JournalRead) -> StreamSlice | None:
        """Окно журнала вызова с сервера; None — сервер журнал не объявлял
        либо такого журнала у него нет.

        Ошибки:
        McpClientError — сервер недоступен или ответил не окном журнала.
        """
        feature = self._journal
        if feature is None:
            return None

        tool = feature.read
        arguments = request.model_dump(mode="json", exclude_none=True)
        try:
            session = await self._connection.open()
            result = await session.call_tool(
                tool,
                arguments,
                read_timeout_seconds=self._config.call_timeout_sec,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            msg = (
                f"mcp server {self._name!r}: reading the journal of call "
                f"{request.node!r} in run {request.run!r} with {tool!r} failed: "
                f"{type(exc).__name__}: {exc}"
            )
            raise McpClientError(msg) from exc

        if result.is_error:
            return None

        try:
            return StreamSlice.model_validate(result.structured_content)
        except ValidationError as exc:
            msg = (
                f"mcp server {self._name!r}: {tool!r} answered call "
                f"{request.node!r} of run {request.run!r} with something other "
                f"than a journal window: {exc}"
            )
            raise McpClientError(msg) from exc

    def _request(
        self, remote: str, call: DagNode, sent: Mapping[str, object]
    ) -> mt.CallToolRequest:
        """Запрос tools/call. Серверу с расширением файлов в _meta едет
        область текущего вызова: по ней сервер держит файлы разговора."""
        arguments = dict(sent)
        meta = self._meta()
        if not meta:
            params = mt.CallToolRequestParams(name=remote, arguments=arguments)

            return mt.CallToolRequest(method="tools/call", params=params)

        params = mt.CallToolRequestParams(
            name=remote,
            arguments=arguments,
            _meta=meta,
        )

        return mt.CallToolRequest(method="tools/call", params=params)

    def _meta(self) -> mt.RequestParamsMeta:
        """Служебные поля запроса: область вызова под ключом, который назвал
        сервер в расширении файлов; серверу без него они не уходят."""
        meta: mt.RequestParamsMeta = {}
        files = self._files_feature
        if files is None:
            return meta

        context = self._contexts.peek()
        if context is None:
            return meta

        meta[files.scope] = context.scope.id

        return meta

    @staticmethod
    async def _list(session: ClientSession) -> list[mt.Tool]:
        """Все страницы tools/list."""
        listed: list[mt.Tool] = []
        cursor: str | None = None
        while True:
            page = await session.list_tools(
                params=mt.PaginatedRequestParams(cursor=cursor)
            )
            listed.extend(page.tools)
            cursor = page.next_cursor
            if not cursor:
                return listed

    def _declared(self, session: ClientSession) -> dict[str, Mapping[str, object]]:
        """Расширения boba среди расширений, объявленных сервером."""
        capabilities = session.server_capabilities
        if capabilities is None:
            return {}

        extensions = capabilities.extensions
        if not extensions:
            return {}

        declared: dict[str, Mapping[str, object]] = {}
        for feature, settings in extensions.items():
            if not feature.startswith(self.FEATURE_PREFIX):
                continue

            declared[feature] = dict(settings)

        return declared


class McpServers:
    """MCP-серверы клиента: подключение на старте и порты для сессии.

    Создаётся сборкой приложения из секции [mcp]. Сервер с общей
    авторизацией подключён один раз на процесс; сервер с личной (proxy)
    — отдельно от имени каждого пользователя, по его первой сессии. start()
    подключает общие серверы; сервер, который не ответил, приложение не
    роняет — он подключается при первой сессии, которой он положен.
    for_session() отдаёт порты серверов, названных профилем сессии.
    """

    KEEP: ClassVar[int] = 256
    """Сколько личных подключений держать; давно не нужные закрываются."""

    def __init__(
        self,
        config: McpServersConfig,
        files: BlockFiles,
        signals: CallSignals,
        contexts: CallContexts,
        conduct: NodeCalls,
    ) -> None:
        self._configs = dict(config.servers)
        self._files = files
        self._signals = signals
        self._contexts = contexts
        self._conduct = conduct
        self._shared: dict[str, McpToolServer] = {}
        self._personal: OrderedDict[tuple[str, McpCaller], McpToolServer]
        self._personal = OrderedDict()
        for name, server in config.servers.items():
            if server.endpoint.personal():
                continue

            self._shared[name] = McpToolServer(
                name, server, files, signals, contexts, None, conduct
            )

    async def start(self) -> None:
        for name, server in self._shared.items():
            await self._open(name, server)

    async def stop(self) -> None:
        for server in self._shared.values():
            await server.close()

        for server in self._personal.values():
            await server.close()

    def known(self, name: str) -> bool:
        """Есть ли сервер с таким именем в секции [mcp.servers]."""
        return name in self._configs

    async def for_session(
        self, caller: McpCaller, names: Iterable[str]
    ) -> Sequence[ToolServer]:
        """Порты серверов names для сессии пользователя caller.

        Ошибки:
        McpClientError — имя не названо в [mcp.servers].
        """
        ports: list[ToolServer] = []
        for name in names:
            if name not in self._configs:
                msg = (
                    f"mcp servers of the session of {caller.login!r}: server "
                    f"{name!r} is not declared in [mcp.servers], declared: "
                    f"{sorted(self._configs)}"
                )
                raise McpClientError(msg)

            server = await self._port(name, caller)
            if not server.opened:
                await self._open(name, server)

            if server.opened:
                ports.append(server)

        return ports

    async def journal(
        self, server: str, caller: McpCaller, request: JournalRead
    ) -> StreamSlice | None:
        """Окно журнала вызова с сервера server от имени caller; None —
        такого сервера или журнала нет.

        Ошибки:
        McpClientError — сервер недоступен или ответил не окном журнала.
        """
        if server not in self._configs:
            return None

        port = await self._port(server, caller)

        return await port.journal(request)

    async def files(self, server: str, caller: McpCaller) -> McpFiles | None:
        """Файлы workspace на сервере server от имени caller; None — сервер
        не назван в [mcp.servers], недоступен или файлов не объявил."""
        if server not in self._configs:
            return None

        port = await self._port(server, caller)
        if not port.opened:
            await self._open(server, port)

        return port.files()

    async def journal_files(self, server: str, caller: McpCaller) -> McpFiles | None:
        """Журналы вызовов сервера server целыми файлами от имени caller;
        None — сервер не назван, недоступен или такого не объявил."""
        if server not in self._configs:
            return None

        port = await self._port(server, caller)
        if not port.opened:
            await self._open(server, port)

        return port.journal_files()

    def journaled(self) -> frozenset[str]:
        """Имена инструментов подключённых серверов, которые ведут журнал
        вызовов: у их шагов есть живой вывод."""
        names: set[str] = set()
        for server in (*self._shared.values(), *self._personal.values()):
            if not server.opened:
                continue

            if JournalFeature.ID not in server.features():
                continue

            for tool in server.tools():
                names.add(tool.name)

        return frozenset(names)

    async def _port(self, name: str, caller: McpCaller) -> McpToolServer:
        """Порт сервера для пользователя: общий либо его личный."""
        shared = self._shared.get(name)
        if shared is not None:
            return shared

        key = (name, caller)
        server = self._personal.get(key)
        if server is None:
            server = McpToolServer(
                name,
                self._configs[name],
                self._files,
                self._signals,
                self._contexts,
                caller,
                self._conduct,
            )
            self._personal[key] = server

        self._personal.move_to_end(key)
        while len(self._personal) > self.KEEP:
            _stale, evicted = self._personal.popitem(last=False)
            await evicted.close()

        return server

    async def _open(self, name: str, server: McpToolServer) -> None:
        """Подключение сервера; сбой журналируется и сервер остаётся без порта
        до следующей попытки."""
        try:
            await server.open()
        except McpClientError as exc:
            logger.error("mcp server %s is unavailable: %s", name, exc)
