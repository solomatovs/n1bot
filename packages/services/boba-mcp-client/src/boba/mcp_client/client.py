"""Клиент MCP-серверов: подключения, списки инструментов и вызовы.

Клиент работает с любым сервером стандарта MCP: берёт список инструментов
(tools/list) и исполняет вызовы (tools/call), отдавая ответ сервера как
есть. Транспорт — streamable HTTP, SSE или stdio. Способ входа — свойство
записи сервера (McpAuth): без входа, готовый токен, вход local учёткой с
паролем и вход proxy утверждением о пользователе. Токен сервера для local и
proxy получают и обновляют штатные провайдеры OAuth библиотеки mcp; своё
здесь — только проход страницы входа без человека (UnattendedLogin).

Ошибки:
McpClientError — сервер недоступен, не прошёл инициализацию либо не
    отвечает на server/discover, не отдал список инструментов, отказал во
    входе, не ответил на вызов, ответил не по схеме инструмента либо порт
    позвали до open().
MCPError (mcp.shared.exceptions) — сервер ответил на tools/call или
    resources/read ошибкой MCP.
"""

from __future__ import annotations

import asyncio
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
from contextlib import AbstractAsyncContextManager, asynccontextmanager, nullcontext
from dataclasses import dataclass
from typing import Annotated, Any, ClassVar, Literal, Protocol, Self, TypeVar
from urllib.parse import quote, urlsplit, urlunsplit
from uuid import uuid4

import httpx2
import mcp.types as mt
from mcp import Client, ClientSession, StdioServerParameters
from mcp.client.auth import OAuthClientProvider, TokenStorage
from mcp.client.auth.extensions.identity_assertion import (
    IdentityAssertionOAuthProvider,
)
from mcp.client.extension import ClientExtension, advertise
from mcp.client.session import IncomingMessage
from mcp.client.sse import sse_client
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client
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
from mcp.types.version import MODERN_PROTOCOL_VERSIONS
from pydantic import (
    AnyUrl,
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    SecretStr,
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
from boba.toolkit.wire import FilesFeature, JournalFeature, JournalRead
from boba.workspace.launcher import ReadWindow

__all__ = [
    "BearerAuth",
    "HttpEndpoint",
    "HttpLocation",
    "LocalAuth",
    "McpAuth",
    "McpAuthEntry",
    "McpCaller",
    "McpClientError",
    "McpServerApps",
    "McpServerConfig",
    "McpServers",
    "McpServersConfig",
    "McpToolPort",
    "McpToolServer",
    "NoAuth",
    "ProxyAuth",
    "SseEndpoint",
    "StdioCommand",
]

logger = logging.getLogger(__name__)

FeatureModel = TypeVar("FeatureModel", bound=ServerFeature)
"""Модель настроек расширения сервера."""

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
        если он не порт схемы по умолчанию."""
        parts = urlsplit(str(self.sandbox_url))
        return urlunsplit((parts.scheme, parts.netloc, "", "", ""))

    def extension(self) -> ClientExtension:
        """Объявление расширения MCP Apps серверу: клиент рисует ресурсы
        приложений."""
        return advertise(EXTENSION_ID, {"mimeTypes": [APP_MIME_TYPE]})


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


class McpConnection:
    """Живая сессия MCP с одним сервером.

    Создаётся портом McpToolServer. Транспорт и сессия библиотеки mcp
    открываются и закрываются в одной собственной задаче — их области отмены
    привязаны к задаче, которая их открыла; запросы идут из задач вызовов.
    Уведомления прогресса сессия отдаёт целиком, с _meta: каждое уходит
    обработчику вызова, который слушает его токен (listening).
    """

    CLIENT: ClassVar[mt.Implementation] = mt.Implementation(
        name="boba-chat", version="1"
    )

    EXTENSIONS: ClassVar[Sequence[ClientExtension]] = (
        advertise(JournalFeature.ID),
        advertise(SealFeature.ID),
    )
    """Расширения, которые клиент объявляет любому серверу: журнал вызовов
    он читает окнами и слушает сигналы его роста; соединения пользователя
    запечатывает ключом сервера, и сервер по этому предлагает их."""

    def __init__(
        self,
        transport: Transport,
        connect_timeout_sec: float,
        extensions: Sequence[ClientExtension],
    ) -> None:
        self._transport = transport
        self._connect_timeout_sec = connect_timeout_sec
        self._extensions = extensions
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
        """Сессия живёт в этой задаче. Протокол согласует библиотека: сервер
        новой спецификации отвечает на server/discover, прежний — на
        initialize, и такой сервер клиент отвергает."""
        stop = self._stop
        try:
            client = Client(
                self._transport.opened(),
                client_info=self.CLIENT,
                extensions=self._extensions,
                cache=None,
                message_handler=self._heard,
            )
            async with client:
                if client.session.discover_result is None:
                    msg = (
                        f"mcp {self._transport.label()}: the server negotiated "
                        f"protocol version {client.protocol_version} through "
                        "initialize, but the client expects server/discover with "
                        f"one of {list(MODERN_PROTOCOL_VERSIONS)}; update the "
                        "server to a release of MCP that answers server/discover."
                    )
                    ready.set_exception(McpClientError(msg))
                    return

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

    async def _heard(self, message: IncomingMessage) -> None:
        """Обработчик сообщений сессии: уведомление прогресса — обработчику
        вызова с его токеном; прочее и прогресс без слушателя не нужны."""
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


class McpToolPort(Protocol):
    """Порт сервера инструментов, которым ретранслятор чата отвечает на
    tools/list и tools/call агента.

    Реализации: McpToolServer — сервер MCP профиля по его транспорту;
    ChatToolPort чата — собственные инструменты чата в его процессе.
    """

    @abstractmethod
    def tools(self) -> Sequence[mt.Tool]:
        """Инструменты сервера как есть."""

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
        progress: Callable[[mt.ProgressNotificationParams], Awaitable[None]] | None,
    ) -> mt.CallToolResult | mt.InputRequiredResult:
        """tools/call с готовыми аргументами; ответ сервера — как есть.

        params — запрос на имя инструмента на сервере, с ответами на прежний
        input_required, если они есть; тред вызова — среди его аргументов.
        progress получает уведомления сервера о ходе вызова
        целиком, с _meta: вызов просит их своим токеном прогресса.

        Ошибки:
        McpClientError — сервер недоступен либо ответа нет: сессия оборвалась
            или вышел срок вызова, соединение закрывается, следующий вызов
            подключается заново; либо ответ не по схеме инструмента.
        MCPError — сервер ответил на запрос ошибкой MCP.
        """


class McpToolServer(McpToolPort):
    """Порт одного MCP-сервера: подключение, список инструментов и вызовы.

    Создаётся McpServers на сервер с общим входом либо на пару «сервер,
    пользователь» с личным. open() подключается, согласует протокол и один
    раз читает список инструментов; после него tools() отдаёт инструменты
    сервера как есть, кроме операций, которые исполняет сам клиент (загрузка
    файла, чтение журнала), а call() шлёт tools/call и отдаёт ответ сервера
    как есть. Расширения boba (features) берутся из возможностей сервера: у
    стороннего их нет. Оборванная сессия открывается заново следующим
    вызовом. От чьего имени идёт подключение, решает способ входа записи.
    """

    FEATURE_PREFIX: ClassVar[str] = "com.boba/"

    def __init__(self, name: str, config: McpServerConfig) -> None:
        self._name = name
        self._config = config
        extensions = list(McpConnection.EXTENSIONS)
        if config.apps is not None:
            extensions.append(config.apps.extension())

        self._connection = McpConnection(
            config.endpoint, config.connect_timeout_sec, extensions
        )
        self._causes = ErrorCauses()
        self._tools: list[mt.Tool] | None = None
        self._features: dict[str, Mapping[str, object]] = {}
        self._journal: JournalFeature | None = None
        """Расширение журнала вызовов, объявленное сервером; None — его нет."""
        self._files_feature: FilesFeature | None = None
        """Расширение файлов workspace, объявленное сервером; None — его нет."""
        self._nested: NestedCallsFeature | None = None
        """Расширение вложенных вызовов, объявленное сервером; None — его нет."""
        self._files: McpFiles | None = None
        self._journal_files: McpFiles | None = None

    async def open(self) -> None:
        """Ошибки:
        McpClientError — сервер недоступен либо не отдал список инструментов.
        """
        session = await self._session()
        self._features = self._declared(session)
        self._journal = self._feature(JournalFeature)
        self._files_feature = self._feature(FilesFeature)
        self._nested = self._feature(NestedCallsFeature)

        try:
            listed = await self._list(session)
        except Exception as exc:
            msg = (
                f"mcp server {self._name!r} ({self._config.endpoint.label()}): "
                f"tools/list failed: {type(exc).__name__}: {exc}"
            )
            raise McpClientError(msg) from exc

        self._tools = list(listed)
        await self._open_files()
        await self._open_journal_files()
        logger.info(
            "mcp server %s: %d tools, features %s",
            self._name,
            len(self._tools),
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

    def journal_feature(self) -> JournalFeature | None:
        """Настройки расширения журнала вызовов; None — сервер журнала не
        объявил."""
        return self._journal

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

    def tools(self) -> Sequence[mt.Tool]:
        if self._tools is None:
            msg = (
                f"mcp server {self._name!r}: tools() is called before open(), "
                "the tool list is not read yet"
            )
            raise McpClientError(msg)

        return self._tools

    def features(self) -> Mapping[str, Mapping[str, object]]:
        return self._features

    async def call(
        self,
        params: mt.CallToolRequestParams,
        progress: Callable[[mt.ProgressNotificationParams], Awaitable[None]] | None,
    ) -> mt.CallToolResult | mt.InputRequiredResult:
        session = await self._session()
        label = f"mcp server {self._name!r} ({self._config.endpoint.label()})"
        listened: AbstractAsyncContextManager[None] = nullcontext()
        meta = mt.RequestParamsMeta()
        if progress is not None:
            token = uuid4().hex
            listened = self._connection.listening(token, progress)
            meta = mt.RequestParamsMeta(progress_token=token)

        try:
            async with listened:
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
                f"{label}: tools/call of {params.name!r} was answered off the "
                f"tool: the result does not match its output schema or is an "
                f"extension result the client did not ask for: {exc}"
            )
            raise McpClientError(msg) from exc

    async def read(self, uri: str) -> mt.ReadResourceResult:
        """resources/read ресурса uri; ответ сервера как есть.

        Ошибки:
        McpClientError — сервер недоступен, не ответил либо вместо ресурса
            попросил ввода.
        MCPError — сервер ответил ошибкой MCP.
        """
        session = await self._session()
        try:
            return await session.read_resource(uri)
        except MCPError as exc:
            await self._unanswered(exc, f"resources/read of {uri!r}")
            raise
        except RuntimeError as exc:
            msg = (
                f"mcp server {self._name!r} ({self._config.endpoint.label()}): "
                f"resources/read of {uri!r} asked for more input instead of the "
                f"resource; reading a resource for an app takes no input: {exc}"
            )
            raise McpClientError(msg) from exc

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
        msg = (
            f"mcp server {self._name!r} ({self._config.endpoint.label()}): "
            f"{action} got no answer: {self._causes.text(error)}"
        )
        raise McpClientError(msg) from error

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
        params = mt.CallToolRequestParams(name=tool, arguments=arguments)
        try:
            result = await self.call(params, None)
        except MCPError as exc:
            msg = (
                f"mcp server {self._name!r}: reading the journal of call "
                f"{request.node!r} in run {request.run!r} with {tool!r} failed: "
                f"MCP error {exc.error.code}: {exc.error.message}"
            )
            raise McpClientError(msg) from exc

        if not isinstance(result, mt.CallToolResult):
            msg = (
                f"mcp server {self._name!r}: {tool!r} asked for more input on call "
                f"{request.node!r} of run {request.run!r}, expected a journal window"
            )
            raise McpClientError(msg)

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
    """MCP-серверы клиента: порты по имени сервера и пользователю.

    Создаётся сборкой приложения из секции [mcp]. Сервер с общей
    авторизацией подключается один раз на процесс, сервер с личной (proxy)
    — от имени каждого пользователя; оба при первом обращении к ним. port()
    отдаёт открытый порт сервера; сервер, который не ответил, отказывает
    ошибкой McpClientError тому, кто его позвал, и следующее обращение
    подключает его заново.
    """

    KEEP: ClassVar[int] = 256
    """Сколько личных подключений держать; давно не нужные закрываются."""

    def __init__(self, config: McpServersConfig) -> None:
        self._configs = dict(config.servers)
        self._names = McpToolNames()
        self._shared: dict[str, McpToolServer] = {}
        self._personal: OrderedDict[tuple[str, McpCaller], McpToolServer]
        self._personal = OrderedDict()
        for name, server in config.servers.items():
            if server.endpoint.personal():
                continue

            self._shared[name] = McpToolServer(name, server)

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

    def journaled(self) -> frozenset[str]:
        """Полные имена инструментов подключённых серверов, которые ведут
        журнал вызовов: у их шагов есть живой вывод."""
        held: list[tuple[str, McpToolServer]] = list(self._shared.items())
        for (name, _caller), server in self._personal.items():
            held.append((name, server))

        names: set[str] = set()
        for name, server in held:
            if not server.opened:
                continue

            if JournalFeature.ID not in server.features():
                continue

            for tool in server.tools():
                names.add(self._names.full(name, tool.name))

        return frozenset(names)

    async def port(self, name: str, caller: McpCaller) -> McpToolServer:
        """Открытый порт сервера name для пользователя caller: общий либо его
        личный.

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

        key = (name, caller)
        server = self._shared.get(name)
        if server is None:
            server = self._personal.get(key)

        if server is None:
            server = McpToolServer(name, config.on_behalf_of(caller))
            self._personal[key] = server

        if key in self._personal:
            self._personal.move_to_end(key)

        while len(self._personal) > self.KEEP:
            _stale, evicted = self._personal.popitem(last=False)
            await evicted.close()

        if not server.opened:
            await server.open()

        return server
