"""Клиент MCP-серверов: подключения, списки, вызовы и потоки подписки.

Клиент работает с любым сервером стандарта MCP 2026-07-28: согласует
протокол server/discover, читает списки инструментов, ресурсов, шаблонов и
промптов, исполняет tools/call, resources/read, prompts/get и
completion/complete и держит потоки subscriptions/listen, отдавая ответ
сервера как есть. Серверу клиент представляется личностью McpClientIdentity:
своей либо агента, чьи запросы ретранслирует чат. Транспорт — streamable
HTTP, SSE или stdio. Способ входа — свойство записи сервера (McpAuth): без
входа, готовый токен, вход local учёткой с паролем и вход proxy
утверждением о пользователе. Токен сервера для local и proxy получают и
обновляют штатные провайдеры OAuth библиотеки mcp; своё здесь — только
проход страницы входа без человека (UnattendedLogin).

Ошибки:
McpClientError — сервер недоступен, не прошёл согласование server/discover,
    не отдал список инструментов, отказал во входе, не ответил на запрос,
    ответил не по схеме, отказал в подписке либо порт позвали до open().
MCPError (mcp.shared.exceptions) — сервер ответил на запрос ошибкой MCP.
"""

from __future__ import annotations

import asyncio
import json
import logging
from abc import abstractmethod
from collections import OrderedDict
from collections.abc import (
    AsyncGenerator,
    AsyncIterator,
    Awaitable,
    Callable,
    Mapping,
    Sequence,
)
from contextlib import (
    AbstractAsyncContextManager,
    asynccontextmanager,
    nullcontext,
    suppress,
)
from dataclasses import dataclass, replace
from typing import Annotated, Any, ClassVar, Literal, Protocol, Self, TypeVar
from urllib.parse import quote, urlsplit, urlunsplit
from uuid import uuid4

import httpx2
import mcp.types as mt
from mcp import ClientSession, StdioServerParameters
from mcp.client.auth import OAuthClientProvider, TokenStorage
from mcp.client.auth.extensions.identity_assertion import (
    IdentityAssertionOAuthProvider,
)
from mcp.client.session import IncomingMessage, MessageHandlerFnT
from mcp.client.sse import sse_client
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client
from mcp.client.subscriptions import (
    ListenNotSupportedError,
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
from mcp.shared.exceptions import MCPError
from mcp.shared.message import SessionMessage
from mcp.shared.uri_template import InvalidUriTemplate, UriTemplate
from mcp.types.version import MODERN_PROTOCOL_VERSIONS
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
from boba.canvas.keys import ObjectKey, WorkspaceMount
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


class Transport(Protocol):
    """Транспорт MCP-сервера: открывает потоки сообщений сессии."""

    @abstractmethod
    def opened(self) -> AbstractAsyncContextManager[Streams]:
        """Потоки сессии; открыты, пока открыт контекст."""

    @abstractmethod
    def on_behalf_of(self, caller: McpCaller) -> Transport:
        """Тот же транспорт со входом от имени пользователя клиента."""

    @abstractmethod
    def label(self) -> str:
        """Адрес сервера для сообщений об ошибках и логов."""


class ErrorCauses:
    """Причины сбоя одной строкой: группа исключений anyio раскрывается до
    своих листьев, иначе в тексте остаётся «unhandled errors in a TaskGroup».
    Создаётся соединением и портом McpToolServer."""

    def text(self, error: BaseException) -> str:
        return "; ".join(self._leaves(error))

    def _leaves(self, error: BaseException) -> list[str]:
        if isinstance(error, BaseExceptionGroup):
            leaves: list[str] = []
            for inner in error.exceptions:
                leaves.extend(self._leaves(inner))

            return leaves

        return [f"{type(error).__name__}: {error}"]


class HttpRefusals:
    """Отказы HTTP одного подключения к серверу.

    Создаётся транспортом HttpEndpoint на каждое открытие и ставится
    наблюдателем ответов его HTTP-клиента. Библиотека mcp заменяет ответ с
    отказом (401, 403, 502) безликой ошибкой протокола; по последнему
    отказу запроса POST транспорт возвращает в текст ошибки адрес и статус.
    Отказ на GET не считается: им сервер без потока уведомлений отвечает
    исправной сессии. К отказу добавляется причина, которую назвал клиент
    (refused): так видно и статус, и что именно сервер отверг.
    """

    FIRST_REFUSAL: ClassVar[int] = 400
    REQUEST: ClassVar[str] = "POST"

    def __init__(self) -> None:
        self._last: str | None = None
        self._causes = ErrorCauses()

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

    def refused(self, error: BaseException) -> McpClientError | None:
        """Отказ HTTP с причиной error; None — отказа не было."""
        last = self._last
        if last is None:
            return None

        return McpClientError(f"{last}; {self._causes.text(error)}")


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
    async def opened(self) -> AsyncGenerator[Streams, None]:
        auth = self.auth.httpx_auth(self.url())
        refusals = HttpRefusals()
        async with create_mcp_http_client(auth=auth) as http:
            http.event_hooks["response"].append(refusals.seen)
            async with streamable_http_client(self.url(), http_client=http) as streams:
                try:
                    yield streams
                except Exception as exc:
                    refused = refusals.refused(exc)
                    if refused is None:
                        raise

                    raise refused from exc


class SseEndpoint(HttpAddress):
    """Сервер по SSE (прежний HTTP-транспорт MCP). Модель конфига; транспорт
    — она же (протокол Transport)."""

    transport: Literal["sse"] = "sse"

    def label(self) -> str:
        return self.url()

    @asynccontextmanager
    async def opened(self) -> AsyncGenerator[Streams, None]:
        auth = self.auth.httpx_auth(self.url())
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

    def on_behalf_of(self, caller: McpCaller) -> Self:
        return self

    @asynccontextmanager
    async def opened(self) -> AsyncGenerator[Streams, None]:
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
    ретранслирует за агента, идут с личностью агента из его запроса
    (McpServers.relayed): сервер видит настоящего клиента и его
    возможности. Расширения, которые исполняет сам чат (журнал,
    запечатывание соединений, MCP Apps), McpToolServer добавляет к любой
    личности.
    """

    info: mt.Implementation
    capabilities: mt.ClientCapabilities

    def key(self) -> str:
        """Одна строка на одну личность: по ней McpServers различает
        подключения."""
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

    Создаётся соединением McpConnection. SDK собирает clientCapabilities
    _meta запроса сам из обработчиков сессии; ретранслятор чата обязан
    передать серверу возможности агента как есть, поэтому сборка
    возможностей здесь отдаёт их из личности клиента.
    """

    def __init__(
        self,
        read_stream: ReadStream[SessionMessage | Exception],
        write_stream: WriteStream[SessionMessage],
        identity: McpClientIdentity,
        message_handler: MessageHandlerFnT,
    ) -> None:
        super().__init__(
            read_stream,
            write_stream,
            client_info=identity.info,
            message_handler=message_handler,
        )
        self._identity = identity

    def _build_capabilities(self, version: str) -> mt.ClientCapabilities:
        return self._identity.capabilities


class McpConnection:
    """Живая сессия MCP с одним сервером.

    Создаётся портом McpToolServer. Транспорт и сессия библиотеки mcp
    открываются и закрываются в одной собственной задаче — их области отмены
    привязаны к задаче, которая их открыла; запросы идут из задач вызовов.
    Протокол согласует server/discover; сервер, который на него не отвечает
    (эпоха рукопожатия initialize), отвергается. Сессия говорит от личности
    identity. Уведомления прогресса сессия отдаёт целиком, с _meta: каждое
    уходит обработчику запроса, который слушает его токен (listening).
    """

    def __init__(
        self,
        transport: Transport,
        connect_timeout_sec: float,
        identity: McpClientIdentity,
    ) -> None:
        self._transport = transport
        self._connect_timeout_sec = connect_timeout_sec
        self._identity = identity
        self._causes = ErrorCauses()
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._ready: asyncio.Future[ClientSession] | None = None
        self._listeners: dict[
            mt.ProgressToken,
            Callable[[mt.ProgressNotificationParams], Awaitable[None]],
        ] = {}

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
        """Сессия живёт в этой задаче. Протокол согласует server/discover:
        сервер прежней спецификации (рукопожатие initialize) на него не
        отвечает, и клиент его отвергает. Отказ HTTP за время согласования
        называет транспорт (HttpRefusals)."""
        stop = self._stop
        try:
            async with self._transport.opened() as (read, write):
                session = IdentifiedSession(read, write, self._identity, self._heard)
                async with session:
                    await self._discover(session)
                    ready.set_result(session)
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

    async def _discover(self, session: ClientSession) -> None:
        """Ошибки:
        McpClientError — сервер отказал в server/discover.
        """
        try:
            await session.discover()
        except MCPError as exc:
            msg = (
                f"mcp {self._transport.label()}: server/discover was refused with "
                f"MCP error {exc.error.code}: {exc.error.message}; the client "
                f"expects server/discover with one of "
                f"{list(MODERN_PROTOCOL_VERSIONS)} and does not speak the "
                "initialize handshake of earlier releases, update the server to "
                "a release of MCP that answers server/discover."
            )
            raise McpClientError(msg) from exc

    async def _heard(self, message: IncomingMessage) -> None:
        """Обработчик сообщений сессии: уведомление прогресса — обработчику
        запроса с его токеном; прочее и прогресс без слушателя не нужны."""
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
        """Все файлы каталогов области scope_id: сервер удаляет их одной
        операцией, область без файлов — успех."""
        url = "/".join((self._root.url(), quote(scope_id, safe="")))
        reply = await self._http.delete(url)
        self._check("DELETE", url, reply)

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


class McpToolPort(Protocol):
    """Порт сервера инструментов, которым ретранслятор чата отвечает на
    server/discover, tools/list и tools/call агента.

    Реализации: McpToolServer — сервер MCP профиля по его транспорту;
    ChatToolPort чата — собственные инструменты чата в его процессе.
    """

    @abstractmethod
    def discovered(self) -> mt.DiscoverResult:
        """Ответ сервера на server/discover: версии, возможности, расширения,
        инструкции и serverInfo в _meta."""

    @abstractmethod
    def tools(self) -> Sequence[mt.Tool]:
        """Инструменты сервера как есть, из последнего прочитанного списка."""

    @abstractmethod
    async def refreshed(self) -> Sequence[mt.Tool]:
        """Список инструментов, перечитанный у сервера.

        Ошибки:
        McpClientError — сервер недоступен либо не отдал список.
        """

    @abstractmethod
    def features(self) -> Mapping[str, Mapping[str, object]]:
        """Расширения com.boba/*, объявленные сервером: идентификатор →
        настройки; у стороннего сервера их нет."""

    @abstractmethod
    def nested_calls(self) -> NestedCallsFeature | None:
        """Инструмент сервера со вложенными вызовами и форма его узлов;
        None — сервер такого не объявил."""

    @abstractmethod
    async def call(
        self,
        params: mt.CallToolRequestParams,
        progress: ProgressHeard | None,
        journals: JournalHeard | None,
    ) -> mt.CallToolResult | mt.InputRequiredResult:
        """tools/call с готовыми аргументами; ответ сервера — как есть.

        params — запрос на имя инструмента на сервере, с ответами на прежний
        input_required, если они есть; тред вызова — среди его аргументов.
        progress получает уведомления сервера о ходе вызова целиком, с
        _meta: вызов просит их своим токеном прогресса. journals узнаёт
        журналы, которые вызов открыл на сервере, пока он идёт; None — не
        нужны.

        Ошибки:
        McpClientError — сервер недоступен либо ответа нет: сессия оборвалась
            или вышел срок вызова, соединение закрывается, следующий вызов
            подключается заново; либо ответ не по схеме инструмента.
        MCPError — сервер ответил на запрос ошибкой MCP.
        """


class JournalOpenings:
    """Журналы, которые вызовы порта открыли на сервере, пока вызовы идут.

    Сервер с расширением журнала перечисляет журналы идущих вызовов
    вызывающего ресурсами resources/list с JournalOrigin в _meta и сообщает
    о смене списка потоком subscriptions/listen. Создаётся портом
    McpToolServer, когда сервер объявил журнал и смену списка ресурсов. Вызов
    порта ждёт журналов своего токена прогресса (expecting): первый такой
    вызов поднимает поток подписки в своей задаче и дожидается его
    подтверждения, на каждую смену списка список перечитывается, и новые
    журналы ждущих токенов уходят их слушателям. Сбой потока — в лог:
    журналы тогда узнаются только из итога вызова, следующий вызов
    поднимает поток заново.
    """

    def __init__(
        self,
        label: str,
        session: Callable[[], Awaitable[ClientSession]],
        links: JournalLinks,
    ) -> None:
        self._label = label
        self._session = session
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
            session = await self._session()
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
    """Порт одного MCP-сервера: подключение, списки и запросы стандарта.

    Создаётся McpServers на пару «сервер, личность клиента» с общим входом
    либо на тройку с пользователем при личном. open() подключается,
    согласует протокол (server/discover) и читает список инструментов;
    tools() отдаёт его как есть, refreshed() перечитывает. Операции, которые
    клиент исполняет сам (загрузка файла), из списка для модели убирает
    ретранслятор чата. call() шлёт tools/call, остальные методы — запросы
    ресурсов, промптов, подсказок и поток subscriptions/listen; ответ
    сервера отдаётся как есть. Расширения boba (features) берутся из
    возможностей сервера: у стороннего их нет. К личности клиента порт
    добавляет расширения, которые исполняет сам чат. Оборванная сессия
    открывается заново следующим запросом.
    """

    FEATURE_PREFIX: ClassVar[str] = "com.boba/"
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
    OWN_META: ClassVar[frozenset[str]] = frozenset(
        {"progress_token", mt.LOG_LEVEL_META_KEY}
    )
    """Ключи _meta запроса, которые порт ставит сам либо не несёт: токен
    прогресса (имя поля модели) и уровень логов."""
    READ_REPLY: ClassVar[
        TypeAdapter[mt.ReadResourceResult | mt.InputRequiredResult]
    ] = TypeAdapter(mt.ReadResourceResult | mt.InputRequiredResult)
    """Ответ на resources/read: содержимое либо просьба о вводе."""
    PROMPT_REPLY: ClassVar[TypeAdapter[mt.GetPromptResult | mt.InputRequiredResult]] = (
        TypeAdapter(mt.GetPromptResult | mt.InputRequiredResult)
    )
    """Ответ на prompts/get: сообщения промпта либо просьба о вводе."""

    def __init__(
        self, name: str, config: McpServerConfig, identity: McpClientIdentity
    ) -> None:
        self._name = name
        self._config = config
        extensions = dict(self.OWN_EXTENSIONS)
        if config.apps is not None:
            extensions[EXTENSION_ID] = config.apps.settings()

        self._connection = McpConnection(
            config.endpoint,
            config.connect_timeout_sec,
            identity.with_extensions(extensions),
        )
        self._label = f"mcp server {name!r} ({config.endpoint.label()})"
        self._causes = ErrorCauses()
        self._tools: list[mt.Tool] | None = None
        self._discovered: mt.DiscoverResult | None = None
        self._features: dict[str, Mapping[str, object]] = {}
        self._journal: JournalFeature | None = None
        """Расширение журнала вызовов, объявленное сервером; None — его нет."""
        self._links: JournalLinks | None = None
        """Адреса каналов журнала по шаблону сервера; None — журнала нет."""
        self._files_feature: FilesFeature | None = None
        """Расширение файлов workspace, объявленное сервером; None — его нет."""
        self._nested: NestedCallsFeature | None = None
        """Расширение вложенных вызовов, объявленное сервером; None — его нет."""
        self._files: McpFiles | None = None
        self._journal_files: McpFiles | None = None
        self._openings: JournalOpenings | None = None

    async def open(self) -> None:
        """Ошибки:
        McpClientError — сервер недоступен либо не отдал список инструментов.
        """
        session = await self._session()
        self._discovered = session.discover_result
        self._features = self._declared(session)
        self._journal = self._feature(JournalFeature)
        self._links = self._journal_links(self._journal)
        self._files_feature = self._feature(FilesFeature)
        self._nested = self._feature(NestedCallsFeature)
        await self.refreshed()
        await self._open_files()
        await self._open_journal_files()
        await self._open_openings(session)
        logger.info(
            "mcp server %s: %d tools, features %s",
            self._name,
            len(self.tools()),
            sorted(self._features),
        )

    def _feature(self, model: type[FeatureModel]) -> FeatureModel | None:
        """Настройки расширения model.ID моделью; None — сервер его не
        объявил либо объявил с настройками не по модели: такой сервер
        работает как сервер без расширения, причина — в логе."""
        settings = self._features.get(model.ID)
        if settings is None:
            return None

        try:
            return model.model_validate(settings)
        except ValidationError as exc:
            logger.warning(
                "mcp server %s (%s): extension %s expects the settings of %s, "
                "got %r: %s; the extension is not declared for this server",
                self._name,
                self._config.endpoint.label(),
                model.ID,
                model.__name__,
                dict(settings),
                exc,
            )
            return None

    def _journal_links(self, declared: JournalFeature | None) -> JournalLinks | None:
        """Адреса каналов журнала по шаблону, объявленному сервером; шаблон
        не по RFC 6570 — сервер работает как сервер без журнала, причина в
        логе."""
        if declared is None:
            return None

        try:
            return JournalLinks(declared.resource)
        except InvalidUriTemplate as exc:
            logger.warning(
                "%s: extension %s declares resource %r that is not an RFC 6570 "
                "template: %s; the journal of the server is not read",
                self._label,
                JournalFeature.ID,
                declared.resource,
                exc,
            )
            return None

    def discovered(self) -> mt.DiscoverResult:
        discovered = self._discovered
        if discovered is None:
            msg = (
                f"{self._label}: discovered() is called before open(), the "
                "server is not discovered yet"
            )
            raise McpClientError(msg)

        return discovered

    def files(self) -> McpFiles | None:
        """Файлы workspace на сервере; None — сервер их не объявил."""
        return self._files

    def journal_files(self) -> McpFiles | None:
        """Журналы вызовов сервера целыми файлами: канал журнала лежит по
        пути `{run}/{node}/{channel}`; None — сервер такого не объявил."""
        return self._journal_files

    def nested_calls(self) -> NestedCallsFeature | None:
        return self._nested

    def apps(self) -> McpServerApps | None:
        """Секция apps записи сервера; None — сервер показывает итоги
        блоками, приложения его не рисуются."""
        return self._config.apps

    def journal_links(self) -> JournalLinks | None:
        """Адреса каналов журнала сервера; None — сервер журнала не объявил."""
        return self._links

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

        self._journal_files = McpFiles(self._name, endpoint, declared.path)

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
        self._files = McpFiles(self._name, endpoint, declared.path)

    async def _open_openings(self, session: ClientSession) -> None:
        """Слежение за журналами идущих вызовов: сервер объявил журнал и
        смену списка ресурсов; прежнее закрывается."""
        if self._openings is not None:
            await self._openings.close()
            self._openings = None

        links = self._links
        if links is None:
            return

        capabilities = session.server_capabilities
        if capabilities is None:
            return

        resources = capabilities.resources
        if resources is None:
            return

        if not resources.list_changed:
            return

        self._openings = JournalOpenings(self._label, self._session, links)

    async def close(self) -> None:
        if self._openings is not None:
            await self._openings.close()
            self._openings = None

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

    def tools(self) -> Sequence[mt.Tool]:
        if self._tools is None:
            msg = (
                f"{self._label}: tools() is called before open(), the tool list "
                "is not read yet"
            )
            raise McpClientError(msg)

        return self._tools

    async def refreshed(self) -> Sequence[mt.Tool]:
        session = await self._session()
        listed: list[mt.Tool] = []
        cursor: str | None = None
        try:
            while True:
                params = mt.PaginatedRequestParams(cursor=cursor)
                page = await session.list_tools(params=params)
                listed.extend(page.tools)
                cursor = page.next_cursor
                if not cursor:
                    break
        except MCPError as exc:
            await self._unanswered(exc, "tools/list")
            msg = (
                f"{self._label}: tools/list was refused with MCP error "
                f"{exc.error.code}: {exc.error.message}"
            )
            raise McpClientError(msg) from exc
        except ValidationError as exc:
            msg = f"{self._label}: tools/list answered off the schema: {exc}"
            raise McpClientError(msg) from exc

        self._tools = listed

        return listed

    def features(self) -> Mapping[str, Mapping[str, object]]:
        return self._features

    async def call(
        self,
        params: mt.CallToolRequestParams,
        progress: ProgressHeard | None,
        journals: JournalHeard | None,
    ) -> mt.CallToolResult | mt.InputRequiredResult:
        session = await self._session()
        token = uuid4().hex
        meta = self._sent_meta(params.meta, token)
        opened: AbstractAsyncContextManager[None] = nullcontext()
        if journals is not None and self._openings is not None:
            opened = self._openings.expecting(token, journals)

        try:
            async with self._heard(token, progress), opened:
                return await session.call_tool(
                    params.name,
                    params.arguments,
                    read_timeout_seconds=self._config.call_timeout_sec,
                    input_responses=params.input_responses,
                    request_state=params.request_state,
                    meta=meta,
                    allow_input_required=True,
                )
        except MCPError as exc:
            await self._unanswered(exc, f"tools/call of {params.name!r}")
            raise
        except RuntimeError as exc:
            msg = (
                f"{self._label}: tools/call of {params.name!r} was answered off "
                f"the tool: the result does not match its output schema or is an "
                f"extension result the client did not ask for: {exc}"
            )
            raise McpClientError(msg) from exc

    async def list_resources(
        self, params: mt.PaginatedRequestParams | None
    ) -> mt.ListResourcesResult:
        """resources/list одной страницей курсора params; ответ как есть.

        Ошибки:
        McpClientError — сервер недоступен или ответил не по схеме.
        MCPError — сервер ответил ошибкой MCP.
        """
        request = mt.ListResourcesRequest(params=params)

        return await self._request(request, mt.ListResourcesResult, None)

    async def list_resource_templates(
        self, params: mt.PaginatedRequestParams | None
    ) -> mt.ListResourceTemplatesResult:
        """resources/templates/list одной страницей; ответ как есть.

        Ошибки: те же, что у list_resources.
        """
        request = mt.ListResourceTemplatesRequest(params=params)

        return await self._request(request, mt.ListResourceTemplatesResult, None)

    async def read_resource(
        self, params: mt.ReadResourceRequestParams, progress: ProgressHeard | None
    ) -> mt.ReadResourceResult | mt.InputRequiredResult:
        """resources/read: содержимое ресурса либо просьба о вводе
        (input_required), с ответами на прежнюю, если они есть в params.

        Ошибки: те же, что у list_resources.
        """
        request = mt.ReadResourceRequest(params=params)

        return await self._request(request, self.READ_REPLY, progress)

    async def list_prompts(
        self, params: mt.PaginatedRequestParams | None
    ) -> mt.ListPromptsResult:
        """prompts/list одной страницей; ответ как есть.

        Ошибки: те же, что у list_resources.
        """
        request = mt.ListPromptsRequest(params=params)

        return await self._request(request, mt.ListPromptsResult, None)

    async def get_prompt(
        self, params: mt.GetPromptRequestParams, progress: ProgressHeard | None
    ) -> mt.GetPromptResult | mt.InputRequiredResult:
        """prompts/get: сообщения промпта либо просьба о вводе.

        Ошибки: те же, что у list_resources.
        """
        request = mt.GetPromptRequest(params=params)

        return await self._request(request, self.PROMPT_REPLY, progress)

    async def complete(self, params: mt.CompleteRequestParams) -> mt.CompleteResult:
        """completion/complete: варианты значения аргумента промпта или
        шаблона ресурса.

        Ошибки: те же, что у list_resources.
        """
        request = mt.CompleteRequest(params=params)

        return await self._request(request, mt.CompleteResult, None)

    async def _request(
        self,
        request: mt.ClientRequest,
        result: type[ResultT] | TypeAdapter[ResultT],
        progress: ProgressHeard | None,
    ) -> ResultT:
        """Запрос request серверу со сроком вызова; ход запроса — progress
        под своим токеном прогресса клиента вместо токена запроса."""
        session = await self._session()
        token = uuid4().hex
        sent = request
        params = request.params
        if params is not None:
            meta = self._sent_meta(params.meta, token)
            sent = request.model_copy(
                update={"params": params.model_copy(update={"meta": meta})}
            )

        try:
            async with self._heard(token, progress):
                return await session.send_request(
                    sent,
                    result,
                    request_read_timeout_seconds=self._config.call_timeout_sec,
                )
        except MCPError as exc:
            await self._unanswered(exc, request.method)
            raise
        except ValidationError as exc:
            msg = f"{self._label}: {request.method} answered off the schema: {exc}"
            raise McpClientError(msg) from exc

    def _sent_meta(
        self, meta: mt.RequestParamsMeta | None, token: str
    ) -> mt.RequestParamsMeta:
        """_meta запроса серверу: ключи запроса как есть, кроме токена
        прогресса — у запроса свой токен клиента token, его слушает
        соединение — и уровня логов: логов сервера клиент не несёт. Ключи
        личности клиента ставит сессия."""
        sent = mt.RequestParamsMeta(progress_token=token)
        if meta is None:
            return sent

        for key, value in meta.items():
            if key in self.OWN_META:
                continue

            sent[key] = value

        return sent

    def _heard(
        self, token: mt.ProgressToken, progress: ProgressHeard | None
    ) -> AbstractAsyncContextManager[None]:
        if progress is None:
            return nullcontext()

        return self._connection.listening(token, progress)

    async def _session(self) -> ClientSession:
        """Открытая сессия сервера; неудача подключения называет сервер."""
        try:
            return await self._connection.open()
        except McpClientError as exc:
            msg = f"mcp server {self._name!r}: {exc}"
            raise McpClientError(msg) from exc

    async def _unanswered(self, error: MCPError, action: str) -> None:
        """Запрос action остался без ответа — оборвалась сессия или вышел
        срок: соединение закрывается, следующий запрос подключится заново, а
        наверх уходит McpClientError. Иную ошибку MCP вызывающий поднимает
        как есть."""
        closed = error.error.code == mt.CONNECTION_CLOSED
        expired = error.error.code == mt.REQUEST_TIMEOUT
        unanswered = closed or expired
        if not unanswered:
            return

        await self._connection.close()
        msg = f"{self._label}: {action} got no answer: {self._causes.text(error)}"
        raise McpClientError(msg) from error

    @asynccontextmanager
    async def listen(
        self, notifications: mt.SubscriptionFilter
    ) -> AsyncGenerator[Subscription, None]:
        """Поток subscriptions/listen сессии этого порта на события
        notifications; поток живёт, пока открыт контекст, подтверждённый
        сервером набор — subscription.honored.

        Ошибки:
        McpClientError — сервер недоступен, подписку не поддерживает или
            поток оборвался без штатного закрытия.
        MCPError — сервер отказал в подписке ошибкой MCP.
        """
        uris: list[str] = []
        if notifications.resource_subscriptions is not None:
            uris.extend(notifications.resource_subscriptions)

        label = f"{self._label}: subscriptions/listen"
        session = await self._session()
        try:
            async with listen(
                session,
                tools_list_changed=bool(notifications.tools_list_changed),
                prompts_list_changed=bool(notifications.prompts_list_changed),
                resources_list_changed=bool(notifications.resources_list_changed),
                resource_subscriptions=uris,
            ) as subscription:
                yield subscription
        except ListenNotSupportedError as exc:
            msg = f"{label}: the negotiated protocol has no listen streams: {exc}"
            raise McpClientError(msg) from exc
        except MCPError as exc:
            await self._unanswered(exc, "subscriptions/listen")
            raise
        except SubscriptionLost as exc:
            msg = f"{label}: the stream ended without the server's close: {exc}"
            raise McpClientError(msg) from exc
        except TimeoutError as exc:
            msg = f"{label}: the server did not acknowledge the subscription: {exc}"
            raise McpClientError(msg) from exc

    async def journal(self, request: JournalRead) -> StreamSlice | None:
        """Окно журнала вызова ресурсом resources/read; None — сервер журнал
        не объявлял либо такого журнала у него нет.

        Ошибки:
        McpClientError — сервер недоступен или ответил не окном журнала.
        """
        links = self._links
        if links is None:
            return None

        uri = links.uri(request)
        what = (
            f"{self._label}: reading the journal of call {request.node!r} in run "
            f"{request.run!r} as resource {uri}"
        )
        params = mt.ReadResourceRequestParams(uri=uri)
        try:
            read = await self.read_resource(params, None)
        except MCPError as exc:
            if exc.error.code in self.NOT_FOUND:
                return None

            msg = f"{what} failed: MCP error {exc.error.code}: {exc.error.message}"
            raise McpClientError(msg) from exc

        if not isinstance(read, mt.ReadResourceResult):
            msg = f"{what}: the server asked for more input, expected a window"
            raise McpClientError(msg)

        return self._window(what, read)

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
    """MCP-серверы клиента: порты по имени сервера, пользователю и личности
    клиента.

    Создаётся сборкой приложения из секции [mcp]. Сервер с общей
    авторизацией подключается один раз на процесс и личность клиента,
    сервер с личной (proxy) — от имени каждого пользователя; оба при первом
    обращении к ним. port() отдаёт открытый порт сервера от личности самого
    чата (OWN), relayed() — от личности агента, чьи запросы ретранслирует
    чат. Сервер, который не ответил, отказывает ошибкой McpClientError тому,
    кто его позвал, и следующее обращение подключает его заново.
    """

    KEEP: ClassVar[int] = 256
    """Сколько личных подключений держать; давно не нужные закрываются."""

    OWN: ClassVar[McpClientIdentity] = McpClientIdentity(
        info=mt.Implementation(name="boba-aid", version="1"),
        capabilities=mt.ClientCapabilities(),
    )
    """Личность чата для его собственных запросов: панель, приложения, лента."""

    def __init__(self, config: McpServersConfig) -> None:
        self._configs = dict(config.servers)
        self._names = McpToolNames()
        self._shared: dict[tuple[str, str], McpToolServer] = {}
        self._personal: OrderedDict[tuple[str, McpCaller, str], McpToolServer]
        self._personal = OrderedDict()

    async def stop(self) -> None:
        for server in self._shared.values():
            await server.close()

        for server in self._personal.values():
            await server.close()

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
        не назван в [mcp.servers] или файлов не объявил."""
        if server not in self._configs:
            return None

        port = await self.port(server, caller)

        return port.files()

    async def journal_files(self, server: str, caller: McpCaller) -> McpFiles | None:
        """Журналы вызовов сервера server целыми файлами от имени caller;
        None — сервер не назван или такого не объявил."""
        if server not in self._configs:
            return None

        port = await self.port(server, caller)

        return port.journal_files()

    async def port(self, name: str, caller: McpCaller) -> McpToolServer:
        """Открытый порт сервера name для пользователя caller от личности
        чата: общий либо его личный.

        Ошибки:
        McpClientError — имя не названо в [mcp.servers] либо сервер
            недоступен или не прошёл подключение.
        """
        return await self.relayed(name, caller, self.OWN)

    async def relayed(
        self, name: str, caller: McpCaller, identity: McpClientIdentity
    ) -> McpToolServer:
        """Открытый порт сервера name для пользователя caller от личности
        identity: так чат ретранслирует запросы агента с его clientInfo и
        clientCapabilities.

        Ошибки:
        McpClientError — имя не названо в [mcp.servers] либо сервер
            недоступен или не прошёл подключение.
        """
        config = self._configs.get(name)
        if config is None:
            msg = (
                f"mcp servers of {caller.login!r}: server {name!r} is not "
                f"declared in [mcp.servers], declared: {sorted(self._configs)}"
            )
            raise McpClientError(msg)

        server = await self._held(name, config, caller, identity)
        if not server.opened:
            await server.open()

        return server

    async def _held(
        self,
        name: str,
        config: McpServerConfig,
        caller: McpCaller,
        identity: McpClientIdentity,
    ) -> McpToolServer:
        """Порт сервера из подключений клиента; нового — заводит."""
        client = identity.key()
        if not config.endpoint.personal():
            shared = (name, client)
            server = self._shared.get(shared)
            if server is None:
                server = McpToolServer(name, config, identity)
                self._shared[shared] = server

            return server

        key = (name, caller, client)
        server = self._personal.get(key)
        if server is None:
            server = McpToolServer(name, config.on_behalf_of(caller), identity)
            self._personal[key] = server

        self._personal.move_to_end(key)
        while len(self._personal) > self.KEEP:
            _stale, evicted = self._personal.popitem(last=False)
            await evicted.close()

        return server
