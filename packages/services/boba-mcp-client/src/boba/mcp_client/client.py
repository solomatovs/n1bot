"""Клиент MCP-серверов: подключения, списки, вызовы и потоки подписки.

Клиент работает с любым сервером стандарта MCP — редакции 2026-07-28 и
прежних с рукопожатием initialize: версию согласует штатная политика SDK
(проба server/discover, при отказе сервера прежней редакции — initialize).
С сервером 2026-07-28 у клиента нет состояния сверх того, что разрешает
спецификация: версия сервера, кэш ответов по правилам caching и то, что
живёт один запрос; личность клиента едет в каждом запросе.
Клиент читает списки инструментов, ресурсов, шаблонов и промптов, исполняет
tools/call, resources/read, prompts/get и completion/complete и держит
потоки subscriptions/listen (они есть только в 2026-07-28), отдавая ответ
сервера как есть. Серверу клиент представляется личностью McpClientIdentity:
своей либо агента, чьи запросы ретранслирует чат. Транспорт — streamable
HTTP, SSE или stdio. Способ входа — свойство записи сервера (McpAuth): без
входа, готовый токен, вход local учёткой с паролем и вход proxy
утверждением о пользователе. Токен сервера для local и proxy получают и
обновляют штатные провайдеры OAuth библиотеки mcp; своё здесь — только
проход страницы входа без человека (UnattendedLogin).

Ошибки:
McpClientError — сервер недоступен, не согласовал версию протокола,
    отказал во входе, не ответил на запрос, забыл сессию клиента, ответил не
    по схеме либо отказал в подписке.
MCPError (mcp.shared.exceptions) — сервер ответил на запрос ошибкой MCP.
"""

from __future__ import annotations

import asyncio
import hashlib
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
    Iterator,
    Mapping,
    Sequence,
)
from contextlib import (
    AbstractAsyncContextManager,
    asynccontextmanager,
    contextmanager,
    nullcontext,
    suppress,
)
from contextvars import ContextVar
from dataclasses import dataclass, replace
from enum import StrEnum
from functools import partial
from http import HTTPStatus
from typing import Annotated, Any, ClassVar, Literal, Protocol, Self, TypeVar
from urllib.parse import quote, urlsplit, urlunsplit
from uuid import uuid4

import anyio
import anyio.abc
import httpx2
import mcp.types as mt
from mcp import ClientSession, StdioServerParameters
from mcp.client._probe import negotiate_auto
from mcp.client.auth import OAuthClientProvider, TokenStorage
from mcp.client.auth.extensions.identity_assertion import (
    IdentityAssertionOAuthProvider,
)
from mcp.client.caching import (
    MAX_TTL_MS,
    CacheEntry,
    CacheKey,
    ClientResponseCache,
    InMemoryResponseCacheStore,
)
from mcp.client.session import IncomingMessage, MessageHandlerFnT
from mcp.client.sse import sse_client
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import MCP_SESSION_ID, streamable_http_client
from mcp.client.subscriptions import (
    ListenNotSupportedError,
    ListenRoute,
    ServerEvent,
    Subscription,
    SubscriptionLost,
    listen,
)
from mcp.server.apps import APP_MIME_TYPE, EXTENSION_ID
from mcp.shared._httpx_utils import create_mcp_http_client
from mcp.shared._stream_protocols import ReadStream, WriteStream
from mcp.shared.auth import (
    AuthorizationCodeResult,
    OAuthClientInformationFull,
    OAuthClientMetadata,
    OAuthToken,
)
from mcp.shared.dispatcher import (
    CallOptions,
    Dispatcher,
    OnNotify,
    OnNotifyIntercept,
    OnRequest,
)
from mcp.shared.exceptions import MCPError
from mcp.shared.inbound import ERROR_CODE_HTTP_STATUS
from mcp.shared.jsonrpc_dispatcher import JSONRPCDispatcher
from mcp.shared.message import SessionMessage
from mcp.shared.subscriptions import event_to_notification
from mcp.shared.uri_template import InvalidUriTemplate, UriTemplate
from mcp.types.version import KNOWN_PROTOCOL_VERSIONS, MODERN_PROTOCOL_VERSIONS
from pydantic import (
    AnyUrl,
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    SecretStr,
    TypeAdapter,
    ValidationError,
    field_validator,
)
from pydantic.alias_generators import to_camel

from boba.auth.proxy import ProxyAssertions
from boba.auth.signin import LoginField
from boba.canvas.journal import StreamSlice
from boba.canvas.keys import ObjectKey
from boba.canvas.storage import (
    FileStat,
    OpenedStream,
    StorageError,
    StorageFullError,
    StorageNotFoundError,
)
from boba.connections.sealed import SealFeature
from boba.toolkit.dag import NestedCallsFeature, ServerFeature
from boba.toolkit.mcp import McpToolNames
from boba.toolkit.wire import FilesFeature, JournalFeature, JournalOrigin, JournalRead
from boba.workspace.launcher import ReadWindow

__all__ = [
    "BearerAuth",
    "HttpEndpoint",
    "HttpLocation",
    "JournalHeard",
    "JournalLinks",
    "JournalOpening",
    "LocalAuth",
    "McpAuth",
    "McpAuthEntry",
    "McpCaller",
    "McpClientError",
    "McpClientIdentity",
    "McpServerApps",
    "McpServerConfig",
    "McpServers",
    "McpServersConfig",
    "McpToolPort",
    "McpToolServer",
    "NoAuth",
    "ProgressHeard",
    "ProxyAuth",
    "SseEndpoint",
    "StdioCommand",
]

logger = logging.getLogger(__name__)

FeatureModel = TypeVar("FeatureModel", bound=ServerFeature)
"""Модель настроек расширения сервера."""

ResultT = TypeVar("ResultT", bound=BaseModel)
"""Модель ответа сервера на запрос порта."""

SentT = TypeVar("SentT")
"""Ответ запроса, который порт шлёт сессией запроса."""

Streams = tuple[ReadStream[SessionMessage | Exception], WriteStream[SessionMessage]]
"""Пара потоков сообщений транспорта MCP: чтение и запись."""


class McpClientError(Exception):
    """С MCP-сервером не удалось договориться: соединение, инициализация,
    список инструментов."""


@dataclass(frozen=True)
class McpCaller:
    """Пользователь клиента, от чьего имени тот ходит на сервер: логин (его
    identifier в клиенте) и роли, с которыми он вошёл в клиент.

    Создаётся хозяином клиента на сессию пользователя. Запись сервера
    подставляет его в свой способ входа (McpAuthEntry.on_behalf_of): вход
    proxy утверждает серверу именно этого пользователя. McpServers по нему
    же различает подключения разных пользователей.
    """

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


class McpAuthEntry(BaseModel):
    """Базовый класс способов входа на MCP-сервер; его наследуют NoAuth,
    BearerAuth, LocalAuth и ProxyAuth.

    Способ входа — секция auth записи сервера в конфиге хозяина клиента
    ([mcp.servers.<имя>.endpoint]). Наследник отдаёт транспорту авторизацию
    запросов (httpx_auth) и, если входит от имени пользователя, подставляет
    его в себя (on_behalf_of).
    """

    model_config = ConfigDict(
        frozen=True, extra="forbid", alias_generator=to_camel, populate_by_name=True
    )

    PERSONAL: ClassVar[bool] = False
    """Подключение своё у каждого пользователя клиента."""

    def httpx_auth(self, resource: str) -> httpx2.Auth | None:
        """Авторизация запросов к серверу по адресу resource; None — запросы
        идут без неё.

        Ошибки:
        McpClientError — записи не хватает того, без чего вход невозможен.
        """
        return None

    def on_behalf_of(self, caller: McpCaller) -> Self:
        """Тот же способ входа от имени пользователя клиента: общий для всех
        пользователей вход остаётся собой."""
        return self


class NoAuth(McpAuthEntry):
    """Сервер без авторизации."""

    auth: Literal["none"] = "none"


class BearerToken(httpx2.Auth):
    """Готовый токен заголовком Authorization каждого запроса."""

    def __init__(self, token: SecretStr) -> None:
        self._token = token

    def auth_flow(self, request: httpx2.Request) -> Any:
        request.headers["Authorization"] = f"Bearer {self._token.get_secret_value()}"
        yield request


class BearerAuth(McpAuthEntry):
    """Готовый токен доступа из конфига: один на всех пользователей клиента."""

    auth: Literal["bearer"] = "bearer"
    token: SecretStr

    def httpx_auth(self, resource: str) -> httpx2.Auth | None:
        return BearerToken(self.token)


class HeldTokens(TokenStorage):
    """Токены сервера и запись клиента OAuth одного подключения в памяти:
    реализация TokenStorage клиента mcp. Переживать процесс им незачем —
    вход повторяется сам: local проходит страницу входа, proxy меняет новое
    утверждение."""

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


class UnattendedLogin:
    """Вход на страницу входа сервиса boba без человека: два обработчика
    штатного провайдера OAuth (OAuthClientProvider) вместо браузера.

    Создаётся способом входа LocalAuth на подключение. Провайдер сам находит
    сервер авторизации, регистрирует клиента (client()), строит адрес
    authorize и после возврата кода меняет его на токены; между этими шагами
    он зовёт redirect() с адресом authorize и callback() за кодом. redirect()
    проходит маршруты сервера как браузер: authorize отвечает адресом
    страницы входа, страница ставит cookie-подтверждение, форма с логином,
    паролем и тем же подтверждением отвечает адресом возврата с кодом.
    Содержимое страницы не читается: всё нужное лежит в заголовке Location,
    cookie и параметрах адресов. По адресу возврата никто не ходит.
    """

    REDIRECT_URI: ClassVar[str] = "http://127.0.0.1/boba-unattended-login"
    CLIENT_NAME: ClassVar[str] = "boba unattended sign-in"
    LOCATION: ClassVar[str] = "location"
    BODY_SHOWN: ClassVar[int] = 600
    """Сколько знаков ответа сервера входит в текст ошибки."""

    def __init__(self, login: str, password: SecretStr, resource: str) -> None:
        self._login = login
        self._password = password
        self._resource = resource
        self._granted: AuthorizationCodeResult | None = None

    def client(self) -> OAuthClientMetadata:
        """Запись клиента OAuth для регистрации у сервера авторизации:
        публичный клиент с кодом авторизации и обновлением токена."""
        return OAuthClientMetadata(
            client_name=self.CLIENT_NAME,
            redirect_uris=[AnyUrl(self.REDIRECT_URI)],
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
            token_endpoint_auth_method="none",  # noqa: S106 — имя метода
        )

    async def redirect(self, authorization_url: str) -> None:
        """Проходит вход по адресу authorize и запоминает код авторизации.

        Ошибки:
        McpClientError — сервер недоступен, вход не начат, страница входа не
            выдала подтверждения либо отказала логину и паролю.
        """
        self._granted = None
        try:
            async with create_mcp_http_client() as http:
                self._granted = await self._signed_in(http, authorization_url)
        except httpx2.HTTPError as exc:
            authorize = httpx2.URL(authorization_url).copy_with(query=None)
            msg = (
                f"mcp auth local for {self._resource}: signing in as "
                f"{self._login!r} at {authorize} failed: "
                f"{type(exc).__name__}: {exc}"
            )
            raise McpClientError(msg) from exc

    async def callback(self) -> AuthorizationCodeResult:
        """Код авторизации, полученный redirect().

        Ошибки:
        McpClientError — кода нет: redirect() не вызывался.
        """
        granted = self._granted
        if granted is None:
            msg = (
                f"mcp auth local for {self._resource}: the authorization code of "
                f"{self._login!r} was asked before the sign-in page was passed"
            )
            raise McpClientError(msg)

        return granted

    async def _signed_in(
        self, http: httpx2.AsyncClient, authorization_url: str
    ) -> AuthorizationCodeResult:
        page_url = self._next(await http.get(authorization_url), "the sign-in page")
        txn = page_url.params.get(LoginField.TXN.value)
        login_url = page_url.copy_with(query=None)
        if txn is None:
            msg = (
                f"mcp auth local for {self._resource}: {login_url} is not the "
                f"sign-in page of a started sign-in, expected parameter "
                f"{LoginField.TXN.value!r}, got parameters {dict(page_url.params)!r}"
            )
            raise McpClientError(msg)

        page = await http.get(page_url)
        proof = page.cookies.get(LoginField.COOKIE.value)
        if proof is None:
            msg = (
                f"mcp auth local for {self._resource}: GET "
                f"{login_url} expected cookie "
                f"{LoginField.COOKIE.value!r} of the sign-in page, got "
                f"{self._reply(page)}"
            )
            raise McpClientError(msg)

        form = {
            LoginField.TXN.value: txn,
            LoginField.PROOF.value: proof,
            LoginField.LOGIN.value: self._login,
            LoginField.PASSWORD.value: self._password.get_secret_value(),
        }
        returned = self._next(
            await http.post(login_url, data=form),
            f"the authorization code of {self._login!r}",
        )
        code = returned.params.get("code")
        if code is None:
            msg = (
                f"mcp auth local for {self._resource}: the sign-in of "
                f"{self._login!r} returned without an authorization code, got "
                f"parameters {dict(returned.params)!r}"
            )
            raise McpClientError(msg)

        return AuthorizationCodeResult(
            code=code,
            state=returned.params.get("state"),
            iss=returned.params.get("iss"),
        )

    def _next(self, reply: httpx2.Response, expected: str) -> httpx2.URL:
        """Адрес из Location ответа-редиректа.

        Ошибки:
        McpClientError — сервер ответил не редиректом.
        """
        location = reply.headers.get(self.LOCATION)
        if reply.is_redirect and location is not None:
            return reply.url.join(location)

        request = reply.request
        msg = (
            f"mcp auth local for {self._resource}: {request.method} "
            f"{request.url.copy_with(query=None)} expected a redirect to {expected}, "
            f"got {self._reply(reply)}"
        )
        raise McpClientError(msg)

    def _reply(self, reply: httpx2.Response) -> str:
        return (
            f"HTTP {reply.status_code} {reply.reason_phrase}: "
            f"{reply.text[: self.BODY_SHOWN]}"
        )


class LocalAuth(McpAuthEntry):
    """Вход local сервиса boba: одна учётка с паролем из таблицы [auth.local]
    сервера на всех пользователей клиента.

    Токен получает штатный провайдер OAuth (код авторизации с PKCE): клиента
    он регистрирует сам, страницу входа вместо браузера проходит
    UnattendedLogin. Токен обновляется по токену обновления; когда сервер
    в обновлении отказал, вход повторяется целиком.
    """

    auth: Literal["local"] = "local"
    login: str = Field(min_length=1)
    password: SecretStr

    def httpx_auth(self, resource: str) -> httpx2.Auth | None:
        login = UnattendedLogin(self.login, self.password, resource)

        return OAuthClientProvider(
            server_url=resource,
            client_metadata=login.client(),
            storage=HeldTokens(),
            redirect_handler=login.redirect,
            callback_handler=login.callback,
        )


class ProxyAuth(McpAuthEntry):
    """Вход proxy сервиса boba: клиент утверждает серверу пользователя.

    Клиент — доверенный бэкенд: он утверждает серверу авторизации issuer
    логин login и роли roles подписанным JWT под общим ключом secret
    ([auth.proxy].secret сервера). Утверждение меняется на токен доступа
    штатным обменом OAuth (grant jwt-bearer); клиента сервер узнаёт по
    client_id и client_secret ([mcp.clients] сервера). Токена обновления
    нет: по истечении провайдер меняет новое утверждение. Хозяин клиента со
    своими пользователями оставляет login пустым и подставляет пользователя
    сессии (on_behalf_of): сервер тогда видит разных пользователей.
    """

    PERSONAL: ClassVar[bool] = True

    auth: Literal["proxy"] = "proxy"
    issuer: HttpLocation
    client_id: str = Field(min_length=1)
    client_secret: SecretStr
    secret: SecretStr
    login: str = ""
    roles: tuple[str, ...] = ()

    def on_behalf_of(self, caller: McpCaller) -> Self:
        return self.model_copy(
            update={"login": caller.login, "roles": tuple(sorted(caller.roles))}
        )

    def httpx_auth(self, resource: str) -> httpx2.Auth | None:
        if not self.login:
            msg = (
                f"mcp auth proxy for {resource}: the entry names no login, "
                "expected the login and roles the server is told about"
            )
            raise McpClientError(msg)

        assertions = StatedAssertions(
            ProxyAssertions(self.secret.get_secret_value(), 0),
            self.client_id,
            self.login,
            self.roles,
        )

        return IdentityAssertionOAuthProvider(
            server_url=resource,
            storage=HeldTokens(),
            client_id=self.client_id,
            client_secret=self.client_secret.get_secret_value(),
            issuer=self.issuer.url(),
            assertion_provider=assertions.issue,
        )


class StatedAssertions:
    """Утверждения о пользователе для обмена на токен сервера.

    Создаётся ProxyAuth на подключение; штатный клиент обмена
    (IdentityAssertionOAuthProvider) зовёт issue() перед каждым обменом и
    получает свежее утверждение о логине и ролях записи.
    """

    ROLES_SEPARATOR: ClassVar[str] = ","

    def __init__(
        self,
        assertions: ProxyAssertions,
        client_id: str,
        login: str,
        roles: Sequence[str],
    ) -> None:
        self._assertions = assertions
        self._client_id = client_id
        self._login = login
        self._roles = self.ROLES_SEPARATOR.join(sorted(roles))

    async def issue(self, audience: str, resource: str) -> str:
        return self._assertions.issue(
            self._client_id, self._login, self._roles, audience, resource
        )


McpAuth = Annotated[
    NoAuth | BearerAuth | LocalAuth | ProxyAuth, Field(discriminator="auth")
]


ResponseHook = Callable[[httpx2.Response], Awaitable[None]]
"""Наблюдатель ответов HTTP-клиента транспорта."""


class Transport(Protocol):
    """Транспорт MCP-сервера: открывает потоки сообщений сессии."""

    @abstractmethod
    def opened(self, seen: ResponseHook) -> AbstractAsyncContextManager[Streams]:
        """Потоки сессии; открыты, пока открыт контекст. Транспорт HTTP
        показывает seen каждый ответ сервера."""

    @abstractmethod
    def on_behalf_of(self, caller: McpCaller) -> Transport:
        """Тот же транспорт со входом от имени пользователя клиента."""

    @abstractmethod
    def label(self) -> str:
        """Адрес сервера для сообщений об ошибках и логов."""


class ErrorCauses:
    """Причины сбоя одной строкой: группа исключений anyio раскрывается до
    своих листьев, иначе в тексте остаётся «unhandled errors in a TaskGroup».
    Создаётся соединением, учётом отказов и портом McpToolServer."""

    def text(self, error: BaseException) -> str:
        return "; ".join(self._leaves(error))

    def _leaves(self, error: BaseException) -> list[str]:
        if isinstance(error, BaseExceptionGroup):
            leaves: list[str] = []
            for inner in error.exceptions:
                leaves.extend(self._leaves(inner))

            return leaves

        return [f"{type(error).__name__}: {error}"]


class HttpRefusal:
    """Отказ HTTP одного запроса к серверу: последний ответ POST этого
    запроса со статусом от 400.

    Создаётся учётом отказов порта (HttpRefusals) на каждый запрос и на
    согласование версии соединения и видит ответы только своего запроса.
    Библиотека mcp заменяет ответ с отказом (401, 403, 502) безликой ошибкой
    протокола; text() возвращает в текст ошибки адрес и статус. Отказ на
    GET не считается: им сервер без потока уведомлений отвечает исправной
    сессии. 404 на запрос с Mcp-Session-Id — сервер забыл сессию
    (forgotten); 400 — отказ, по телу которого порт узнаёт в сервере
    современной редакции сервер прежней (bad_request).
    """

    REQUEST: ClassVar[str] = "POST"

    def __init__(self, causes: ErrorCauses) -> None:
        self._causes = causes
        self._last: str | None = None
        self._bad_request = False
        self._forgotten = False

    def seen(self, response: httpx2.Response) -> None:
        request = response.request
        if request.method != self.REQUEST:
            return

        if response.status_code < HTTPStatus.BAD_REQUEST:
            self._last = None
            self._bad_request = False
            return

        if response.status_code == HTTPStatus.NOT_FOUND:
            self._forgotten = MCP_SESSION_ID in request.headers

        self._bad_request = response.status_code == HTTPStatus.BAD_REQUEST
        self._last = (
            f"{request.method} {request.url}: expected a reply of the MCP server, "
            f"got HTTP {response.status_code} {response.reason_phrase}"
        )

    def forgotten(self) -> bool:
        """Сервер ответил запросу 404 в своей сессии: сессии больше нет."""
        return self._forgotten

    def bad_request(self) -> bool:
        """Последний отказ запроса — 400."""
        return self._bad_request

    def text(self, error: BaseException) -> str:
        """Причина error вместе с отказом HTTP запроса, если он был."""
        causes = self._causes.text(error)
        last = self._last
        if last is None:
            return causes

        return f"{last}; {causes}"


class HttpRefusals:
    """Учёт отказов HTTP подключений порта по запросам.

    Создаётся портом McpToolServer; seen() транспорт HTTP ставит
    наблюдателем ответов своего HTTP-клиента. Каждый запрос порта и каждое
    согласование версии открывают свой отказ (watching) в переменной
    контекста, а ответ на запрос HTTP-клиент разбирает в контексте того,
    кто запрос отправил: отказ одного запроса не достаётся соседнему.
    """

    def __init__(self, label: str) -> None:
        self._current: ContextVar[HttpRefusal | None] = ContextVar(
            f"mcp-refusal {label}", default=None
        )
        self._causes = ErrorCauses()

    async def seen(self, response: httpx2.Response) -> None:
        refusal = self._current.get()
        if refusal is None:
            return

        refusal.seen(response)

    @contextmanager
    def watching(self) -> Iterator[HttpRefusal]:
        """Отказ запроса, который идёт, пока открыт контекст."""
        refusal = HttpRefusal(self._causes)
        token = self._current.set(refusal)
        try:
            yield refusal
        finally:
            self._current.reset(token)


class HttpAddress(HttpLocation):
    """Адрес HTTP-сервера MCP: место и способ авторизации."""

    auth: McpAuth = NoAuth()

    def personal(self) -> bool:
        """Подключение своё у каждого пользователя: токен выдаётся ему."""
        return self.auth.PERSONAL

    def on_behalf_of(self, caller: McpCaller) -> Self:
        return self.model_copy(update={"auth": self.auth.on_behalf_of(caller)})


class HttpEndpoint(HttpAddress):
    """Сервер по streamable HTTP. Модель конфига; транспорт — она же
    (протокол Transport наследовать нельзя: метакласс pydantic)."""

    transport: Literal["streamable-http"] = "streamable-http"

    def label(self) -> str:
        return self.url()

    @asynccontextmanager
    async def opened(self, seen: ResponseHook) -> AsyncGenerator[Streams, None]:
        auth = self.auth.httpx_auth(self.url())
        async with create_mcp_http_client(auth=auth) as http:
            http.event_hooks["response"].append(seen)
            async with streamable_http_client(self.url(), http_client=http) as streams:
                yield streams


class SseEndpoint(HttpAddress):
    """Сервер по SSE (прежний HTTP-транспорт MCP). Модель конфига; транспорт
    — она же (протокол Transport). Ответов своего HTTP-клиента транспорт
    SDK не показывает: отказ называет только ошибка протокола."""

    transport: Literal["sse"] = "sse"

    def label(self) -> str:
        return self.url()

    @asynccontextmanager
    async def opened(self, seen: ResponseHook) -> AsyncGenerator[Streams, None]:
        auth = self.auth.httpx_auth(self.url())
        async with sse_client(self.url(), auth=auth) as streams:
            yield streams


class StdioCommand(BaseModel):
    """Сервер-субпроцесс со стандартным вводом и выводом. Модель конфига;
    транспорт — она же (протокол Transport). Ответов HTTP у него нет."""

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

    def on_behalf_of(self, caller: McpCaller) -> Self:
        return self

    @asynccontextmanager
    async def opened(self, seen: ResponseHook) -> AsyncGenerator[Streams, None]:
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


class McpServerApps(BaseModel):
    """Секция [mcp.servers.<имя>.apps]: серверу разрешено показывать свой
    интерфейс (MCP Apps) в браузере пользователя.

    sandbox_url — страница-прокси песочницы на источнике, отдельном от чата
    и от песочниц других серверов: в ней чат рисует приложения сервера.
    Запись сервера с этой секцией объявляет серверу расширение
    io.modelcontextprotocol/ui; источник адреса проверяют McpServersConfig и
    конфиг хоста приложений.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    sandbox_url: HttpUrl

    def origin(self) -> str:
        """Источник страницы-прокси в форме браузера: схема, хост и порт,
        если он не порт схемы по умолчанию; учётные данные адреса в
        источник не входят."""
        url = self.sandbox_url
        host = url.host
        if host is None:
            msg = f"[apps] sandbox_url {url} expects a host, got none"
            raise ValueError(msg)

        bare = HttpUrl.build(scheme=url.scheme, host=host, port=url.port)
        parts = urlsplit(str(bare))

        return urlunsplit((parts.scheme, parts.netloc, "", "", ""))

    def settings(self) -> dict[str, Any]:
        """Настройки расширения MCP Apps в возможностях клиента: клиент
        рисует ресурсы приложений этого вида."""
        return {"mimeTypes": [APP_MIME_TYPE]}


class McpServerConfig(BaseModel):
    """Один MCP-сервер в конфиге клиента: секция [mcp.servers.<имя>].

    Адрес сервера (endpoint) и способ авторизации в нём. Сервер подключён
    тем сессиям, чей профиль клиента его называет; набор инструментов решает
    сам сервер. Сроки — на подключение с инициализацией и на один вызов.
    Секция apps есть только у сервера, чьи приложения клиент рисует; без неё
    сервер показывает итоги блоками content.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    endpoint: McpEndpoint
    connect_timeout_sec: float = Field(gt=0)
    call_timeout_sec: float = Field(gt=0)
    apps: McpServerApps | None = None

    def on_behalf_of(self, caller: McpCaller) -> McpServerConfig:
        """Та же запись со входом от имени пользователя клиента: способ
        входа сам решает, подставлять ли его."""
        return self.model_copy(update={"endpoint": self.endpoint.on_behalf_of(caller)})


class McpServersConfig(BaseModel):
    """Секция [mcp]: MCP-серверы клиента по именам. Имя сервера входит в
    полные имена его инструментов (McpToolNames) и проверяется их формой."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    servers: Mapping[str, McpServerConfig]

    @field_validator("servers")
    @classmethod
    def _named(
        cls, value: Mapping[str, McpServerConfig]
    ) -> Mapping[str, McpServerConfig]:
        names = McpToolNames()
        for name in value:
            names.checked_server(name)

        return value

    @field_validator("servers")
    @classmethod
    def _sandboxes_apart(
        cls, value: Mapping[str, McpServerConfig]
    ) -> Mapping[str, McpServerConfig]:
        """У песочниц разных серверов разные источники: иначе приложения
        одного сервера достают до приложений другого."""
        held: dict[str, str] = {}
        for name, server in value.items():
            apps = server.apps
            if apps is None:
                continue

            origin = apps.origin()
            other = held.get(origin)
            if other is not None:
                msg = (
                    f"[mcp.servers.{name}.apps] sandbox_url {apps.sandbox_url} is on "
                    f"origin {origin}, which is already the sandbox origin of server "
                    f"{other!r}; give every server with apps a sandbox on its own "
                    "origin, so that the apps of one server cannot reach another"
                )
                raise ValueError(msg)

            held[origin] = name

        return value


@dataclass(frozen=True)
class McpClientIdentity:
    """Кем клиент представляется серверу в _meta каждого запроса: имя и
    версия (clientInfo) и возможности (clientCapabilities).

    Свою личность клиент чата держит в McpServers.OWN; запросы, которые чат
    ретранслирует за агента, идут с личностью агента из его запроса: сервер
    видит настоящего клиента и его возможности. Личность — свойство
    запроса, а не подключения: порт McpToolServer принимает её каждым
    запросом. Расширения, которые исполняет сам чат (журнал, запечатывание
    соединений, MCP Apps), порт добавляет к любой личности.
    """

    info: mt.Implementation
    capabilities: mt.ClientCapabilities

    def key(self) -> str:
        """Одна строка на одну личность: по ней порт различает сессии
        прежней редакции и кэши ответов."""
        info = self.info.model_dump(mode="json", by_alias=True, exclude_none=True)
        capabilities = self.capabilities.model_dump(
            mode="json", by_alias=True, exclude_none=True
        )

        return json.dumps([info, capabilities], sort_keys=True)

    def with_extensions(self, extensions: Mapping[str, Mapping[str, Any]]) -> Self:
        """Та же личность с расширениями extensions поверх своих: одноимённое
        расширение берётся из extensions."""
        merged: dict[str, dict[str, Any]] = {}
        declared = self.capabilities.extensions
        if declared is not None:
            for identifier, settings in declared.items():
                merged[identifier] = dict(settings)

        for identifier, settings in extensions.items():
            merged[identifier] = dict(settings)

        capabilities = self.capabilities.model_copy(update={"extensions": merged})

        return replace(self, capabilities=capabilities)


class IdentifiedSession(ClientSession):
    """Сессия SDK mcp, которая объявляет серверу ровно заданные возможности.

    Сессию подключения создаёт соединение McpConnection, лёгкую сессию
    запроса — порт (RequestSession). SDK собирает clientCapabilities _meta
    запроса сам из обработчиков сессии; ретранслятор чата обязан передать
    серверу возможности агента как есть, поэтому сборка возможностей здесь
    отдаёт их из личности клиента; на проводе рукопожатия — без
    elicitation, sampling и roots: запросов сервера посреди вызова принять
    некому. absorbed() отдаёт сессии карточки инструментов, которые порт уже
    знает: по ним она зеркалит параметры x-mcp-header в заголовки и
    проверяет итог вызова по схеме.
    """

    def __init__(
        self,
        dispatcher: Dispatcher[Any],
        identity: McpClientIdentity,
        message_handler: MessageHandlerFnT | None,
    ) -> None:
        super().__init__(
            client_info=identity.info,
            message_handler=message_handler,
            dispatcher=dispatcher,
        )
        self._identity = identity
        self._wire = dispatcher

    def absorbed(self, tools: Sequence[mt.Tool]) -> None:
        """Карточки tools, известные порту, — сессии."""
        listing = mt.ListToolsResult(tools=list(tools))
        self._absorb_tool_listing(listing, complete=False)

    def adopted(self, version: str) -> None:
        """Версия современной редакции, которую порт уже знает у сервера,
        принята без пробы и без обмена по сети."""
        described = mt.DiscoverResult(
            supported_versions=[version], capabilities=mt.ServerCapabilities()
        )
        self.adopt(described)

    def _build_capabilities(self, version: str) -> mt.ClientCapabilities:
        capabilities = self._identity.capabilities
        if version in MODERN_PROTOCOL_VERSIONS:
            return capabilities

        return capabilities.model_copy(
            update={"elicitation": None, "sampling": None, "roots": None}
        )


class BorrowedDispatcher(Dispatcher[Any]):
    """Реализация Dispatcher SDK над диспетчером подключения для лёгкой
    сессии запроса (RequestSession): запросы и уведомления уходят
    диспетчером подключения, а своего цикла приёма нет — принимает сессия
    подключения."""

    def __init__(self, line: Dispatcher[Any]) -> None:
        self._line = line

    async def run(
        self,
        on_request: OnRequest,
        on_notify: OnNotify,
        on_notify_intercept: OnNotifyIntercept | None = None,
        *,
        task_status: anyio.abc.TaskStatus[None] = anyio.TASK_STATUS_IGNORED,
    ) -> None:
        task_status.started()
        await anyio.sleep_forever()

    async def send_raw_request(
        self,
        method: str,
        params: Mapping[str, Any] | None,
        opts: CallOptions | None = None,
    ) -> dict[str, Any]:
        return await self._line.send_raw_request(method, params, opts)

    async def notify(
        self,
        method: str,
        params: Mapping[str, Any] | None,
        opts: CallOptions | None = None,
    ) -> None:
        await self._line.notify(method, params, opts)


class RequestSession(IdentifiedSession):
    """Лёгкая сессия SDK одного запроса к серверу современной редакции.

    Создаётся портом McpToolServer на каждый запрос поверх сессии
    подключения порта с личностью клиента этого запроса: принимает версию
    подключения без обмена по сети (adopt) и ставит личность и возможности в
    _meta запроса, а сам запрос шлёт диспетчер подключения
    (BorrowedDispatcher). Ответы и уведомления принимает сессия подключения;
    маршрут потока subscriptions/listen регистрируется у неё же — иначе
    подтверждение потока до запроса не дойдёт. В контекст лёгкая сессия не
    входит: своего цикла приёма у неё нет, а поток subscriptions/listen
    идёт в группе задач сессии подключения.
    """

    def __init__(self, line: IdentifiedSession, identity: McpClientIdentity) -> None:
        super().__init__(BorrowedDispatcher(line._wire), identity, None)
        self._line = line
        self._task_group = line._task_group
        version = line.protocol_version
        if version is None:
            msg = (
                f"a request session of {identity.info.name!r} expects an opened "
                "session to borrow its negotiated protocol version, got none"
            )
            raise McpClientError(msg)

        self.adopted(version)

    def _register_listen_route(self, request_id: mt.RequestId) -> ListenRoute:
        return self._line._register_listen_route(request_id)

    def _unregister_listen_route(self, request_id: mt.RequestId) -> None:
        self._line._unregister_listen_route(request_id)


class AgingCacheStore(InMemoryResponseCacheStore):
    """Хранилище кэша ответов SDK в памяти, которое отдаёт ответ с
    оставшимся сроком свежести: в ttlMs — сколько ему осталось, а не сколько
    дал сервер. Так ретранслятор чата отдаёт агенту честный срок. Ответ без
    ttlMs от сервера (прежняя редакция) отдаётся как есть.

    Создаётся соединением McpConnection, одно на все его кэши.
    """

    async def get(self, key: CacheKey) -> CacheEntry | None:
        entry = await super().get(key)
        if entry is None:
            return None

        expires = entry.expires_at
        if expires is None:
            return entry

        value = entry.value
        if "ttl_ms" not in value.model_fields_set:
            return entry

        left = max(0, int((expires - time.time()) * 1000))

        return replace(entry, value=value.model_copy(update={"ttl_ms": left}))


class McpConnection:
    """Подключение к серверу MCP с согласованной версией протокола.

    Создаётся портом McpToolServer: у сервера прежней редакции — на каждую
    личность клиента (сессия initialize и есть состояние, которого требует
    2025-11-25), у современного — одно на порт: его диспетчер несёт лёгкие
    сессии запросов всех личностей (RequestSession). Транспорт и сессия SDK
    открываются и закрываются в одной собственной задаче — их области
    отмены привязаны к задаче, которая их открыла; запросы идут из задач
    вызовов. Версию согласует штатная политика SDK (negotiate_auto), а
    версию современной редакции, которую порт уже знает, сессия принимает
    без пробы. Отказ HTTP за время согласования называет учёт отказов порта
    (HttpRefusals). Уведомления прогресса уходят обработчику запроса,
    который слушает его токен (listening); уведомления сервера о смене
    списков и ресурсов сбрасывают кэши ответов подключения (cache).
    """

    def __init__(
        self,
        transport: Transport,
        connect_timeout_sec: float,
        identity: McpClientIdentity,
        refusals: HttpRefusals,
    ) -> None:
        self._transport = transport
        self._connect_timeout_sec = connect_timeout_sec
        self._identity = identity
        self._refusals = refusals
        self._causes = ErrorCauses()
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._ready: asyncio.Future[IdentifiedSession] | None = None
        self._session: IdentifiedSession | None = None
        self._listeners: dict[
            mt.ProgressToken,
            Callable[[mt.ProgressNotificationParams], Awaitable[None]],
        ] = {}
        self._store = AgingCacheStore()
        self._caches: dict[str, ClientResponseCache] = {}
        self._target = hashlib.sha256(transport.label().encode()).hexdigest()

    @asynccontextmanager
    async def listening(
        self,
        token: mt.ProgressToken,
        heard: Callable[[mt.ProgressNotificationParams], Awaitable[None]],
    ) -> AsyncGenerator[None, None]:
        """Уведомления прогресса под токеном token уходят heard, пока
        открыт контекст."""
        self._listeners[token] = heard
        try:
            yield
        finally:
            self._listeners.pop(token, None)

    def cache(self, identity: McpClientIdentity) -> ClientResponseCache:
        """Кэш ответов личности identity на этом подключении: у современной
        редакции ответ свеж ttlMs и делится по cacheScope, у прежней — живёт
        до конца сессии; уведомления сервера о смене списков его сбрасывают,
        а с подключением он закрывается."""
        key = identity.key()
        cache = self._caches.get(key)
        if cache is not None:
            return cache

        lifetime = MAX_TTL_MS
        if self._negotiated() in MODERN_PROTOCOL_VERSIONS:
            lifetime = 0

        cache = ClientResponseCache(
            store=self._store,
            partition=key,
            arm_id=self._target,
            default_ttl_ms=lifetime,
            clock=time.time,
            share_public=False,
            negotiated_version=self._negotiated,
        )
        self._caches[key] = cache

        return cache

    async def evicted(self, notification: mt.ServerNotification) -> None:
        """Уведомление сервера сбрасывает ответы кэшей, к которым относится."""
        for cache in list(self._caches.values()):
            await cache.evict_for_notification(notification)

    async def heard(self, event: ServerEvent) -> None:
        """Событие потока subscriptions/listen сбрасывает кэш раньше, чем
        его получит читатель потока: тот перечитает свежий ответ."""
        await self.evicted(event_to_notification(event, {}))

    async def open(self, version: str | None) -> IdentifiedSession:
        """Сессия с согласованным протоколом; повторный вызов отдаёт уже
        открытую. version — версия современной редакции, которую порт уже
        знает у сервера: сессия принимает её без пробы; None — версию
        согласует negotiate_auto.

        Ошибки:
        McpClientError — сервер недоступен, не согласовал версию либо не
            ответил за срок подключения.
        """
        if self._ready is None:
            self._stop = asyncio.Event()
            self._ready = asyncio.get_running_loop().create_future()
            self._task = asyncio.create_task(
                self._serve(self._ready, version),
                name=f"mcp:{self._transport.label()}",
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
        self._session = None
        self._caches.clear()
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

    def _negotiated(self) -> str | None:
        """Версия открытой сессии; None — сессия не открыта."""
        session = self._session
        if session is None:
            return None

        return session.protocol_version

    async def _serve(
        self, ready: asyncio.Future[IdentifiedSession], version: str | None
    ) -> None:
        """Сессия живёт в этой задаче."""
        stop = self._stop
        with self._refusals.watching() as refusal:
            try:
                async with self._transport.opened(self._refusals.seen) as streams:
                    read, write = streams
                    dispatcher = JSONRPCDispatcher(read, write)
                    session = IdentifiedSession(dispatcher, self._identity, self._heard)
                    async with session:
                        await self._negotiated_by(session, version)
                        self._session = session
                        ready.set_result(session)
                        await stop.wait()
            except Exception as exc:
                if not ready.done():
                    msg = (
                        f"mcp {self._transport.label()}: connecting and "
                        f"initializing failed: {refusal.text(exc)}"
                    )
                    ready.set_exception(McpClientError(msg))
                    return

                raise

    async def _negotiated_by(
        self, session: IdentifiedSession, version: str | None
    ) -> None:
        """Ошибки:
        McpClientError — сервер отверг обе попытки либо назвал версию,
            которой клиент не знает.
        """
        if version is not None:
            session.adopted(version)
            return

        try:
            await negotiate_auto(session)
        except MCPError as exc:
            msg = (
                f"mcp {self._transport.label()}: negotiating the protocol by "
                f"server/discover and then initialize was refused with MCP error "
                f"{exc.error.code}: {exc.error.message} (data {exc.error.data!r}); "
                f"the client speaks {list(KNOWN_PROTOCOL_VERSIONS)}"
            )
            raise McpClientError(msg) from exc
        except RuntimeError as exc:
            msg = (
                f"mcp {self._transport.label()}: initialize was answered with a "
                f"protocol version the client does not know: {exc}; the client "
                f"speaks {list(KNOWN_PROTOCOL_VERSIONS)}"
            )
            raise McpClientError(msg) from exc

    async def _heard(self, message: IncomingMessage) -> None:
        """Обработчик сообщений сессии: уведомление сервера сбрасывает кэши,
        к которым относится; уведомление прогресса — обработчику запроса с
        его токеном; прочее и прогресс без слушателя не нужны."""
        if isinstance(message, Exception):
            return

        await self.evicted(message)
        if not isinstance(message, mt.ProgressNotification):
            return

        heard = self._listeners.get(message.params.progress_token)
        if heard is None:
            return

        await heard(message.params)


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

    def __init__(self, server: str, endpoint: HttpAddress, path: str) -> None:
        self._server = server
        self._root = HttpLocation(
            scheme=endpoint.scheme, host=endpoint.host, port=endpoint.port, path=path
        )
        auth = endpoint.auth.httpx_auth(endpoint.url())
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

    async def delete_scope(self, scope_id: str) -> None:
        """Область scope_id целиком — файлы её каталогов и журналы вызовов
        её запусков: сервер удаляет их одной операцией, пустая область —
        успех."""
        area = FilesFeature.AREA.format(scope=quote(scope_id, safe=""))
        url = "/".join((self._root.url(), area))
        reply = await self._http.delete(url)
        self._check("DELETE", url, reply)

    def _url(self, key: ObjectKey) -> str:
        route = FilesFeature.ROUTE.format(
            scope=key.thread_id, dir=key.dir.value, name=key.name
        )

        return "/".join((self._root.url(), quote(route)))

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


class JournalLinks:
    """Адреса каналов журнала вызова на стороне клиента: шаблон uri ресурса
    канала, который объявил сервер (JournalFeature.resource), раскрывается и
    сопоставляется шаблонами URI SDK mcp (RFC 6570).

    Создаётся портом McpToolServer по объявлению сервера. По uri() клиент
    читает окна журнала, по address() узнаёт адрес журнала в ссылке итога и
    в ресурсе списка.

    Ошибки:
    InvalidUriTemplate — шаблон сервера не по RFC 6570.
    """

    def __init__(self, template: str) -> None:
        self._template = UriTemplate.parse(template)

    def uri(self, address: JournalRead) -> str:
        """Uri ресурса канала журнала по его адресу."""
        variables: dict[str, str] = {}
        for name, value in address.model_dump(mode="json", exclude_none=True).items():
            variables[name] = str(value)

        return self._template.expand(variables)

    def address(self, uri: str) -> JournalRead | None:
        """Адрес канала журнала по ссылке; None — ссылка не на журнал."""
        matched = self._template.match(uri)
        if matched is None:
            return None

        try:
            return JournalRead.model_validate(matched)
        except ValidationError as exc:
            logger.info("journal link %s names no journal channel: %s", uri, exc)
            return None


@dataclass(frozen=True)
class JournalOpening:
    """Журнал вызова открылся на сервере: запуск и идентификатор вызова на
    сервере (адрес журнала) и ключ узла от автора связки; node None — журнал
    вызова одного инструмента. Создаёт JournalOpenings по ресурсу списка."""

    run: str
    call: str
    node: str | None


JournalHeard = Callable[[JournalOpening], Awaitable[None]]
"""Кому порт сообщает журналы, открытые вызовом."""

ProgressHeard = Callable[[mt.ProgressNotificationParams], Awaitable[None]]
"""Кому порт отдаёт уведомления сервера о ходе запроса."""


PortSend = Callable[[McpConnection, IdentifiedSession], Awaitable[SentT]]
"""Запрос порта: шлёт его сессией запроса на подключении порта."""


class CachedMethod(StrEnum):
    """Запросы, ответы на которые порт держит в кэше SDK по правилам
    caching."""

    DISCOVER = "server/discover"
    TOOLS = "tools/list"


@dataclass(frozen=True)
class McpDescription:
    """Описание сервера: ответ server/discover (у сервера прежней редакции —
    собранный из его initialize) со сроком свежести и расширения boba,
    которые из него следуют.

    Создаёт порт (McpToolServer.described, ChatToolPort чата) на каждое
    обращение из ответа, который тот взял из кэша или у сервера. Расширение
    с настройками не по модели считается необъявленным: такой сервер
    работает как сервер без расширения, причина — в логе.
    """

    FEATURE_PREFIX: ClassVar[str] = "com.boba/"

    label: str
    result: mt.DiscoverResult

    def features(self) -> Mapping[str, Mapping[str, object]]:
        """Расширения com.boba/*, объявленные сервером: идентификатор →
        настройки; у стороннего сервера их нет."""
        extensions = self.result.capabilities.extensions
        if not extensions:
            return {}

        declared: dict[str, Mapping[str, object]] = {}
        for feature, settings in extensions.items():
            if not feature.startswith(self.FEATURE_PREFIX):
                continue

            declared[feature] = dict(settings)

        return declared

    def journal(self) -> JournalFeature | None:
        """Расширение журнала вызовов; None — сервер его не объявил."""
        return self._feature(JournalFeature)

    def files(self) -> FilesFeature | None:
        """Расширение файлов workspace; None — сервер его не объявил."""
        return self._feature(FilesFeature)

    def nested_calls(self) -> NestedCallsFeature | None:
        """Инструмент сервера со вложенными вызовами и форма его узлов;
        None — сервер такого не объявил."""
        return self._feature(NestedCallsFeature)

    def links(self) -> JournalLinks | None:
        """Адреса каналов журнала по шаблону, объявленному сервером; None —
        журнала нет либо шаблон не по RFC 6570 (причина в логе)."""
        declared = self.journal()
        if declared is None:
            return None

        try:
            return JournalLinks(declared.resource)
        except InvalidUriTemplate as exc:
            logger.warning(
                "%s: extension %s declares resource %r that is not an RFC 6570 "
                "template: %s; the journal of the server is not read",
                self.label,
                JournalFeature.ID,
                declared.resource,
                exc,
            )
            return None

    def _feature(self, model: type[FeatureModel]) -> FeatureModel | None:
        settings = self.features().get(model.ID)
        if settings is None:
            return None

        try:
            return model.model_validate(settings)
        except ValidationError as exc:
            logger.warning(
                "%s: extension %s expects the settings of %s, got %r: %s; the "
                "extension is not declared for this server",
                self.label,
                model.ID,
                model.__name__,
                dict(settings),
                exc,
            )
            return None


class McpToolPort(Protocol):
    """Порт сервера инструментов, которым ретранслятор чата отвечает на
    server/discover, tools/list и tools/call агента.

    Реализации: McpToolServer — сервер MCP профиля по его транспорту;
    ChatToolPort чата — собственные инструменты чата в его процессе.
    Личность клиента у каждого запроса своя: client.
    """

    @abstractmethod
    async def described(self, client: McpClientIdentity) -> McpDescription:
        """Что сервер говорит о себе клиенту client: описание server/discover
        со сроком свежести и расширения boba.

        Ошибки:
        McpClientError — сервер недоступен либо ответил не по схеме.
        MCPError — сервер ответил ошибкой MCP.
        """

    @abstractmethod
    async def tools(self, client: McpClientIdentity) -> mt.ListToolsResult:
        """Инструменты сервера клиенту client одним списком, со сроком
        свежести и областью кэша ответа сервера.

        Ошибки: те же, что у described.
        """

    @abstractmethod
    async def call(
        self,
        client: McpClientIdentity,
        params: mt.CallToolRequestParams,
        card: mt.Tool | None,
        progress: ProgressHeard | None,
        journals: JournalHeard | None,
    ) -> mt.CallToolResult | mt.InputRequiredResult:
        """tools/call клиента client с готовыми аргументами; ответ сервера —
        как есть.

        params — запрос на имя инструмента на сервере, с ответами на прежний
        input_required, если они есть; тред вызова — среди его аргументов.
        card — карточка инструмента из списка, по которому вызов собран: по
        ней сессия зеркалит параметры x-mcp-header и проверяет итог; None —
        карточки у вызывающего нет. progress получает уведомления сервера о
        ходе вызова целиком, с _meta: вызов просит их своим токеном
        прогресса. journals узнаёт журналы, которые вызов открыл на сервере,
        пока он идёт; None — не нужны.

        Ошибки:
        McpClientError — сервер недоступен либо ответа нет: сессия оборвалась
            или вышел срок вызова; либо ответ не по схеме инструмента.
        MCPError — сервер ответил на запрос ошибкой MCP.
        """


class JournalOpenings:
    """Журналы, которые вызовы порта открыли на сервере, пока вызовы идут.

    Сервер с расширением журнала перечисляет журналы идущих вызовов
    вызывающего ресурсами resources/list с JournalOrigin в _meta и сообщает
    о смене списка потоком subscriptions/listen. Создаётся портом
    McpToolServer над отдельным соединением с сервером, когда сервер объявил
    журнал и смену списка ресурсов: запросы основного соединения ждут
    ответа идущего вызова под замком входа OAuth. Вызов
    порта ждёт журналов своего токена прогресса (expecting): первый такой
    вызов поднимает поток подписки в своей задаче и дожидается его
    подтверждения, на каждую смену списка список перечитывается, и новые
    журналы ждущих токенов уходят их слушателям. Сбой потока — в лог:
    журналы тогда узнаются только из итога вызова, следующий вызов
    поднимает поток заново.
    """

    def __init__(
        self, label: str, connection: McpConnection, links: JournalLinks
    ) -> None:
        self._label = label
        self._connection = connection
        self._links = links
        self._expected: dict[mt.ProgressToken, JournalHeard] = {}
        self._reported: dict[mt.ProgressToken, set[tuple[str, str]]] = {}
        self._task: asyncio.Task[None] | None = None
        self._ready: asyncio.Future[bool] | None = None

    @asynccontextmanager
    async def expecting(
        self, token: mt.ProgressToken, heard: JournalHeard
    ) -> AsyncGenerator[None, None]:
        """Журналы, открытые запросом с токеном token, уходят heard, пока
        открыт контекст; поток подписки к этому моменту подтверждён либо
        его нет."""
        await self._listening()
        self._expected[token] = heard
        self._reported[token] = set()
        try:
            yield
        finally:
            self._expected.pop(token, None)
            self._reported.pop(token, None)

    async def close(self) -> None:
        task = self._task
        self._task = None
        self._ready = None
        if task is None:
            return

        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    async def _listening(self) -> bool:
        """Поднимает поток подписки, если его нет, и ждёт подтверждения."""
        if self._task is None or self._task.done():
            self._ready = asyncio.get_running_loop().create_future()
            self._task = asyncio.create_task(
                self._listen(self._ready), name=f"journals:{self._label}"
            )

        ready = self._ready
        if ready is None:
            return False

        return await asyncio.shield(ready)

    async def _listen(self, ready: asyncio.Future[bool]) -> None:
        try:
            session = await self._connection.open(None)
            async with listen(session, resources_list_changed=True) as subscription:
                honored = subscription.honored.resources_list_changed
                if not honored:
                    logger.warning(
                        "%s: subscriptions/listen did not acknowledge "
                        "resourcesListChanged, journals of running calls are "
                        "named only by their results",
                        self._label,
                    )
                    ready.set_result(False)
                    return

                ready.set_result(True)
                async for _event in subscription:
                    await self._relist(session)
        except (
            McpClientError,
            MCPError,
            SubscriptionLost,
            ListenNotSupportedError,
            TimeoutError,
            ValidationError,
        ) as exc:
            logger.warning(
                "%s: the listen stream for journals of running calls ended, "
                "journals are named only by the results of the calls: %s: %s",
                self._label,
                type(exc).__name__,
                exc,
            )
        finally:
            if not ready.done():
                ready.set_result(False)

    async def _relist(self, session: ClientSession) -> None:
        """Список ресурсов целиком; новые журналы ждущих токенов — их
        слушателям."""
        cursor: str | None = None
        while True:
            params = mt.PaginatedRequestParams(cursor=cursor)
            page = await session.list_resources(params=params)
            for resource in page.resources:
                await self._seen(resource)

            cursor = page.next_cursor
            if not cursor:
                return

    async def _seen(self, resource: mt.Resource) -> None:
        meta = resource.meta
        if meta is None:
            return

        raw = meta.get(JournalFeature.ID)
        if raw is None:
            return

        try:
            origin = JournalOrigin.model_validate(raw)
        except ValidationError as exc:
            logger.warning(
                "%s: resource %s carries %s that is not a journal origin: %s",
                self._label,
                resource.uri,
                JournalFeature.ID,
                exc,
            )
            return

        heard = self._expected.get(origin.request)
        if heard is None:
            return

        read = self._links.address(str(resource.uri))
        if read is None:
            return

        reported = self._reported.setdefault(origin.request, set())
        place = (read.run, read.node)
        if place in reported:
            return

        reported.add(place)
        await heard(JournalOpening(run=read.run, call=read.node, node=origin.node))


class McpToolServer(McpToolPort):
    """Порт одного MCP-сервера для одного контекста авторизации: подключения,
    описание, списки и запросы стандарта.

    Создаётся McpServers на сервер с общим входом либо на пару «сервер,
    пользователь» с личным (proxy); подключается при первом запросе.
    Личность клиента — у каждого запроса своя (client): к ней порт добавляет
    расширения, которые исполняет сам чат. С сервером современной редакции у
    порта одно подключение, и личность едет в _meta каждого запроса лёгкой
    сессией SDK над ним (RequestSession); с сервером прежней редакции — своё
    подключение на личность: возможности там часть сессии. Версию сервера
    порт помнит и подключается заново без пробы, пока сервер её не
    опроверг: на отказ -32022 и на отказ 400 без современной ошибки —
    признак сервера прежней редакции — порт закрывает подключение,
    согласует версию заново и повторяет запрос один раз. Описание сервера
    (described) и список инструментов (tools) порт держит кэшем ответов SDK
    по правилам caching: ответ свеж ttlMs, делится по cacheScope,
    сбрасывается уведомлением о смене списка; у сервера прежней редакции —
    на время его сессии. call() шлёт tools/call, остальные методы — запросы
    ресурсов, промптов, подсказок и поток subscriptions/listen; ответ
    сервера отдаётся как есть. Оборванная и забытая сервером сессия
    открывается заново следующим запросом. Вытесненный порт (retire)
    закрывается, когда кончатся идущие через него запросы.
    """

    OWN_EXTENSIONS: ClassVar[Mapping[str, Mapping[str, Any]]] = {
        JournalFeature.ID: {},
        SealFeature.ID: {},
    }
    """Расширения, которые чат объявляет любому серверу: журнал вызовов он
    читает и слушает сам, соединения пользователя запечатывает ключом
    сервера, и сервер по этому предлагает их."""
    NOT_FOUND: ClassVar[frozenset[int]] = frozenset({mt.INVALID_PARAMS, -32002})
    """Коды «ресурса нет» resources/read: -32602 и прежний -32002, которого
    в константах SDK нет."""
    RECOGNIZED: ClassVar[frozenset[int]] = frozenset(ERROR_CODE_HTTP_STATUS)
    """Ошибки, с которыми сервер современной редакции отвечает 400: другое
    тело отказа 400 на современный запрос — сервер прежней редакции."""
    OWN_META: ClassVar[frozenset[str]] = frozenset(
        {
            "progress_token",
            mt.LOG_LEVEL_META_KEY,
            mt.PROTOCOL_VERSION_META_KEY,
            mt.CLIENT_INFO_META_KEY,
            mt.CLIENT_CAPABILITIES_META_KEY,
        }
    )
    """Ключи _meta запроса, которые порт ставит сам либо не несёт: токен
    прогресса (имя поля модели), уровень логов и конверт версии и личности —
    его ставит сессия запроса."""
    READ_REPLY: ClassVar[
        TypeAdapter[mt.ReadResourceResult | mt.InputRequiredResult]
    ] = TypeAdapter(mt.ReadResourceResult | mt.InputRequiredResult)
    """Ответ на resources/read: содержимое либо просьба о вводе."""
    PROMPT_REPLY: ClassVar[TypeAdapter[mt.GetPromptResult | mt.InputRequiredResult]] = (
        TypeAdapter(mt.GetPromptResult | mt.InputRequiredResult)
    )
    """Ответ на prompts/get: сообщения промпта либо просьба о вводе."""

    def __init__(self, name: str, config: McpServerConfig) -> None:
        self._name = name
        self._config = config
        self._label = f"mcp server {name!r} ({config.endpoint.label()})"
        extensions = dict(self.OWN_EXTENSIONS)
        if config.apps is not None:
            extensions[EXTENSION_ID] = config.apps.settings()

        self._extensions = extensions
        self._own = McpServers.OWN.with_extensions(extensions)
        self._refusals = HttpRefusals(self._label)
        self._line = self._connection(self._own)
        self._sessions: dict[str, McpConnection] = {}
        """Подключения сервера прежней редакции по личностям клиента."""
        self._version: str | None = None
        """Версия, которую сервер согласовал последней; None — порт её ещё
        не знает либо сервер её опроверг."""
        self._probing = asyncio.Lock()
        # замок входа OAuth ждёт ответа вызова: журналы — своим соединением
        self._watching = self._connection(self._own)
        self._openings: JournalOpenings | None = None
        self._files: dict[str, McpFiles] = {}
        self._running = 0
        self._retired = False

    async def described(self, client: McpClientIdentity) -> McpDescription:
        identity = self._speaking(client)
        send = partial(self._discovered, identity)
        result = await self._exchange(identity, CachedMethod.DISCOVER, send)

        return McpDescription(self._label, result)

    async def tools(self, client: McpClientIdentity) -> mt.ListToolsResult:
        identity = self._speaking(client)
        send = partial(self._listed, identity, "use")

        return await self._exchange(identity, CachedMethod.TOOLS, send)

    async def files(self) -> McpFiles | None:
        """Файлы workspace на сервере; None — сервер их не объявил.

        Ошибки: те же, что у described.
        """
        described = await self.described(McpServers.OWN)
        declared = described.files()
        if declared is None:
            return None

        return self._files_at(declared.path)

    async def workspace(self) -> str | None:
        """Каталог workspace, под которым песочница сервера видит файлы
        workspace: по нему ключи файлов в итогах сервера разбирает тот, кто
        держит этот порт; None — сервер файлов не объявил.

        Ошибки: те же, что у described.
        """
        described = await self.described(McpServers.OWN)
        declared = described.files()
        if declared is None:
            return None

        return declared.workspace

    async def journal_files(self) -> McpFiles | None:
        """Журналы вызовов сервера целыми файлами: канал журнала лежит по
        пути `{run}/{node}/{channel}`; None — сервер такого не объявил.

        Ошибки: те же, что у described.
        """
        described = await self.described(McpServers.OWN)
        declared = described.journal()
        if declared is None:
            return None

        return self._files_at(declared.path)

    async def call(
        self,
        client: McpClientIdentity,
        params: mt.CallToolRequestParams,
        card: mt.Tool | None,
        progress: ProgressHeard | None,
        journals: JournalHeard | None,
    ) -> mt.CallToolResult | mt.InputRequiredResult:
        identity = self._speaking(client)
        token = uuid4().hex
        sent = params.model_copy(update={"meta": self._sent_meta(params.meta, token)})
        expecting: AbstractAsyncContextManager[None] = nullcontext()
        if journals is not None:
            expecting = await self._expecting(client, token, journals)

        async def send(
            connection: McpConnection, session: IdentifiedSession
        ) -> mt.CallToolResult | mt.InputRequiredResult:
            async with self._heard(connection, token, progress):
                return await self._tool_call(identity, connection, session, sent, card)

        async with expecting:
            return await self._exchange(
                identity, f"tools/call of {params.name!r}", send
            )

    async def list_resources(
        self, client: McpClientIdentity, params: mt.PaginatedRequestParams | None
    ) -> mt.ListResourcesResult:
        """resources/list одной страницей курсора params; ответ как есть.

        Ошибки:
        McpClientError — сервер недоступен или ответил не по схеме.
        MCPError — сервер ответил ошибкой MCP.
        """
        request = mt.ListResourcesRequest(params=params)

        return await self._request(client, request, mt.ListResourcesResult, None)

    async def list_resource_templates(
        self, client: McpClientIdentity, params: mt.PaginatedRequestParams | None
    ) -> mt.ListResourceTemplatesResult:
        """resources/templates/list одной страницей; ответ как есть.

        Ошибки: те же, что у list_resources.
        """
        request = mt.ListResourceTemplatesRequest(params=params)

        return await self._request(
            client, request, mt.ListResourceTemplatesResult, None
        )

    async def read_resource(
        self,
        client: McpClientIdentity,
        params: mt.ReadResourceRequestParams,
        progress: ProgressHeard | None,
    ) -> mt.ReadResourceResult | mt.InputRequiredResult:
        """resources/read: содержимое ресурса либо просьба о вводе
        (input_required), с ответами на прежнюю, если они есть в params.

        Ошибки: те же, что у list_resources.
        """
        request = mt.ReadResourceRequest(params=params)

        return await self._request(client, request, self.READ_REPLY, progress)

    async def list_prompts(
        self, client: McpClientIdentity, params: mt.PaginatedRequestParams | None
    ) -> mt.ListPromptsResult:
        """prompts/list одной страницей; ответ как есть.

        Ошибки: те же, что у list_resources.
        """
        request = mt.ListPromptsRequest(params=params)

        return await self._request(client, request, mt.ListPromptsResult, None)

    async def get_prompt(
        self,
        client: McpClientIdentity,
        params: mt.GetPromptRequestParams,
        progress: ProgressHeard | None,
    ) -> mt.GetPromptResult | mt.InputRequiredResult:
        """prompts/get: сообщения промпта либо просьба о вводе.

        Ошибки: те же, что у list_resources.
        """
        request = mt.GetPromptRequest(params=params)

        return await self._request(client, request, self.PROMPT_REPLY, progress)

    async def complete(
        self, client: McpClientIdentity, params: mt.CompleteRequestParams
    ) -> mt.CompleteResult:
        """completion/complete: варианты значения аргумента промпта или
        шаблона ресурса.

        Ошибки: те же, что у list_resources.
        """
        request = mt.CompleteRequest(params=params)

        return await self._request(client, request, mt.CompleteResult, None)

    @asynccontextmanager
    async def listen(
        self, client: McpClientIdentity, notifications: mt.SubscriptionFilter
    ) -> AsyncGenerator[Subscription, None]:
        """Поток subscriptions/listen клиента client на события
        notifications; поток живёт, пока открыт контекст, подтверждённый
        сервером набор — subscription.honored. Событие сбрасывает кэш порта,
        к которому относится, раньше, чем его получит читатель потока.

        Ошибки:
        McpClientError — сервер недоступен, подписку не поддерживает или
            поток оборвался без штатного закрытия.
        MCPError — сервер отказал в подписке ошибкой MCP.
        """
        identity = self._speaking(client)
        uris: list[str] = []
        if notifications.resource_subscriptions is not None:
            uris.extend(notifications.resource_subscriptions)

        label = f"{self._label}: subscriptions/listen"
        async with self._busy():
            connection, line = await self._opened(identity)
            with self._refusals.watching() as refusal:
                try:
                    async with (
                        listen(
                            self._asking(line, identity),
                            tools_list_changed=bool(notifications.tools_list_changed),
                            prompts_list_changed=bool(
                                notifications.prompts_list_changed
                            ),
                            resources_list_changed=bool(
                                notifications.resources_list_changed
                            ),
                            resource_subscriptions=uris,
                            on_event=connection.heard,
                        ) as subscription,
                    ):
                        yield subscription
                except ListenNotSupportedError as exc:
                    msg = (
                        f"{label}: the negotiated protocol has no listen streams: {exc}"
                    )
                    raise McpClientError(msg) from exc
                except MCPError as exc:
                    await self._unanswered(
                        exc, "subscriptions/listen", refusal, connection
                    )
                    raise
                except SubscriptionLost as exc:
                    msg = f"{label}: the stream ended without the server's close: {exc}"
                    raise McpClientError(msg) from exc
                except TimeoutError as exc:
                    msg = (
                        f"{label}: the server did not acknowledge the subscription: "
                        f"{exc}"
                    )
                    raise McpClientError(msg) from exc

    async def journal(self, request: JournalRead) -> StreamSlice | None:
        """Окно журнала вызова ресурсом resources/read; None — сервер журнал
        не объявлял либо такого журнала у него нет.

        Ошибки:
        McpClientError — сервер недоступен или ответил не окном журнала.
        """
        described = await self.described(McpServers.OWN)
        links = described.links()
        if links is None:
            return None

        uri = links.uri(request)
        what = (
            f"{self._label}: reading the journal of call {request.node!r} in run "
            f"{request.run!r} as resource {uri}"
        )
        params = mt.ReadResourceRequestParams(uri=uri)
        try:
            read = await self.read_resource(McpServers.OWN, params, None)
        except MCPError as exc:
            if exc.error.code in self.NOT_FOUND:
                return None

            msg = f"{what} failed: MCP error {exc.error.code}: {exc.error.message}"
            raise McpClientError(msg) from exc

        if not isinstance(read, mt.ReadResourceResult):
            msg = f"{what}: the server asked for more input, expected a window"
            raise McpClientError(msg)

        return self._window(what, read)

    async def retire(self) -> None:
        """Порт вытеснен из McpServers: закрывается сразу либо, если через
        него идут запросы, когда кончится последний."""
        self._retired = True
        if self._running:
            return

        await self.close()

    async def close(self) -> None:
        if self._openings is not None:
            await self._openings.close()
            self._openings = None

        await self._watching.close()
        for files in self._files.values():
            await files.close()

        self._files.clear()
        for connection in self._sessions.values():
            await connection.close()

        await self._line.close()

    def _connection(self, identity: McpClientIdentity) -> McpConnection:
        return McpConnection(
            self._config.endpoint,
            self._config.connect_timeout_sec,
            identity,
            self._refusals,
        )

    def _speaking(self, client: McpClientIdentity) -> McpClientIdentity:
        """Личность client с расширениями, которые исполняет сам чат."""
        return client.with_extensions(self._extensions)

    async def _exchange(
        self, identity: McpClientIdentity, action: str, send: PortSend[SentT]
    ) -> SentT:
        """Запрос action личности identity, который send шлёт сессией
        запроса.

        Ошибки:
        McpClientError — сервер недоступен, ответа нет либо ответ не по схеме.
        MCPError — сервер ответил ошибкой MCP.
        """
        async with self._busy():
            return await self._sent(identity, action, send, True)

    async def _sent(
        self,
        identity: McpClientIdentity,
        action: str,
        send: PortSend[SentT],
        again: bool,
    ) -> SentT:
        """Запрос с повтором на опровергнутую сервером версию (again)."""
        connection, line = await self._opened(identity)
        with self._refusals.watching() as refusal:
            try:
                return await send(connection, self._asking(line, identity))
            except MCPError as exc:
                if again and self._left(exc, refusal, line):
                    await self._forget(connection, action, exc)
                    return await self._sent(identity, action, send, False)

                await self._unanswered(exc, action, refusal, connection)
                raise
            except ValidationError as exc:
                msg = f"{self._label}: {action} answered off the schema: {exc}"
                raise McpClientError(msg) from exc

    @asynccontextmanager
    async def _busy(self) -> AsyncGenerator[None, None]:
        """Запрос идёт через порт, пока открыт контекст."""
        self._running += 1
        try:
            yield
        finally:
            await self._released()

    async def _released(self) -> None:
        self._running -= 1
        if not self._retired:
            return

        if self._running:
            return

        await self.close()

    async def _opened(
        self, identity: McpClientIdentity
    ) -> tuple[McpConnection, IdentifiedSession]:
        """Подключение для запроса личности identity и его сессия: пробу
        версии делает один запрос, остальные её ждут."""
        if self._version is not None:
            return await self._settled(identity)

        async with self._probing:
            return await self._settled(identity)

    async def _settled(
        self, identity: McpClientIdentity
    ) -> tuple[McpConnection, IdentifiedSession]:
        """Подключение порта с сервером современной редакции либо
        подключение личности с сервером прежней; подключение, на котором
        сервер согласовал современную редакцию, становится подключением
        порта."""
        version = self._version
        if version in MODERN_PROTOCOL_VERSIONS:
            return self._line, await self._open(self._line, version)

        key = identity.key()
        connection = self._sessions.get(key)
        if connection is None:
            connection = self._connection(identity)
            self._sessions[key] = connection

        session = await self._open(connection, None)
        negotiated = session.protocol_version
        self._version = negotiated
        if negotiated not in MODERN_PROTOCOL_VERSIONS:
            return connection, session

        self._sessions.pop(key)
        await self._line.close()
        self._line = connection

        return connection, session

    async def _open(
        self, connection: McpConnection, version: str | None
    ) -> IdentifiedSession:
        """Открытая сессия подключения; неудача подключения называет сервер."""
        try:
            return await connection.open(version)
        except McpClientError as exc:
            msg = f"mcp server {self._name!r}: {exc}"
            raise McpClientError(msg) from exc

    def _asking(
        self, line: IdentifiedSession, identity: McpClientIdentity
    ) -> IdentifiedSession:
        """Сессия запроса личности identity: у сервера современной редакции
        — лёгкая сессия над подключением порта, у прежней — сессия личности
        сама."""
        if line.protocol_version not in MODERN_PROTOCOL_VERSIONS:
            return line

        return RequestSession(line, identity)

    def _left(
        self, error: MCPError, refusal: HttpRefusal, line: IdentifiedSession
    ) -> bool:
        """Сервер опроверг версию, которую порт за ним помнит: отказ -32022
        либо, на современный запрос, 400 без современной ошибки — так
        отвечает сервер прежней редакции."""
        if error.error.code == mt.UNSUPPORTED_PROTOCOL_VERSION:
            return True

        if line.protocol_version not in MODERN_PROTOCOL_VERSIONS:
            return False

        if not refusal.bad_request():
            return False

        return error.error.code not in self.RECOGNIZED

    async def _forget(
        self, connection: McpConnection, action: str, error: MCPError
    ) -> None:
        """Версия, которую сервер опроверг, забыта: подключение закрыто,
        следующий запрос согласует её заново."""
        logger.info(
            "%s: %s was refused for the protocol version %s, the version is "
            "negotiated again: MCP error %s: %s",
            self._label,
            action,
            self._version,
            error.error.code,
            error.error.message,
        )
        self._version = None
        await connection.close()

    async def _unanswered(
        self,
        error: MCPError,
        action: str,
        refusal: HttpRefusal,
        connection: McpConnection,
    ) -> None:
        """Запрос action остался без ответа — оборвалось подключение, вышел
        срок или сервер забыл сессию (её требует открыть заново 2025-11-25):
        наверх уходит McpClientError, оборванное и забытое подключение
        закрывается, следующий запрос подключится заново. Иную ошибку MCP
        вызывающий поднимает как есть."""
        if refusal.forgotten():
            await connection.close()
            msg = (
                f"{self._label}: {action} was refused, the server forgot the "
                "session of the client (HTTP 404 with Mcp-Session-Id); the next "
                f"request opens a new session: {refusal.text(error)}"
            )
            raise McpClientError(msg) from error

        if error.error.code == mt.CONNECTION_CLOSED:
            await connection.close()
            msg = f"{self._label}: {action} got no answer: {refusal.text(error)}"
            raise McpClientError(msg) from error

        if error.error.code == mt.REQUEST_TIMEOUT:
            msg = f"{self._label}: {action} got no answer: {refusal.text(error)}"
            raise McpClientError(msg) from error

    async def _discovered(
        self,
        identity: McpClientIdentity,
        connection: McpConnection,
        session: IdentifiedSession,
    ) -> mt.DiscoverResult:
        """Описание сервера: у современной редакции — server/discover через
        кэш личности, у прежней — initialize её сессии."""
        version = session.protocol_version
        if version not in MODERN_PROTOCOL_VERSIONS:
            return self._initialized(session)

        cache = connection.cache(identity)
        hit = await cache.read(CachedMethod.DISCOVER, "")
        if isinstance(hit, mt.DiscoverResult):
            return hit

        generation = cache.capture(CachedMethod.DISCOVER, "")
        raw = await session.send_discover(version)
        discovered = mt.DiscoverResult.model_validate(raw)
        await cache.write(CachedMethod.DISCOVER, "", discovered, generation, "use")

        return discovered

    def _initialized(self, session: IdentifiedSession) -> mt.DiscoverResult:
        """Описание сервера прежней редакции из initialize его сессии: без
        потоков subscriptions/listen — без смены списков и подписки; срока
        свежести у него нет.

        Ошибки:
        McpClientError — сессия без согласованной версии и возможностей.
        """
        version = session.protocol_version
        if version is None:
            msg = f"{self._label}: the session has no negotiated protocol version"
            raise McpClientError(msg)

        capabilities = session.server_capabilities
        if capabilities is None:
            msg = f"{self._label}: the session has no capabilities of the server"
            raise McpClientError(msg)

        meta: dict[str, Any] = {}
        info = session.server_info
        if info is not None:
            dumped = info.model_dump(mode="json", by_alias=True, exclude_none=True)
            meta[mt.SERVER_INFO_META_KEY] = dumped

        return mt.DiscoverResult(
            supported_versions=[version],
            capabilities=self._unheard(capabilities),
            instructions=session.instructions,
            _meta=meta,
        )

    @staticmethod
    def _unheard(capabilities: mt.ServerCapabilities) -> mt.ServerCapabilities:
        """Возможности без смены списков и подписки на ресурсы."""
        update: dict[str, Any] = {}
        if capabilities.tools is not None:
            update["tools"] = capabilities.tools.model_copy(
                update={"list_changed": False}
            )

        if capabilities.prompts is not None:
            update["prompts"] = capabilities.prompts.model_copy(
                update={"list_changed": False}
            )

        if capabilities.resources is not None:
            update["resources"] = capabilities.resources.model_copy(
                update={"list_changed": False, "subscribe": False}
            )

        return capabilities.model_copy(update=update)

    async def _listed(
        self,
        identity: McpClientIdentity,
        mode: Literal["use", "refresh"],
        connection: McpConnection,
        session: IdentifiedSession,
    ) -> mt.ListToolsResult:
        """Список инструментов через кэш личности: свежий — без запроса;
        refresh — перечитывается в любом случае."""
        cache = connection.cache(identity)
        if mode == "use":
            hit = await cache.read(CachedMethod.TOOLS, "")
            if isinstance(hit, mt.ListToolsResult):
                return hit

        generation = cache.capture(CachedMethod.TOOLS, "")
        listed = await self._pages(session)
        await cache.write(CachedMethod.TOOLS, "", listed, generation, mode)

        return listed

    async def _pages(self, session: IdentifiedSession) -> mt.ListToolsResult:
        """Список инструментов целиком: страницы курсора одним ответом; срок
        — наименьший из сроков страниц, область — private, если хоть одна
        страница private."""
        first = await session.list_tools(params=mt.PaginatedRequestParams())
        cursor = first.next_cursor
        if not cursor:
            return first

        tools = list(first.tools)
        ttl_ms = first.ttl_ms
        scope = first.cache_scope
        while cursor:
            page = await session.list_tools(
                params=mt.PaginatedRequestParams(cursor=cursor)
            )
            tools.extend(page.tools)
            ttl_ms = min(ttl_ms, page.ttl_ms)
            if page.cache_scope == "private":
                scope = "private"

            cursor = page.next_cursor

        update: dict[str, Any] = {"tools": tools, "next_cursor": None}
        if "ttl_ms" in first.model_fields_set:
            update["ttl_ms"] = ttl_ms
            update["cache_scope"] = scope

        return first.model_copy(update=update)

    async def _tool_call(
        self,
        identity: McpClientIdentity,
        connection: McpConnection,
        session: IdentifiedSession,
        params: mt.CallToolRequestParams,
        card: mt.Tool | None,
    ) -> mt.CallToolResult | mt.InputRequiredResult:
        """tools/call сессией запроса; отказ HeaderMismatch — сессии не
        хватило схемы инструмента для заголовков Mcp-Param-*: список
        перечитывается, вызов повторяется один раз."""
        if card is not None:
            session.absorbed([card])

        try:
            return await self._asked(session, params)
        except MCPError as exc:
            if exc.error.code != mt.HEADER_MISMATCH:
                raise

            logger.info(
                "%s: tools/call of %r was refused for its Mcp-Param headers, "
                "the tool list is read again: %s",
                self._label,
                params.name,
                exc.error.message,
            )

        await self._listed(identity, "refresh", connection, session)

        return await self._asked(session, params)

    async def _asked(
        self, session: IdentifiedSession, params: mt.CallToolRequestParams
    ) -> mt.CallToolResult | mt.InputRequiredResult:
        try:
            return await session.call_tool(
                params.name,
                params.arguments,
                read_timeout_seconds=self._config.call_timeout_sec,
                input_responses=params.input_responses,
                request_state=params.request_state,
                meta=params.meta,
                allow_input_required=True,
            )
        except RuntimeError as exc:
            msg = (
                f"{self._label}: tools/call of {params.name!r} was answered off "
                f"the tool: the result does not match its output schema or is an "
                f"extension result the client did not ask for: {exc}"
            )
            raise McpClientError(msg) from exc

    async def _request(
        self,
        client: McpClientIdentity,
        request: mt.ClientRequest,
        result: type[ResultT] | TypeAdapter[ResultT],
        progress: ProgressHeard | None,
    ) -> ResultT:
        """Запрос request клиента client серверу со сроком вызова; ход
        запроса — progress под своим токеном прогресса клиента вместо
        токена запроса."""
        identity = self._speaking(client)
        token = uuid4().hex
        sent = request
        params = request.params
        if params is not None:
            meta = self._sent_meta(params.meta, token)
            sent = request.model_copy(
                update={"params": params.model_copy(update={"meta": meta})}
            )

        async def send(
            connection: McpConnection, session: IdentifiedSession
        ) -> ResultT:
            async with self._heard(connection, token, progress):
                return await session.send_request(
                    sent,
                    result,
                    request_read_timeout_seconds=self._config.call_timeout_sec,
                )

        return await self._exchange(identity, request.method, send)

    def _sent_meta(
        self, meta: mt.RequestParamsMeta | None, token: str
    ) -> mt.RequestParamsMeta:
        """_meta запроса серверу: ключи запроса как есть, кроме токена
        прогресса — у запроса свой токен клиента token, его слушает
        подключение — и уровня логов: логов сервера клиент не несёт. Ключи
        личности клиента ставит сессия запроса."""
        sent = mt.RequestParamsMeta(progress_token=token)
        if meta is None:
            return sent

        for key, value in meta.items():
            if key in self.OWN_META:
                continue

            sent[key] = value

        return sent

    def _heard(
        self,
        connection: McpConnection,
        token: mt.ProgressToken,
        progress: ProgressHeard | None,
    ) -> AbstractAsyncContextManager[None]:
        if progress is None:
            return nullcontext()

        return connection.listening(token, progress)

    async def _expecting(
        self, client: McpClientIdentity, token: str, journals: JournalHeard
    ) -> AbstractAsyncContextManager[None]:
        """Слежение за журналами вызова с токеном token, если сервер объявил
        журнал и смену списка ресурсов."""
        openings = await self._journal_openings(client)
        if openings is None:
            return nullcontext()

        return openings.expecting(token, journals)

    async def _journal_openings(
        self, client: McpClientIdentity
    ) -> JournalOpenings | None:
        if self._openings is not None:
            return self._openings

        described = await self.described(client)
        links = described.links()
        if links is None:
            return None

        resources = described.result.capabilities.resources
        if resources is None:
            return None

        if not resources.list_changed:
            return None

        self._openings = JournalOpenings(self._label, self._watching, links)

        return self._openings

    def _files_at(self, path: str) -> McpFiles | None:
        """Клиент файлов сервера под маршрутом path; у сервера не по HTTP
        файлов нет."""
        endpoint = self._config.endpoint
        if not isinstance(endpoint, HttpAddress):
            return None

        files = self._files.get(path)
        if files is not None:
            return files

        files = McpFiles(self._name, endpoint, path)
        self._files[path] = files

        return files

    def _window(self, what: str, read: mt.ReadResourceResult) -> StreamSlice:
        """Окно журнала из содержимого ресурса: текст и координаты окна в
        _meta под ключом расширения журнала."""
        if len(read.contents) != 1:
            msg = f"{what}: expected one text content, got {len(read.contents)}"
            raise McpClientError(msg)

        (content,) = read.contents
        if not isinstance(content, mt.TextResourceContents):
            msg = f"{what}: expected a text content, got {type(content).__name__}"
            raise McpClientError(msg)

        meta = content.meta
        if meta is None:
            msg = f"{what}: the content names no window place under {JournalFeature.ID}"
            raise McpClientError(msg)

        place = meta.get(JournalFeature.ID)
        if not isinstance(place, Mapping):
            msg = (
                f"{what}: expected the window place under {JournalFeature.ID}, "
                f"got {place!r}"
            )
            raise McpClientError(msg)

        try:
            return StreamSlice.model_validate({**place, "text": content.text})
        except ValidationError as exc:
            msg = f"{what}: the window place is off the journal window: {exc}"
            raise McpClientError(msg) from exc


class McpServers:
    """MCP-серверы клиента: порты по имени сервера и пользователю.

    Создаётся сборкой приложения из секции [mcp]. Порт сервера с общей
    авторизацией — один на процесс, сервера с личной (proxy) — свой у
    каждого пользователя: кэши ответов с cacheScope private не делятся
    между контекстами авторизации. Личность клиента порт принимает каждым
    запросом: собственные запросы чата идут от личности OWN, ретранслятор —
    от личности агента. Порт подключается при первом запросе; сервер,
    который не ответил, отказывает ошибкой McpClientError тому, кто его
    позвал, и следующий запрос подключает его заново. Давно не нужный
    личный порт вытесняется и закрывается, как только через него кончатся
    идущие запросы.
    """

    KEEP: ClassVar[int] = 256
    """Сколько личных портов держать."""

    OWN: ClassVar[McpClientIdentity] = McpClientIdentity(
        info=mt.Implementation(name="boba-aid", version="1"),
        capabilities=mt.ClientCapabilities(),
    )
    """Личность чата для его собственных запросов: панель, приложения, лента."""

    def __init__(self, config: McpServersConfig) -> None:
        self._configs = dict(config.servers)
        self._shared: dict[str, McpToolServer] = {}
        self._personal: OrderedDict[tuple[str, McpCaller], McpToolServer]
        self._personal = OrderedDict()

    async def stop(self) -> None:
        for server in self._shared.values():
            await server.close()

        for server in self._personal.values():
            await server.close()

    def served(self, name: str) -> bool:
        """Сервер name назван в [mcp.servers]."""
        return name in self._configs

    def apps(self, name: str) -> McpServerApps | None:
        """Секция apps сервера name; None — сервер не назван либо
        показывает итоги блоками, приложения его не рисуются."""
        config = self._configs.get(name)
        if config is None:
            return None

        return config.apps

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

        port = await self.port(server, caller)

        return await port.journal(request)

    async def files(self, server: str, caller: McpCaller) -> McpFiles | None:
        """Файлы workspace на сервере server от имени caller; None — сервер
        не назван в [mcp.servers] или файлов не объявил.

        Ошибки:
        McpClientError — сервер недоступен.
        """
        if server not in self._configs:
            return None

        port = await self.port(server, caller)

        return await port.files()

    async def workspace(self, server: str, caller: McpCaller) -> str | None:
        """Каталог workspace сервера server для caller; None — сервер не
        назван в [mcp.servers] или файлов не объявил.

        Ошибки:
        McpClientError — сервер недоступен.
        """
        if server not in self._configs:
            return None

        port = await self.port(server, caller)

        return await port.workspace()

    async def journal_files(self, server: str, caller: McpCaller) -> McpFiles | None:
        """Журналы вызовов сервера server целыми файлами от имени caller;
        None — сервер не назван или такого не объявил.

        Ошибки:
        McpClientError — сервер недоступен.
        """
        if server not in self._configs:
            return None

        port = await self.port(server, caller)

        return await port.journal_files()

    async def port(self, name: str, caller: McpCaller) -> McpToolServer:
        """Порт сервера name для пользователя caller: общий либо его личный.

        Ошибки:
        McpClientError — имя не названо в [mcp.servers].
        """
        config = self._configs.get(name)
        if config is None:
            msg = (
                f"mcp servers of {caller.login!r}: server {name!r} is not "
                f"declared in [mcp.servers], declared: {sorted(self._configs)}"
            )
            raise McpClientError(msg)

        if not config.endpoint.personal():
            shared = self._shared.get(name)
            if shared is None:
                shared = McpToolServer(name, config)
                self._shared[name] = shared

            return shared

        key = (name, caller)
        server = self._personal.get(key)
        if server is None:
            server = McpToolServer(name, config.on_behalf_of(caller))
            self._personal[key] = server

        self._personal.move_to_end(key)
        while len(self._personal) > self.KEEP:
            _stale, evicted = self._personal.popitem(last=False)
            await evicted.retire()

        return server
