"""Авторизация и HTTP/WebSocket ACP над общим PostgreSQL-агентом."""

from __future__ import annotations

import asyncio
import json
import logging
import logging.config
import sys
import time
from collections.abc import AsyncGenerator, Iterable, Iterator, Sequence
from contextlib import aclosing, asynccontextmanager, suppress
from contextvars import ContextVar
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, ClassVar
from uuid import UUID

import uvicorn
from acp.experimental.negotiation import AgentProtocolConnection, AgentProtocolRouter
from acp.experimental.v2 import schema
from acp.http.protocol import CONNECTION_ID_HEADER, CONTENT_TYPE_SSE, SESSION_ID_HEADER
from acp.ws.server import handle_websocket
from fastmcp.server.auth import AccessToken
from fastmcp.server.auth.middleware import RequireAuthMiddleware
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.auth.routes import build_resource_metadata_url
from omegaconf import DictConfig
from pydantic import (
    AnyHttpUrl,
    ValidationError,
)
from starlette.applications import Starlette
from starlette.requests import HTTPConnection, Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import BaseRoute, Route, WebSocketRoute
from starlette.types import Receive, Scope, Send
from starlette.websockets import WebSocket

from boba.agent import Installed
from boba.agent.acp_connection import AcpAgent
from boba.agent.acp_server import AgentProfiles, SessionHub
from boba.agent.acp_validator import AcpVersion
from boba.agent.history import HistoryStore
from boba.agent.llm import OpenAiTransport, Providers
from boba.agent.settings import (
    AgentProfile,
    AgentServerConfig,
    DataConfig,
    ServeConfig,
    ServeHistory,
    ServeSection,
    SettingsError,
    TlsConfig,
)
from boba.auth import SessionService, SignIns
from boba.auth.assembly import AuthServices
from boba.auth.config import ProxyAuthConfig
from boba.cancellation import ProcessStop
from boba.config import ConfigError, bind
from boba.config.application import ConfigFiles
from boba.crypto import SecretCipher
from boba.db.postgres import AsyncPostgresPool
from boba.http_server import (
    HealthCheckError,
    HealthProbe,
    SignalledServer,
)
from boba.identity.api import RoleCatalog, StoredUser, UserRows
from boba.identity.principal import UserIdentity
from boba.identity.signin import ProfileCatalog
from boba.identity_postgres import UsersTable
from boba.krb.layout import InstanceLayout
from boba.ldap import Ldap3Directory
from boba.oauth.claims import TokenClaims
from boba.oauth.server.clients import RegisteredClients
from boba.oauth.server.provider import AuthServer
from boba.oauth.server.resources import ServiceAuth
from boba.oauth.server.sealed import SealedValues
from boba.oauth.server.tokens import ServiceTokens
from boba.transport.acp.http import HttpServer

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
    и печатей, имя ресурса ACP, проба готовности."""

    NAME = "boba-agent"
    TOKEN_SALT = "boba-agent-access-token"  # noqa: S105 — соль вывода ключа, не секрет
    SEALED_SALT = "boba-agent-sealed-values"
    RESOURCE = "acp"
    HEALTH = "/health"
    STORE_THREAD = "history-store"


@dataclass(frozen=True)
class Caller:
    """Вошедший пользователь подключения: логин из токена и профили агента,
    выданные его ролям (области токена)."""

    user_id: UUID
    login: str
    profiles: frozenset[str]


class ServeAgents(UserRows, RoleCatalog):
    """Один пул, один SessionHub и независимые ACP-подключения."""

    def __init__(
        self,
        section: ServeSection,
        history: ServeHistory,
        profiles: AgentProfiles,
        version: str,
        auth_methods: Sequence[schema.AgentAuthMethod],
    ) -> None:
        self._version = version
        self._postgres = history.postgres
        self._pool = AsyncPostgresPool(history.postgres)
        self._users = UsersTable(self._pool, history.db_schema)
        llm = OpenAiTransport()
        store = HistoryStore(self._pool)
        cipher = SecretCipher(section.providers.cipher.get_secret_value())
        providers = Providers(store, cipher)

        self.hub = SessionHub(
            store, llm, profiles, section.settings, version, providers=providers
        )

    async def start(self) -> None:
        await self._pool.open()
        await self.hub.start(self._postgres)

    async def resolve_roles(
        self, identity: UserIdentity, candidates: Iterable[str]
    ) -> frozenset[str]:
        return await self._users.resolve_roles(identity, candidates)

    async def stored(self, identity: UserIdentity) -> StoredUser | None:
        return await self._users.stored(identity)

    async def stored_by_id(self, user_id: UUID) -> StoredUser | None:
        return await self._users.stored_by_id(user_id)

    async def upsert(
        self, identity: UserIdentity, roles: frozenset[str] | None = None
    ) -> StoredUser:
        return await self._users.upsert(identity, roles)

    async def stop(self) -> None:
        await self.hub.close()
        await self._pool.close()

    def opened_sessions(self) -> int:
        return len(self.hub.agents)

    def agent(
        self, caller: Caller, maximum_protocol_version: AcpVersion = AcpVersion.V2
    ) -> AcpAgent:
        return AcpAgent(
            self.hub,
            str(caller.user_id),
            caller.profiles,
            self._version,
            maximum_protocol_version=maximum_protocol_version,
        )

    @property
    def info(self) -> schema.Implementation:
        return schema.Implementation(name=ServeMark.NAME.value, version=self._version)


class ServeConnection(AgentProtocolConnection):
    """Закрытие клиента снимает подписки, исполнение общей сессии продолжается."""

    def __init__(self, connection: AgentProtocolConnection, agent: AcpAgent) -> None:
        self._delegate = connection
        self._agent = agent
        self._closed = False

    def busy(self) -> bool:
        return self._agent.busy()

    async def _listen(self) -> None:
        await self._delegate.listen()

    async def close(self) -> None:
        if not self._closed:
            self._closed = True
            try:
                await self._agent.close_all()
            finally:
                await self._delegate.close()


@dataclass
class Admission:
    """Вход подключения на время запроса, который его открывает: кто вошёл
    и какое подключение роутер для него создал."""

    caller: Caller
    connection: ServeConnection | None = None


@dataclass
class HttpActivity:
    """Движение подключения HTTP: когда кончился последний запрос и сколько
    запросов (POST в работе, открытые потоки GET) идут сейчас. По нему
    AcpEndpoint закрывает подключение, брошенное клиентом."""

    connection: ServeConnection
    last: float
    running: int


class ServeRouter(AgentProtocolRouter):
    """Роутер протокола SDK, который на каждое подключение отдаёт вход ACP
    агента (AcpAgent) с политикой вошедшего.

    Создаётся сборкой службы; его получают AcpServer (streamable HTTP) и
    handle_websocket (WebSocket) SDK. Вошедшего обработчик запроса кладёт в
    admitted() на время вызова SDK, который открывает подключение.
    """

    def __init__(
        self, agents: ServeAgents, maximum_protocol_version: AcpVersion = AcpVersion.V2
    ) -> None:
        self._agents = agents
        self._maximum_protocol_version = maximum_protocol_version
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
        agent = self._agents.agent(admission.caller, self._maximum_protocol_version)
        wire = agent.connect(input_stream, output_stream, **connection_kwargs)
        connection = ServeConnection(wire, agent)
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
        except ValidationError:
            logger.warning("request token has invalid claims")
            return None

        return Caller(
            user_id=claims.user_id, login=claims.login, profiles=frozenset(token.scopes)
        )


class AcpEndpoint:
    """Адрес ACP службы: streamable HTTP (POST, GET, DELETE) и WebSocket.

    Создаётся сборкой службы над AcpServer SDK и роутером службы. Запрос
    открытия подключения (initialize по HTTP, рукопожатие WebSocket) идёт в
    SDK под входом вошедшего; остальные запросы HTTP принимаются только от
    того же пользователя, что открыл подключение. Подключение HTTP без
    запросов и ходов дольше idle_sec закрывает watch().
    """

    TICK_SEC: ClassVar[float] = 1.0

    def __init__(
        self, router: ServeRouter, callers: CallerTokens, idle_sec: float
    ) -> None:
        self._router = router
        self._server = HttpServer(router)
        self._callers = callers
        self._idle_sec = idle_sec
        self._owners: dict[str, UUID] = {}
        """Кто открыл каждое подключение HTTP: запросы чужого подключения
        отвергаются как неизвестные."""
        self._opened: dict[str, HttpActivity] = {}

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

        if self._owners.get(connection_id) != caller.user_id:
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

        self._owners[opened] = caller.user_id
        self._opened[opened] = HttpActivity(
            connection=connection, last=time.monotonic(), running=0
        )

        return response

    async def _deleted(self, connection_id: str) -> Response:
        """Клиент закрыл подключение: его сессии закрываются."""
        self._owners.pop(connection_id, None)
        activity = self._opened.pop(connection_id, None)
        response = await self._server.handle_delete(connection_id=connection_id)
        if activity is not None:
            self._router.forget(activity.connection)
            await activity.connection.close()

        return response

    async def _post(self, request: Request, connection_id: str) -> Response:
        message = await self._message(request)
        if message is None:
            return JSONResponse({"error": "the body is not JSON"}, 400)

        async with self._working(connection_id):
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

        frames = self._server.open_stream(
            connection_id=connection_id, session_id=session_id
        )

        return StreamingResponse(
            self._streamed(connection_id, frames),
            media_type=CONTENT_TYPE_SSE,
            headers={"Cache-Control": "no-cache"},
        )

    async def _streamed(
        self, connection_id: str, frames: AsyncGenerator[bytes, None]
    ) -> AsyncGenerator[bytes, None]:
        """Поток GET подключения: пока он открыт, подключение не брошено."""
        async with self._working(connection_id), aclosing(frames):
            async for frame in frames:
                yield frame

    @asynccontextmanager
    async def _working(self, connection_id: str) -> AsyncGenerator[None, None]:
        activity = self._opened.get(connection_id)
        if activity is None:
            yield
            return

        activity.running += 1
        try:
            yield
        finally:
            activity.running -= 1
            activity.last = time.monotonic()

    async def watch(self) -> None:
        """Закрывает подключения HTTP, брошенные клиентом: без запросов и
        ходов дольше idle_sec. Идёт задачей приложения до его конца."""
        while True:
            await asyncio.sleep(self.TICK_SEC)
            for connection_id in list(self._abandoned()):
                logger.info(
                    "connection %s of %r is idle for %.0fs: closing it",
                    connection_id,
                    self._owners.get(connection_id),
                    self._idle_sec,
                )
                await self._deleted(connection_id)

    def _abandoned(self) -> Iterator[str]:
        now = time.monotonic()
        for connection_id, activity in self._opened.items():
            if activity.running > 0:
                continue

            if activity.connection.busy():
                continue

            if now - activity.last < self._idle_sec:
                continue

            yield connection_id

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

        self._profiles = AgentProfiles(config.agent.profiles)
        self._agents = ServeAgents(
            config.agent,
            config.history,
            self._profiles,
            version,
            self._auth_methods(),
        )
        self._sign_ins = self._signed(
            config,
            self._profiles,
            self._agents,
        )

    def endpoint(self) -> str:
        return f"{self._config.server.public_url.rstrip('/')}{self._config.agent.path}"

    def app(self) -> Starlette:
        """Приложение службы: маршруты входа, адрес ACP и проба готовности под
        префиксом службы, метаданные ресурса ACP — в корне."""
        section = self._config.agent
        auth = self._auth()
        provider = auth.resource(ServeMark.RESOURCE, [])
        metadata = build_resource_metadata_url(AnyHttpUrl(self.endpoint()))
        router = ServeRouter(self._agents)
        endpoint = AcpEndpoint(router, CallerTokens(), section.idle_sec)
        v1_router = ServeRouter(self._agents, AcpVersion.V1)
        v1_endpoint = AcpEndpoint(v1_router, CallerTokens(), section.idle_sec)
        guarded = RequireAuthMiddleware(endpoint, [], metadata)
        v1_guarded = RequireAuthMiddleware(v1_endpoint, [], metadata)
        served: list[BaseRoute] = [
            Route(ServeMark.HEALTH, self._health, methods=["GET"]),
            Route(section.path, guarded, methods=["POST", "GET", "DELETE"]),
            WebSocketRoute(section.path, endpoint.websocket),
            Route(section.path + "/v1", v1_guarded, methods=["POST", "GET", "DELETE"]),
            WebSocketRoute(section.path + "/v1", v1_endpoint.websocket),
        ]
        routes = auth.app_routes(provider.get_well_known_routes(section.path), served)

        @asynccontextmanager
        async def lifespan(app: Starlette) -> AsyncGenerator[None, None]:
            watch = asyncio.create_task(endpoint.watch())
            v1_watch = asyncio.create_task(v1_endpoint.watch())
            try:
                yield
            finally:
                watch.cancel()
                v1_watch.cancel()
                with suppress(asyncio.CancelledError):
                    await watch

                with suppress(asyncio.CancelledError):
                    await v1_watch

                await endpoint.close()
                await v1_endpoint.close()

        return Starlette(
            routes=routes, middleware=provider.get_middleware(), lifespan=lifespan
        )

    async def start(self) -> None:
        await self._agents.start()

    def sessions(self) -> int:
        """Сколько сессий служба держит открытыми сейчас."""
        return self._agents.opened_sessions()

    async def stop(self) -> None:
        await self._agents.stop()

    def _auth_methods(self) -> list[schema.AgentAuthMethod]:
        """Отдельного ACP-входа нет: Bearer проверяется до initialize."""
        return []

    async def _health(self, request: Request) -> Response:
        """Проба готовности: процесс слушает; число открытых сессий."""
        return JSONResponse({"sessions": self._agents.opened_sessions()})

    @staticmethod
    def _signed(
        config: ServeConfig, profiles: ProfileCatalog, roles: RoleCatalog
    ) -> SignIns:
        """Способы входа процесса из [auth]: пароли, SPNEGO и proxy с их
        провайдерами ролей и профилей."""
        services = AuthServices(
            Ldap3Directory(),
            profiles,
            roles,
            config.session.auth_secret,
        )
        return config.AUTH_BACKENDS.build(config.auth, services)

    def _auth(self) -> ServiceAuth:
        """Вход службы: сервер авторизации над способами входа [auth] и
        проверяющий токена адреса ACP."""
        session = self._config.session
        section = self._config.agent
        public_url = self._config.server.public_url.rstrip("/")
        tokens = ServiceTokens(
            public_url,
            session.auth_secret,
            session.lifetime(),
            session.session_generation(),
            ServeMark.TOKEN_SALT,
        )
        sealed = SealedValues(session.auth_secret, ServeMark.SEALED_SALT)
        server = AuthServer(
            public_url,
            self._sign_ins,
            tokens,
            self._config.auth_entries(ProxyAuthConfig),
            RegisteredClients(sealed, section.clients),
            sealed,
            [self.endpoint()],
            sessions=SessionService(self._agents, self._profiles),
        )

        return ServiceAuth(
            public_url,
            server,
            tokens,
            ServeMark.NAME,
            {ServeMark.RESOURCE: self.endpoint()},
        )

    async def serve(self) -> None:
        """Слушает адрес [agent] до остановки процесса."""
        server = self._config.server
        await self.start()
        try:
            listener = SignalledServer(
                uvicorn.Config(
                    self.app(),
                    host=server.host,
                    port=server.port,
                    log_config=None,
                    timeout_graceful_shutdown=server.shutdown_timeout_sec,
                ),
                ProcessStop(),
            )
            logger.info(
                "boba serve listens on %s:%d, acp at %s",
                server.host,
                server.port,
                self.endpoint(),
            )
            await listener.serve_until_stopped()
        finally:
            await self.stop()


class ServeEntry:
    """Вход команд `boba serve` (конфиг из файлов, журнал, служба) и
    `boba health` (проба готовности запущенной службы)."""

    def __init__(self, paths: Sequence[Path]) -> None:
        self._paths = tuple(paths)
        self._configs = ConfigFiles(ServeConfig, values=("auth",))

    async def run(self) -> None:
        """Ошибки:
        SystemExit — конфиг не прочитан или служба не поднялась.
        """
        try:
            config = self._configs.load(self._paths).value
            layout = InstanceLayout(
                self._paths[0].resolve().parent,
                Path(config.paths.data_dir),
                Path(config.tls.ca_file),
            )
            layout.apply()
        except (ConfigError, ValueError, OSError) as exc:
            msg = f"boba serve: the config {self._paths[0]} is not usable: {exc}"
            raise SystemExit(msg) from exc

        logging.config.dictConfig(config.logger)
        installed = Installed()
        version = installed.version()
        try:
            host = ServeHost(config, version)
        except SettingsError as exc:
            raise SystemExit(f"boba serve: {exc}") from exc

        await host.serve()

    def health(self) -> None:
        """Проба готовности по [agent]: порт и префикс службы.

        Ошибки:
        SystemExit — конфиг не прочитан или служба не ответила успехом.
        """
        try:
            section = bind(self._configs.read(self._paths), "server", AgentServerConfig)
            probe = HealthProbe(section.port, section.url_prefix, ServeMark.HEALTH)
            checked = probe.check()
        except (ConfigError, HealthCheckError) as exc:
            raise SystemExit(f"boba health: {exc}") from exc

        sys.stdout.write(f"{checked}\n")


def prepare_migrations(raw: DictConfig, conf_dir: Path) -> None:
    """Подготовить окружение команды migrate из моделей приложения."""
    paths = bind(raw, "app.data", DataConfig)
    tls = bind(raw, "app.tls", TlsConfig)
    InstanceLayout(conf_dir, Path(paths.data_dir), Path(tls.ca_file)).apply()
