"""Агент как служба: команда `boba serve --config <toml> --site <toml>`.

Процесс агента сам слушает сеть: ACP по streamable HTTP и WebSocket
(AcpServer и handle_websocket SDK) на адресе секции [agent], вход — сервер
авторизации boba-oauth над способами входа [auth] (proxy для harness, пароли,
SPNEGO). Адрес ACP принимает только токен службы, выпущенный для него: без
токена — 401 со ссылкой на метаданные ресурса (RFC 9728), токен без единого
профиля агента — 403; WebSocket без токена закрывается на рукопожатии.

Каждое подключение получает свой вход ACP (AcpAgent) с политикой вошедшего:
сессии заводятся на его логин и видны только ему (хранилище истории с
владельцем), профили моделей — только выданные его ролям. Все подключения
делят сборку процесса: каталог профилей, поток хранилища истории с одним пулом
[agent.history].postgres.pool и одним соединением замков, потоки сессий с
пределом sessions.maxOpen. Закрытие подключения закрывает его сессии.

Ошибки:
SystemExit — конфиг не прочитан либо служба не поднялась; текст называет
    причину.
"""

from __future__ import annotations

import json
import logging
import logging.config
import os
from collections.abc import AsyncGenerator, Mapping, Sequence
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

import uvicorn
from acp.connection import Connection
from acp.experimental.negotiation import AgentProtocolConnection, AgentProtocolRouter
from acp.experimental.v2 import schema
from acp.http.protocol import CONNECTION_ID_HEADER, CONTENT_TYPE_SSE, SESSION_ID_HEADER
from acp.http.server import AcpServer
from acp.ws.server import handle_websocket
from fastmcp.server.auth import AccessToken
from fastmcp.server.auth.middleware import RequireAuthMiddleware
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.auth.routes import build_resource_metadata_url
from mcp.shared.auth import OAuthClientInformationFull
from pydantic import (
    AnyHttpUrl,
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
)
from starlette.applications import Starlette
from starlette.requests import HTTPConnection, Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import BaseRoute, Route, WebSocketRoute
from starlette.types import Receive, Scope, Send
from starlette.websockets import WebSocket

from boba.access import RoleVisibility
from boba.agent.settings import (
    HistoryKind,
    HistoryPoolSettings,
    ModelSettings,
    PostgresHistorySettings,
    SettingsLayer,
)
from boba.auth import SignIns
from boba.auth.proxy import ProxyAssertions
from boba.auth.sso import SpnegoGate
from boba.cancellation import ProcessStop
from boba.cli.acp_server import AcpAgent
from boba.cli.app import (
    AgentAssembly,
    Environment,
    Installed,
    ModelCatalog,
    OpenedStore,
)
from boba.cli.settings import Arguments, EffectiveSettings, Settings, SettingsError
from boba.cli.threads import LoopThread, SessionThreads, ThreadedHistoryStore
from boba.db.postgres import AsyncPostgresPool
from boba.db.postgres.connection import LibpqConnection, PostgresConfig
from boba.history.postgres import PostgresHistoryStore, SessionLocks
from boba.identity.signin import ProfileCatalog
from boba.ldap import Ldap3Directory
from boba.llm.http.ollama import MANIFEST as OLLAMA
from boba.llm.http.openai import MANIFEST as OPENAI
from boba.llm.onnx.chat import MANIFEST as ONNX
from boba.llm.providers import LlmProviders, LlmProviderTypes
from boba.oauth.server import (
    AuthServer,
    RegisteredClients,
    SealedValues,
    ServiceAuth,
    ServiceTokens,
    TokenClaims,
    TrustedClient,
)
from boba.runtime.config import ConfigFiles, ProcessConfig
from boba.runtime.http import SignalledServer
from boba.runtime.signin import SignInAssembly
from boba.transport.acp.protocol import AgentSide

__all__ = [
    "AcpEndpoint",
    "AgentProfile",
    "AgentProfiles",
    "Caller",
    "ServeConfig",
    "ServeEntry",
    "ServeHistory",
    "ServeHost",
    "ServeMark",
    "ServeRouter",
    "ServeSection",
]

logger = logging.getLogger(__name__)


class ServeMark(StrEnum):
    """Как служба называет себя: имя на странице входа, соли ключей токенов
    и печатей, имя ресурса ACP и способа входа в initialize, проба
    готовности."""

    NAME = "boba-agent"
    TOKEN_SALT = "boba-agent-access-token"  # noqa: S105 — соль вывода ключа, не секрет
    SEALED_SALT = "boba-agent-sealed-values"
    RESOURCE = "acp"
    AUTH_METHOD = "boba-oauth"
    HEALTH = "/health"
    STORE_THREAD = "history-store"


@dataclass(frozen=True)
class Caller:
    """Вошедший пользователь подключения: логин из токена и профили агента,
    выданные его ролям (области токена)."""

    login: str
    profiles: frozenset[str]


class AgentProfile(RoleVisibility):
    """Профиль агента [agent.profiles.<имя>]: роли, которым он выдан, и
    профиль модели агента — модель, её пределы и свой системный промпт."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    model: ModelSettings


class ServeHistory(BaseModel):
    """Секция [agent.history]: схема истории и подключение истории
    ([postgres_history] целиком, со своей ролью и пулом)."""

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    db_schema: str = Field(alias="schema", min_length=1)
    postgres: PostgresConfig


class ServeSection(BaseModel):
    """Секция [agent]: адрес службы и путь ACP, публичный адрес (от него
    строятся метаданные OAuth и ресурс токена), история, слой настроек
    агента без истории и моделей, профили агента и доверенные клиенты
    OAuth."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    host: str = Field(min_length=1)
    port: int = Field(gt=0, lt=65536)
    path: str = Field(min_length=1, pattern=r"^/")
    public_url: str = Field(min_length=1)
    shutdown_timeout_sec: int = Field(gt=0)
    history: ServeHistory
    settings: SettingsLayer
    """Слой настроек агента: права, лимиты, сессии, серверы MCP, промпт.
    Историю, профили моделей и профиль по умолчанию задаёт служба."""
    profiles: dict[str, AgentProfile] = Field(min_length=1)
    clients: dict[str, TrustedClient]
    """Доверенные клиенты OAuth по client_id: им разрешён вход proxy."""

    @field_validator("settings")
    @classmethod
    def _service_layer(cls, value: SettingsLayer) -> SettingsLayer:
        if value.history is not None:
            msg = (
                "[agent.settings]: the history is [agent.history] of the service, "
                f"remove history of kind {value.history.kind!r}"
            )
            raise ValueError(msg)

        if value.models:
            msg = (
                "[agent.settings]: model profiles live in [agent.profiles.<name>], "
                f"remove models {sorted(value.models)}"
            )
            raise ValueError(msg)

        if value.model is not None:
            msg = (
                "[agent.settings]: the default model is the first profile granted "
                f"to the user, remove model = {value.model!r}"
            )
            raise ValueError(msg)

        return value


class ServeConfig(ProcessConfig):
    """Секции процесса службы: секции процесса и [agent]."""

    agent: ServeSection


class AgentProfiles(ProfileCatalog):
    """Профили агента [agent.profiles] как каталог профилей входа: вход
    кладёт в области токена профили, выданные ролям вошедшего, а
    подключение открывает сессии только на них. Создаётся сборкой службы."""

    def __init__(self, profiles: Mapping[str, AgentProfile]) -> None:
        self._profiles = dict(profiles)

    def granted_by_roles(self, user_roles: frozenset[str]) -> frozenset[str]:
        names: set[str] = set()
        for name, profile in self._profiles.items():
            if profile.visible_for(user_roles):
                names.add(name)

        return frozenset(names)

    def known(self, name: str) -> bool:
        return name in self._profiles


class ServeAgents:
    """Общая сборка процесса службы и вход ACP на подключение.

    Создаёт ServeHost: настройки агента из [agent] (все профили, история
    строкой libpq общего LibpqConnection), каталог моделей, поток хранилища
    истории с соединением замков, потоки сессий. agent(caller) собирает вход
    ACP вошедшего: хранилище с его владельцем над общими пулом и замками,
    профили — выданные его ролям, по умолчанию — первый из них в порядке
    [agent.profiles].
    """

    def __init__(
        self,
        section: ServeSection,
        version: str,
        auth_methods: Sequence[schema.AgentAuthMethod],
    ) -> None:
        env = dict(os.environ)
        self._auth_methods = list(auth_methods)
        cwd = Path.cwd()
        self._section = section
        self._version = version
        self._info = schema.Implementation(name=ServeMark.NAME.value, version=version)
        self._settings = Settings(Arguments(), env, Path.home(), cwd).effective_of(
            self._layer(section)
        )
        self._types = LlmProviderTypes.declare([OPENAI, OLLAMA, ONNX])
        self._providers = LlmProviders(self._types)
        self._catalog = ModelCatalog(self._settings, env, self._providers)
        self._environment = Environment(env)
        self._cwd = cwd
        self._locks = SessionLocks(section.history.postgres)
        self._store_thread = LoopThread(ServeMark.STORE_THREAD.value)
        self._threads = SessionThreads(self._settings.sessions.max_open)

    def start(self) -> None:
        self._store_thread.start()

    async def stop(self) -> None:
        await self._store_thread.run(self._locks.close())
        await self._store_thread.run(AsyncPostgresPool.close_all())
        await self._store_thread.stop()
        await self._providers.aclose()

    def opened_sessions(self) -> int:
        return self._threads.opened()

    def agent(self, caller: Caller) -> tuple[AcpAgent, OpenedStore]:
        """Вход ACP вошедшего и его хранилище: хранилище закрывается вместе
        с подключением.

        Ошибки:
        SettingsError — вошедшему не выдано ни одного профиля.
        """
        settings = self._granted(caller)
        history = self._section.history
        store = PostgresHistoryStore(
            history.postgres, history.db_schema, caller.login, self._locks
        )
        threaded = ThreadedHistoryStore(store, self._store_thread)
        assembly = AgentAssembly(
            settings,
            self._catalog,
            self._types,
            threaded,
            (),
            self._environment,
            self._cwd,
            self._version,
        )
        agent = AcpAgent(
            assembly, self._info, settings.mcp, self._threads, self._auth_methods
        )

        return agent, OpenedStore(store=threaded, locks=())

    @property
    def info(self) -> schema.Implementation:
        return self._info

    def _granted(self, caller: Caller) -> EffectiveSettings:
        """Ошибки:
        SettingsError — вошедшему не выдано ни одного профиля.
        """
        granted: dict[str, ModelSettings] = {}
        for name, profile in self._settings.models.items():
            if name in caller.profiles:
                granted[name] = profile

        if not granted:
            msg = (
                f"{caller.login!r} has no agent profile granted to their roles; "
                "ask the administrator for a role of [agent.profiles]"
            )
            raise SettingsError(msg)

        first = next(iter(granted))

        return self._settings.model_copy(update={"model": first, "models": granted})

    @staticmethod
    def _layer(section: ServeSection) -> SettingsLayer:
        """Слой настроек агента службы: слой [agent.settings], все профили
        [agent.profiles] и история [agent.history] строкой libpq.

        Ошибки:
        SettingsError — вход роли истории не выразим строкой libpq.
        """
        postgres = section.history.postgres
        try:
            dsn = LibpqConnection(postgres).dsn()
        except ValueError as exc:
            msg = f"[agent.history].postgres cannot be used by the agent: {exc}"
            raise SettingsError(msg) from exc

        pool = postgres.pool
        max_size = pool.min_size
        if pool.max_size is not None:
            max_size = pool.max_size

        history = PostgresHistorySettings(
            kind=HistoryKind.POSTGRES,
            dsn=dsn,
            schema=section.history.db_schema,
            pool=HistoryPoolSettings(
                min_size=pool.min_size, max_size=max_size, timeout_sec=pool.timeout
            ),
        )
        models: dict[str, Mapping[str, Any]] = {}
        for name, profile in section.profiles.items():
            models[name] = profile.model.model_dump(by_alias=True, mode="python")

        first = next(iter(section.profiles))

        return section.settings.model_copy(
            update={"history": history, "models": models, "model": first}
        )


class ServeConnection(AgentProtocolConnection):
    """Подключение ACP вошедшего: соединение SDK его входа (AgentSide) и
    сам вход; закрытие подключения закрывает его сессии и хранилище.
    Создаёт ServeRouter."""

    def __init__(
        self, connection: Connection, agent: AcpAgent, store: OpenedStore
    ) -> None:
        super().__init__(connection)
        self._agent = agent
        self._store = store
        self._closed = False

    async def close(self) -> None:
        if self._closed:
            return

        self._closed = True
        try:
            await self._agent.close_all()
            await self._store.store.close()
        finally:
            await super().close()


@dataclass
class Admission:
    """Вход подключения на время запроса, который его открывает: кто вошёл
    и какое подключение роутер для него создал."""

    caller: Caller
    connection: ServeConnection | None = None


class ServeRouter(AgentProtocolRouter):
    """Роутер протокола SDK, который на каждое подключение отдаёт вход ACP
    агента (AcpAgent) с политикой вошедшего.

    Создаётся сборкой службы; его получают AcpServer (streamable HTTP) и
    handle_websocket (WebSocket) SDK. Вошедшего обработчик запроса кладёт в
    admitted() на время вызова SDK, который открывает подключение.
    """

    def __init__(self, agents: ServeAgents) -> None:
        self._agents = agents
        self._admission: ContextVar[Admission] = ContextVar("serve_admission")
        self._connections: set[ServeConnection] = set()

    @asynccontextmanager
    async def admitted(self, admission: Admission) -> AsyncGenerator[None, None]:
        token = self._admission.set(admission)
        try:
            yield
        finally:
            self._admission.reset(token)

    def connect(
        self,
        input_stream: Any,
        output_stream: Any = None,
        **connection_kwargs: Any,
    ) -> AgentProtocolConnection:
        admission = self._admission.get()
        listening = bool(connection_kwargs.get("listening", True))
        agent, store = self._agents.agent(admission.caller)
        side = AgentSide(agent.attach, self._agents.info, input_stream, listening)
        connection = ServeConnection(side.connection, agent, store)
        admission.connection = connection
        self._connections.add(connection)
        logger.info("connection of %r opened", admission.caller.login)

        return connection

    def forget(self, connection: ServeConnection) -> None:
        self._connections.discard(connection)

    async def close_all(self) -> None:
        for connection in list(self._connections):
            await connection.close()


class CallerTokens:
    """Вошедший по токену запроса, который проверил вход приложения
    (BearerAuthBackend): логин и профили агента — области токена."""

    def of(self, connection: HTTPConnection) -> Caller | None:
        """None — токена нет или он не принят."""
        user = connection.scope.get("user")
        if not isinstance(user, AuthenticatedUser):
            return None

        token = user.access_token
        if not isinstance(token, AccessToken):
            return None

        try:
            claims = TokenClaims.model_validate(token.claims)
        except ValidationError as exc:
            logger.warning(
                "token of client %r carries no login and roles: %s",
                token.client_id,
                exc,
            )
            return None

        return Caller(login=claims.login, profiles=frozenset(token.scopes))


class AcpEndpoint:
    """Адрес ACP службы: streamable HTTP (POST, GET, DELETE) и WebSocket.

    Создаётся сборкой службы над AcpServer SDK и роутером службы. Запрос
    открытия подключения (initialize по HTTP, рукопожатие WebSocket) идёт в
    SDK под входом вошедшего; остальные запросы HTTP принимаются только от
    того же пользователя, что открыл подключение.
    """

    def __init__(self, router: ServeRouter, callers: CallerTokens) -> None:
        self._router = router
        self._server = AcpServer(router)
        self._callers = callers
        self._owners: dict[str, str] = {}
        """Кто открыл каждое подключение HTTP: запросы чужого подключения
        отвергаются как неизвестные."""
        self._opened: dict[str, ServeConnection] = {}

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        request = Request(scope, receive)
        response = await self._respond(request)
        await response(scope, receive, send)

    async def _respond(self, request: Request) -> Response:
        caller = self._callers.of(request)
        if caller is None:
            return JSONResponse({"error": "the request carries no token"}, 401)

        if not caller.profiles:
            message = (
                f"{caller.login!r} has no agent profile granted to their roles: "
                "ask the administrator for a role of [agent.profiles]"
            )
            return JSONResponse({"error": message}, 403)

        connection_id = request.headers.get(CONNECTION_ID_HEADER)
        if request.method == "POST" and connection_id is None:
            return await self._open(request, caller)

        if connection_id is None:
            return JSONResponse({"error": "missing connection id"}, 400)

        if self._owners.get(connection_id) != caller.login:
            return JSONResponse({"error": "unknown connection id"}, 404)

        return await self._connected(request, connection_id)

    async def _connected(self, request: Request, connection_id: str) -> Response:
        """Запрос открытого подключения вошедшего."""
        match request.method:
            case "POST":
                return await self._post(request, connection_id)
            case "GET":
                return self._stream(request, connection_id)
            case "DELETE":
                return await self._deleted(connection_id)

        return JSONResponse({"error": f"method {request.method} is not allowed"}, 405)

    async def _open(self, request: Request, caller: Caller) -> Response:
        """Первый POST подключения — initialize — под входом вошедшего."""
        message = await self._message(request)
        if message is None:
            return JSONResponse({"error": "the body is not JSON"}, 400)

        admission = Admission(caller=caller)
        async with self._router.admitted(admission):
            response = await self._server.handle_post(
                message,
                content_type=request.headers.get("content-type"),
                connection_id=None,
                session_id=request.headers.get(SESSION_ID_HEADER),
            )

        opened = response.headers.get(CONNECTION_ID_HEADER)
        connection = admission.connection
        if opened is None:
            return response

        if connection is None:
            return response

        self._owners[opened] = caller.login
        self._opened[opened] = connection

        return response

    async def _deleted(self, connection_id: str) -> Response:
        """Клиент закрыл подключение: его сессии закрываются."""
        self._owners.pop(connection_id, None)
        connection = self._opened.pop(connection_id, None)
        response = await self._server.handle_delete(connection_id=connection_id)
        if connection is not None:
            self._router.forget(connection)
            await connection.close()

        return response

    async def _post(self, request: Request, connection_id: str) -> Response:
        message = await self._message(request)
        if message is None:
            return JSONResponse({"error": "the body is not JSON"}, 400)

        return await self._server.handle_post(
            message,
            content_type=request.headers.get("content-type"),
            connection_id=connection_id,
            session_id=request.headers.get(SESSION_ID_HEADER),
        )

    def _stream(self, request: Request, connection_id: str) -> Response:
        accept = request.headers.get("accept", "")
        if CONTENT_TYPE_SSE not in accept:
            message = f"Accept must include {CONTENT_TYPE_SSE}"
            return JSONResponse({"error": message}, 406)

        session_id = request.headers.get(SESSION_ID_HEADER)
        refused = self._server.validate_stream(
            connection_id=connection_id, session_id=session_id
        )
        if refused is not None:
            return refused

        return StreamingResponse(
            self._server.open_stream(
                connection_id=connection_id, session_id=session_id
            ),
            media_type=CONTENT_TYPE_SSE,
            headers={"Cache-Control": "no-cache"},
        )

    @staticmethod
    async def _message(request: Request) -> object | None:
        try:
            return await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None

    async def websocket(self, websocket: WebSocket) -> None:
        """Подключение по WebSocket: токен проверен на рукопожатии, без него
        или без профилей сокет закрывается кодом 1008 до принятия; конец
        сокета закрывает сессии подключения."""
        caller = self._callers.of(websocket)
        if caller is None:
            await websocket.close(code=1008, reason="the handshake carries no token")
            return

        if not caller.profiles:
            await websocket.close(code=1008, reason="no agent profile is granted")
            return

        admission = Admission(caller=caller)
        try:
            async with self._router.admitted(admission):
                await handle_websocket(self._router, websocket)
        finally:
            connection = admission.connection
            if connection is not None:
                self._router.forget(connection)
                await connection.close()

    async def close(self) -> None:
        await self._router.close_all()
        await self._server.close()


class ServeHost:
    """Сборка процесса службы: вход, общая сборка агента и приложение ASGI.

    Создаётся входом процесса из конфига; serve() слушает адрес [agent] до
    остановки и закрывает подключения с их сессиями на выходе.
    """

    def __init__(self, config: ServeConfig, version: str) -> None:
        self._config = config
        section = config.agent
        self._profiles = AgentProfiles(section.profiles)
        self._agents = ServeAgents(section, version, self._auth_methods())
        self._sign_ins = self._signed(config, self._profiles)

    def resource(self) -> str:
        section = self._config.agent
        return f"{section.public_url.rstrip('/')}{section.path}"

    def app(self) -> Starlette:
        """Приложение службы: маршруты входа, метаданные ресурса ACP, адрес
        ACP и проба готовности."""
        section = self._config.agent
        auth = self._auth()
        provider = auth.resource(ServeMark.RESOURCE, [])
        metadata = build_resource_metadata_url(AnyHttpUrl(self.resource()))
        router = ServeRouter(self._agents)
        endpoint = AcpEndpoint(router, CallerTokens())
        guarded = RequireAuthMiddleware(endpoint, [], metadata)
        routes: list[BaseRoute] = [
            Route(ServeMark.HEALTH, self._health, methods=["GET"]),
            *auth.routes(),
            *provider.get_well_known_routes(section.path),
            Route(section.path, guarded, methods=["POST", "GET", "DELETE"]),
            WebSocketRoute(section.path, endpoint.websocket),
        ]

        @asynccontextmanager
        async def lifespan(app: Starlette) -> AsyncGenerator[None, None]:
            try:
                yield
            finally:
                await endpoint.close()

        return Starlette(
            routes=routes, middleware=provider.get_middleware(), lifespan=lifespan
        )

    def start(self) -> None:
        self._agents.start()

    def sessions(self) -> int:
        """Сколько сессий служба держит открытыми сейчас."""
        return self._agents.opened_sessions()

    async def stop(self) -> None:
        await self._agents.stop()

    def _auth_methods(self) -> list[schema.AgentAuthMethod]:
        """Способ входа в ответе initialize: токен службы, который адрес ACP
        проверил до подключения; auth/login с ним принимается сразу."""
        metadata = build_resource_metadata_url(AnyHttpUrl(self.resource()))
        method = schema.AgentAuthMethod(
            method_id=ServeMark.AUTH_METHOD,
            name="Boba sign-in",
            description=(
                f"OAuth 2.1 bearer token of {self._config.agent.public_url}, "
                "checked on every HTTP request and on the WebSocket handshake; "
                f"resource metadata: {metadata}"
            ),
        )

        return [method]

    async def _health(self, request: Request) -> Response:
        """Проба готовности: процесс слушает; число открытых сессий."""
        return JSONResponse({"sessions": self._agents.opened_sessions()})

    @staticmethod
    def _signed(config: ServeConfig, profiles: ProfileCatalog) -> SignIns:
        """Способы входа процесса из [auth]: пароли, SPNEGO и proxy с их
        провайдерами ролей и профилей."""
        assembly = SignInAssembly(Ldap3Directory(), profiles)
        sso = None
        if kerberos := config.kerberos():
            sso = SpnegoGate(assembly.sso(kerberos, config.session.auth_secret))

        proxy = None
        if proxy_config := config.proxy():
            proxy = assembly.proxy(proxy_config)

        return SignIns(assembly.password(config.auth), sso, proxy)

    def _auth(self) -> ServiceAuth:
        """Вход службы: сервер авторизации над способами входа [auth] и
        проверяющий токена адреса ACP."""
        session = self._config.session
        section = self._config.agent
        public_url = section.public_url.rstrip("/")
        tokens = ServiceTokens(
            public_url,
            session.auth_secret,
            session.session_ttl_sec,
            session.session_max_sec,
            session.session_generation(),
            ServeMark.TOKEN_SALT,
        )
        assertions = None
        if proxy := self._config.proxy():
            assertions = ProxyAssertions(
                proxy.secret.get_secret_value(), proxy.max_skew_sec
            )

        sealed = SealedValues(session.auth_secret, ServeMark.SEALED_SALT)
        declared: dict[str, OAuthClientInformationFull] = {}
        for client_id, client in section.clients.items():
            declared[client_id] = client.registered(client_id)

        server = AuthServer(
            public_url,
            self._sign_ins,
            tokens,
            assertions,
            RegisteredClients(sealed, declared),
            sealed,
            [self.resource()],
        )

        return ServiceAuth(
            public_url,
            server,
            tokens,
            ServeMark.NAME,
            {ServeMark.RESOURCE: self.resource()},
        )

    async def serve(self) -> None:
        """Слушает адрес [agent] до остановки процесса."""
        section = self._config.agent
        self.start()
        try:
            listener = SignalledServer(
                uvicorn.Config(
                    self.app(),
                    host=section.host,
                    port=section.port,
                    log_config=None,
                    timeout_graceful_shutdown=section.shutdown_timeout_sec,
                ),
                ProcessStop(),
            )
            logger.info(
                "boba serve listens on %s:%d, acp at %s",
                section.host,
                section.port,
                self.resource(),
            )
            await listener.serve_until_stopped()
        finally:
            await self.stop()


class ServeEntry:
    """Вход команды `boba serve`: конфиг из файлов, журнал, служба."""

    def __init__(self, config: Path, site: Path) -> None:
        self._files = ConfigFiles(config=config, site=site)

    async def run(self) -> None:
        """Ошибки:
        SystemExit — конфиг не прочитан или служба не поднялась.
        """
        try:
            config = ServeConfig.load(self._files)
        except (ValidationError, ValueError, OSError) as exc:
            msg = f"boba serve: the config {self._files.config} is not usable: {exc}"
            raise SystemExit(msg) from exc

        logging.config.dictConfig(config.logger)
        version = Installed().version()
        try:
            host = ServeHost(config, version)
        except SettingsError as exc:
            raise SystemExit(f"boba serve: {exc}") from exc

        await host.serve()
