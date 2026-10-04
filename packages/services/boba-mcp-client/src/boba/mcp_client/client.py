"""Порт сервера инструментов над MCP-сервером.

Клиент работает с любым сервером стандарта MCP: берёт список инструментов
(tools/list), строит по их схемам инструменты для модели и исполняет вызовы
(tools/call). Транспорт — streamable HTTP, SSE или stdio. Итог вызова
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
import time
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
from contextvars import ContextVar
from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated, Any, ClassVar, Literal, Protocol
from urllib.parse import quote

import httpx2
import mcp.types as mt
from langchain_core.messages import ToolCall, ToolMessage
from langchain_core.tools import BaseTool, StructuredTool
from mcp import Client, ClientSession, StdioServerParameters
from mcp.client.extension import ClientExtension, advertise
from mcp.client.sse import sse_client
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client
from mcp.shared._stream_protocols import ReadStream, WriteStream
from mcp.shared.message import SessionMessage
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError

from boba.auth.proxy import ProxySignature
from boba.canvas.journal import StreamSlice
from boba.canvas.keys import ObjectKey
from boba.canvas.storage import (
    FileStat,
    OpenedStream,
    StorageError,
    StorageFullError,
    StorageNotFoundError,
)
from boba.identity.context import CallContexts
from boba.identity.signin import ProxyHeaderNames, ProxyRequest
from boba.toolkit.result import (
    ErrorResult,
    FailureResult,
    MarkdownResult,
    ToolResultBase,
)
from boba.toolkit.wire import (
    FilesFeature,
    JournalFeature,
    JournalRead,
    JournalSignal,
    RequestMeta,
    ResultWire,
    WireMeta,
    WireResult,
)
from boba.toolrun.stream_calls import CallReply, ToolServer
from boba.workspace.launcher import ReadWindow

__all__ = [
    "BearerAuth",
    "BlockFiles",
    "CallSignals",
    "DroppedSignals",
    "HttpEndpoint",
    "HttpLocation",
    "JournalAddress",
    "JournalAddresses",
    "JournalListener",
    "McpCaller",
    "McpClientError",
    "McpServerConfig",
    "McpServers",
    "McpServersConfig",
    "McpToolServer",
    "NamedBlocks",
    "NoAuth",
    "ProxyAuth",
    "SseEndpoint",
    "StdioCommand",
]

logger = logging.getLogger(__name__)

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

    Клиент — доверенный бэкенд: он уже проверил пользователя и называет
    серверу его логин (identifier пользователя в клиенте) и роли, с
    которыми тот вошёл в клиент, заголовками headers под подписью HMAC
    ключом secret. sign_in — адрес входа сервера ([auth.proxy].path); ответ
    — токен сессии сервера. Токен свой у каждого пользователя, поэтому и
    подключение к серверу у каждого своё.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    PERSONAL: ClassVar[bool] = True

    auth: Literal["proxy"] = "proxy"
    secret: SecretStr
    sign_in: HttpLocation
    headers: ProxyHeaderNames

    def httpx_auth(self, caller: McpCaller | None, resource: str) -> httpx2.Auth | None:
        if caller is None:
            msg = (
                f"mcp auth proxy for {resource}: the connection is opened "
                "without a caller, expected the login and roles of the session user"
            )
            raise McpClientError(msg)

        return ProxySession(self, caller)


class ProxySession(httpx2.Auth):
    """Токен сервера для одного пользователя по входу proxy.

    Создаётся ProxyAuth на подключение пользователя. Перед запросом без
    действующего токена входит на сервер подписанными заголовками; токен
    живёт до своего срока, отказ 401 сбрасывает его, и запрос повторяется
    один раз.
    """

    MARGIN_SEC: ClassVar[float] = 15.0
    """За сколько до конца срока токен считается истёкшим."""

    def __init__(self, config: ProxyAuth, caller: McpCaller) -> None:
        self._config = config
        self._caller = caller
        self._signature = ProxySignature(config.secret.get_secret_value())
        self._token = ""
        self._expires = 0.0
        self._lock = asyncio.Lock()

    async def async_auth_flow(
        self, request: httpx2.Request
    ) -> AsyncGenerator[httpx2.Request, httpx2.Response]:
        async with self._lock:
            if time.monotonic() >= self._expires:
                response = yield self._sign_in_request()
                await response.aread()
                self._take(response)

        request.headers["Authorization"] = f"Bearer {self._token}"
        response = yield request
        if response.status_code != httpx2.codes.UNAUTHORIZED:
            return

        async with self._lock:
            response = yield self._sign_in_request()
            await response.aread()
            self._take(response)

        request.headers["Authorization"] = f"Bearer {self._token}"
        yield request

    def _sign_in_request(self) -> httpx2.Request:
        names = self._config.headers
        roles = ",".join(sorted(self._caller.roles))
        unsigned = ProxyRequest(
            login=self._caller.login, timestamp=str(int(time.time())), roles=roles
        )
        headers = {
            names.user: unsigned.login,
            names.timestamp: unsigned.timestamp,
            names.signature: self._signature.sign(unsigned),
        }
        if names.roles:
            headers[names.roles] = roles

        return httpx2.Request("POST", self._config.sign_in.url(), headers=headers)

    def _take(self, response: httpx2.Response) -> None:
        url = self._config.sign_in.url()
        if response.status_code != httpx2.codes.OK:
            msg = (
                f"POST {url}: proxy sign-in of {self._caller.login!r} with roles "
                f"{sorted(self._caller.roles)} expected 200, got "
                f"{response.status_code}: {response.text[:300]}"
            )
            raise McpClientError(msg)

        try:
            issued = IssuedToken.model_validate_json(response.content)
        except ValidationError as exc:
            msg = (
                f"POST {url}: proxy sign-in of {self._caller.login!r} expected "
                f"access_token and expires_in, got {response.text[:300]}: {exc}"
            )
            raise McpClientError(msg) from exc

        self._token = issued.access_token
        self._expires = time.monotonic() + issued.expires_in - self.MARGIN_SEC


class IssuedToken(BaseModel):
    """Ответ входа сервера: токен доступа и срок его жизни."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    access_token: str = Field(min_length=1)
    expires_in: float = Field(gt=0)


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

    EXTENSIONS: ClassVar[Sequence[ClientExtension]] = (
        advertise(JournalFeature.ID.value),
    )
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


class McpFiles:
    """Файлы workspace пользователя на сервере boba-mcp.

    Создаётся портом McpToolServer, когда сервер объявил расширение
    FilesFeature, из адреса сервера, пути маршрута файлов и пользователя,
    чьим именем идёт подключение. Байты идут потоком в обе стороны: запись
    шлёт чанки источника телом PUT, чтение отдаёт чанки ответа GET окном
    Range. Владельца workspace сервер берёт из токена входа, поэтому из
    ключа файла на сервер уходят только область, каталог и имя.

    Ошибки те же, что у хранилища: StorageNotFoundError — файла нет,
    StorageFullError — в workspace нет места, StorageError — прочий отказ.
    """

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

    async def close(self) -> None:
        await self._http.aclose()

    async def upload(self, key: ObjectKey, source: AsyncIterator[bytes]) -> int:
        """Пишет файл потоком; возвращает число принятых сервером байт."""
        url = self._url(key)
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


ToolBody = Callable[..., Awaitable[tuple[str, ToolResultBase]]]
"""Тело инструмента MCP: аргументы вызова -> текст и модель результата."""

RemoteCall = Callable[
    [str, Mapping[str, object]], Awaitable[tuple[str, ToolResultBase]]
]
"""Вызов сервера: имя инструмента на сервере и аргументы -> итог."""


class McpToolBody:
    """Тело одного инструмента MCP-сервера для langchain.

    Создаётся портом McpToolServer на каждый инструмент списка. langchain
    зовёт called() с аргументами модели; тело передаёт их вызову сервера
    вместе с именем инструмента на сервере.
    """

    def __init__(self, remote: str, call: RemoteCall) -> None:
        self._remote = remote
        self._call = call

    async def called(self, **arguments: object) -> tuple[str, ToolResultBase]:
        return await self._call(self._remote, arguments)


class McpToolStubs:
    """Инструменты модели по списку инструментов MCP-сервера.

    Создаётся портом McpToolServer. Инструмент несёт имя, описание и схему
    аргументов сервера; его тело — вызов сервера, который даёт порт. Вызов
    идёт обычным путём инструмента langchain, поэтому лента клиента получает
    те же события шага, что и от своих инструментов.
    """

    def __init__(self, prefix: str) -> None:
        self._prefix = prefix

    def name_of(self, tool: mt.Tool) -> str:
        return f"{self._prefix}{tool.name}"

    def stub(self, tool: mt.Tool, body: ToolBody) -> BaseTool:
        description = tool.description
        if not description:
            description = tool.name

        return StructuredTool(
            name=self.name_of(tool),
            description=description,
            args_schema=dict(tool.input_schema),
            coroutine=body,
            response_format="content_and_artifact",
        )


class BlockFiles(Protocol):
    """Куда клиент кладёт нетекстовые блоки результата: картинки, звук,
    вложенные файлы.

    Реализует хозяин клиента: чат сохраняет блок файлом в workspace треда и
    показывает вложением. Итог — строка для модели о том, где теперь файл.
    """

    @abstractmethod
    async def attached(self, call: ToolCall, index: int, mime: str, data: bytes) -> str:
        """Принять блок номер index результата вызова call."""


class NamedBlocks(BlockFiles):
    """Реализация BlockFiles без хранилища: блок только называется в тексте
    результата. Ей пользуется клиент, которому некуда класть файлы."""

    async def attached(self, call: ToolCall, index: int, mime: str, data: bytes) -> str:
        return f"[{mime}, {len(data)} bytes]"


class JournalAddress(BaseModel):
    """Где лежит журнал вызова: сервер клиента и запуск на нём.

    Клиент кладёт адрес в response_metadata сообщения инструмента: история
    хранит его вместе с итогом, и журнал читается после конца хода.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    KEY: ClassVar[str] = "boba_journal"

    server: str = Field(min_length=1)
    run: str = Field(min_length=1)

    def stamp(self, message: ToolMessage) -> None:
        message.response_metadata[self.KEY] = self.model_dump(mode="json")


class JournalAddresses:
    """Разбор адреса журнала из сообщения инструмента истории. Создаётся
    тем, кто читает журнал по истории (панель живого вывода чата)."""

    def of(self, message: ToolMessage) -> JournalAddress | None:
        """Адрес журнала вызова; None — вызов исполнял не сервер с журналом."""
        stamped = message.response_metadata.get(JournalAddress.KEY)
        if stamped is None:
            return None

        return JournalAddress.model_validate(stamped)


class LiveCall:
    """Вызов, чьё тело сейчас исполняется: сам вызов и запуск, которым его
    исполнил сервер (известен из итога). Создаётся портом на время вызова."""

    def __init__(self, call: ToolCall) -> None:
        self.call = call
        self.run = ""


class JournalListener(Protocol):
    """Приёмник сигналов роста журнала одного вызова."""

    @abstractmethod
    async def appended(self, signal: JournalSignal) -> None:
        """Канал журнала вызова дорос; зовётся из задачи сессии MCP."""


class CallSignals(Protocol):
    """Куда клиент отдаёт сигналы роста журнала вызовов сервера boba-mcp.

    Реализует хозяин клиента: чат по сигналам ведёт панель живого вывода.
    listener() зовётся в контексте вызова до отправки запроса; сигналы
    приходят в задаче сессии MCP, где контекста вызова уже нет.
    """

    @abstractmethod
    def listener(self, server: str, call: ToolCall) -> JournalListener:
        """Приёмник сигналов вызова call сервера server."""


class DroppedSignals(CallSignals, JournalListener):
    """Реализация CallSignals без получателя: сигналы не нужны клиенту, у
    которого нет панели живого вывода; журнал при этом читается окнами."""

    def listener(self, server: str, call: ToolCall) -> JournalListener:
        return self

    async def appended(self, signal: JournalSignal) -> None:
        return


class CallProgress:
    """Уведомления notifications/progress одного вызова как сигналы журнала.

    Создаётся портом McpToolServer на вызов сервера, объявившего журнал.
    Сигнал — модель JournalSignal в поле message; уведомление без неё —
    обычный прогресс сервера, журнала оно не касается.
    """

    def __init__(self, listener: JournalListener) -> None:
        self._listener = listener

    async def __call__(
        self, progress: float, total: float | None, message: str | None
    ) -> None:
        if message is None:
            return

        try:
            signal = JournalSignal.model_validate_json(message)
        except ValidationError:
            return

        await self._listener.appended(signal)


class McpResults:
    """Итог вызова MCP как сообщение инструмента истории.

    Создаётся портом McpToolServer. Результат с полями boba в _meta —
    итог сервера boba-mcp: его модель оживает по kind. Результат любого
    другого сервера — текст его content: текстовые блоки подряд, блоки-файлы
    (картинки, звук, вложенные ресурсы) уходят хозяину клиента (BlockFiles)
    и остаются в тексте его строкой, structuredContent без текста — JSON.
    """

    def __init__(self, files: BlockFiles) -> None:
        self._files = files
        self._wire = ResultWire()
        self._causes = ErrorCauses()

    async def parts(
        self, call: ToolCall, result: mt.CallToolResult
    ) -> tuple[str, ToolResultBase]:
        """Текст для модели и модель результата вызова call."""
        text = await self._text(call, result)
        artifact = self._artifact(result, text, bool(result.is_error))

        return artifact.packed()

    def failed(self, call: ToolCall, error: Exception) -> tuple[str, ToolResultBase]:
        """Итог вызова, который до сервера не дошёл или остался без ответа."""
        failure = ErrorResult(
            message=(
                f"tool {call['name']!r} got no result from its mcp server: "
                f"{self._causes.text(error)}"
            ),
            error_kind=McpFailure.TRANSPORT,
        )

        return failure.packed()

    def _artifact(
        self, result: mt.CallToolResult, text: str, failed: bool
    ) -> ToolResultBase:
        own = self._own(result, text, failed)
        if own is not None:
            return own

        if failed:
            message = text
            if not message:
                message = "the mcp server reported an error without a text"

            return ErrorResult(message=message, error_kind=McpFailure.TOOL_ERROR)

        return MarkdownResult(text=text)

    def _own(
        self, result: mt.CallToolResult, text: str, failed: bool
    ) -> ToolResultBase | None:
        """Модель результата сервера boba-mcp; None — сервер её не прислал
        либо прислал вид, которого клиент не знает."""
        meta = result.meta
        if not meta:
            return None

        if WireMeta.NAMESPACE.value not in meta:
            return None

        wire = WireResult(
            content=text,
            structured=result.structured_content,
            is_error=failed,
            meta=meta,
        )
        try:
            return self._wire.revived(wire).artifact
        except ValidationError as exc:
            logger.warning(
                "mcp result of a known kind does not match its model, shown as "
                "text: %s",
                exc,
            )
            return None

    async def _text(self, call: ToolCall, result: mt.CallToolResult) -> str:
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
    него tools() отдаёт заглушки для модели, submit() шлёт вызовы серверу.
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
    ) -> None:
        self._name = name
        self._signals = signals
        self._contexts = contexts
        self._config = config
        self._connection = McpConnection(
            config.endpoint, caller, config.connect_timeout_sec
        )
        self._stubs = McpToolStubs(config.prefix)
        self._calls: ContextVar[LiveCall] = ContextVar(f"mcp_call_{name}")
        """Вызов, чьё тело сейчас исполняется: телу нужен его id."""
        self._wire = ResultWire()
        self._by_name: dict[str, BaseTool] = {}
        """Имя инструмента у модели -> инструмент."""
        self._results = McpResults(files)
        self._tools: list[BaseTool] | None = None
        self._features: dict[str, Mapping[str, object]] = {}
        self._caller = caller
        self._files: McpFiles | None = None

    async def open(self) -> None:
        """Ошибки:
        McpClientError — сервер недоступен или не отдал список инструментов.
        """
        session = await self._connection.open()
        self._features = self._declared(session)

        try:
            listed = await self._list(session)
        except Exception as exc:
            msg = (
                f"mcp server {self._name!r} ({self._config.endpoint.label()}): "
                f"tools/list failed: {type(exc).__name__}: {exc}"
            )
            raise McpClientError(msg) from exc

        client_side = self._client_side()
        tools: list[BaseTool] = []
        remote: dict[str, BaseTool] = {}
        for tool in listed:
            if tool.name in client_side:
                continue

            stub = self._stubs.stub(tool, McpToolBody(tool.name, self._body).called)
            tools.append(stub)
            remote[stub.name] = stub

        self._tools = tools
        self._by_name = remote
        await self._open_files()
        logger.info(
            "mcp server %s: %d tools, features %s",
            self._name,
            len(tools),
            sorted(self._features),
        )

    def files(self) -> McpFiles | None:
        """Файлы workspace на сервере; None — сервер их не объявил."""
        return self._files

    async def _open_files(self) -> None:
        """Клиент файлов по расширению FilesFeature; прежний закрывается."""
        if self._files is not None:
            await self._files.close()
            self._files = None

        declared = self._features.get(FilesFeature.ID.value)
        if declared is None:
            return

        endpoint = self._config.endpoint
        if not isinstance(endpoint, HttpAddress):
            return

        path = declared.get(FilesFeature.PATH.value)
        if not isinstance(path, str):
            msg = (
                f"mcp server {self._name!r}: extension {FilesFeature.ID.value} "
                f"expects {FilesFeature.PATH.value} as a string, got {path!r}"
            )
            raise McpClientError(msg)

        self._files = McpFiles(self._name, endpoint, self._caller, path)

    def _client_side(self) -> frozenset[str]:
        """Инструменты сервера, которые исполняет сам клиент, а не модель:
        загрузку файла клиент шлёт потоком на маршрут файлов сервера, модели
        адрес загрузки не нужен."""
        files = self._features.get(FilesFeature.ID.value)
        if files is None:
            return frozenset()

        upload = files.get(FilesFeature.UPLOAD.value)
        if not isinstance(upload, str):
            return frozenset()

        return frozenset({upload})

    async def close(self) -> None:
        if self._files is not None:
            await self._files.close()
            self._files = None

        await self._connection.close()

    @property
    def opened(self) -> bool:
        """Список инструментов прочитан: порт можно отдавать модели."""
        return self._tools is not None

    def tools(self) -> Sequence[BaseTool]:
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
        self, calls: Sequence[ToolCall]
    ) -> Sequence[asyncio.Future[ToolMessage]]:
        pending: list[asyncio.Future[ToolMessage]] = []
        for call in calls:
            pending.append(asyncio.ensure_future(self._called(call)))

        return pending

    async def _called(self, call: ToolCall) -> ToolMessage:
        """Вызов инструмента обычным путём langchain: его события получает
        лента клиента. Итог тела — модель результата; сбой — модель отказа."""
        tool = self._by_name.get(call["name"])
        if tool is None:
            failure = ErrorResult(
                message=f"mcp server {self._name!r} has no tool {call['name']!r}",
                error_kind=McpFailure.TOOL_ERROR,
            )

            return CallReply(call).message(failure, True)

        live = LiveCall(call)
        token = self._calls.set(live)
        try:
            message = await tool.ainvoke(call)
        finally:
            self._calls.reset(token)

        artifact = message.artifact
        if not isinstance(artifact, ToolResultBase):
            artifact = MarkdownResult(text=str(message.content))

        reply = CallReply(call).message(artifact, isinstance(artifact, FailureResult))
        if live.run:
            JournalAddress(server=self._name, run=live.run).stamp(reply)

        return reply

    async def _body(
        self, remote: str, arguments: Mapping[str, object]
    ) -> tuple[str, ToolResultBase]:
        """Тело инструмента: запрос tools/call серверу от имени текущего вызова."""
        live = self._calls.get()
        call = live.call
        try:
            session = await self._connection.open()
            result = await session.send_request(
                self._request(remote, call, arguments),
                mt.CallToolResult,
                request_read_timeout_seconds=self._config.call_timeout_sec,
                progress_callback=self._progress(call),
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

        if result.meta:
            live.run = self._wire.run_of(result.meta)

        return await self._results.parts(call, result)

    def _progress(self, call: ToolCall) -> CallProgress | None:
        """Приёмник прогресса вызова; None — сервер журнал не объявлял, и
        токен прогресса ему не шлётся."""
        if JournalFeature.ID.value not in self._features:
            return None

        return CallProgress(self._signals.listener(self._name, call))

    async def journal(self, request: JournalRead) -> StreamSlice | None:
        """Окно журнала вызова с сервера; None — сервер журнал не объявлял
        либо такого журнала у него нет.

        Ошибки:
        McpClientError — сервер недоступен или ответил не окном журнала.
        """
        feature = self._features.get(JournalFeature.ID.value)
        if feature is None:
            return None

        tool = str(feature.get(JournalFeature.READ.value))
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
        self, remote: str, call: ToolCall, sent: Mapping[str, object]
    ) -> mt.CallToolRequest:
        """Запрос tools/call. Серверу, объявившему возможности boba, в _meta
        едут идентификатор вызова модели (его журнал и итог несут тот же id)
        и область текущего вызова: по ней сервер держит файлы разговора."""
        arguments = dict(sent)
        meta = self._meta(call)
        if not meta:
            params = mt.CallToolRequestParams(name=remote, arguments=arguments)

            return mt.CallToolRequest(method="tools/call", params=params)

        params = mt.CallToolRequestParams(
            name=remote,
            arguments=arguments,
            _meta=meta,
        )

        return mt.CallToolRequest(method="tools/call", params=params)

    def _meta(self, call: ToolCall) -> mt.RequestParamsMeta:
        """Служебные поля запроса; стороннему серверу они не уходят."""
        meta: mt.RequestParamsMeta = {}
        if not self._features:
            return meta

        if call_id := call["id"]:
            meta[RequestMeta.CALL_ID.value] = call_id

        if context := self._contexts.peek():
            meta[RequestMeta.SCOPE.value] = context.scope.id

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
    ) -> None:
        self._configs = dict(config.servers)
        self._files = files
        self._signals = signals
        self._contexts = contexts
        self._shared: dict[str, McpToolServer] = {}
        self._personal: OrderedDict[tuple[str, McpCaller], McpToolServer]
        self._personal = OrderedDict()
        for name, server in config.servers.items():
            if server.endpoint.personal():
                continue

            self._shared[name] = McpToolServer(
                name, server, files, signals, contexts, None
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

    def journaled(self) -> frozenset[str]:
        """Имена инструментов подключённых серверов, которые ведут журнал
        вызовов: у их шагов есть живой вывод."""
        names: set[str] = set()
        for server in (*self._shared.values(), *self._personal.values()):
            if not server.opened:
                continue

            if JournalFeature.ID.value not in server.features():
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
