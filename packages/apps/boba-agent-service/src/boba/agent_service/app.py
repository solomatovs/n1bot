"""Процесс службы агента: вход OAuth, ACP по streamable HTTP и WebSocket,
процессы агента на сессии.

`boba-agent-service --config <toml> --site <toml>` читает конфиг, собирает
сервер авторизации (boba-oauth) над способами входа [auth] и слушает адрес
секции [agent]. Адрес ACP (`[agent].path`) принимает только токен службы,
выпущенный для него: без токена — 401 со ссылкой на метаданные ресурса
(RFC 9728), токен без единого профиля агента — 403; WebSocket без токена
закрывается на рукопожатии. Подключение с принятым токеном получает
ClientLink (boba.agent_service.relay) от имени вошедшего; HTTP-запрос с
идентификатором чужого подключения получает 404.

Ошибки:
SystemExit — конфиг не прочитан либо служба не поднялась; текст называет
    причину.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import logging.config
from collections.abc import AsyncGenerator, Mapping
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from enum import StrEnum
from functools import partial
from typing import Any, ClassVar

import uvicorn
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

from boba.agent.settings import PostgresHistorySettings, SettingsLayer
from boba.agent_service.relay import (
    AgentLauncher,
    AgentProcessSection,
    AgentProfile,
    AgentSettings,
    Caller,
    ClientLink,
    ProcessQuota,
)
from boba.auth.proxy import ProxyAssertions
from boba.cancellation import ProcessStop
from boba.identity.signin import ProfileCatalog
from boba.oauth.server import (
    AuthServer,
    RegisteredClients,
    SealedValues,
    ServiceAuth,
    ServiceTokens,
    TokenClaims,
    TrustedClient,
)
from boba.runtime import providers
from boba.runtime.config import ConfigArguments, ConfigFiles, ProcessConfig
from boba.runtime.di import Container
from boba.runtime.http import SignalledServer

__all__ = [
    "AcpEndpoint",
    "AgentProfiles",
    "AgentSection",
    "AgentServiceConfig",
    "AgentServiceEntry",
    "AgentServiceHost",
    "ServiceMark",
    "ServiceRouter",
]

logger = logging.getLogger(__name__)


class ServiceMark(StrEnum):
    """Как служба называет себя: имя на странице входа, соли ключей токенов
    и печатей, имя ресурса ACP и способа входа в initialize."""

    NAME = "boba-agent"
    TOKEN_SALT = "boba-agent-access-token"  # noqa: S105 — соль вывода ключа, не секрет
    SEALED_SALT = "boba-agent-sealed-values"
    RESOURCE = "acp"
    AUTH_METHOD = "boba-oauth"
    HEALTH = "/health"


class AgentSection(BaseModel):
    """Секция [agent]: адрес службы и путь ACP, публичный адрес (от него
    строятся метаданные OAuth и ресурс токена), запуск процессов агента,
    базовый слой файла настроек, профили агента и доверенные клиенты OAuth."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    host: str = Field(min_length=1)
    port: int = Field(gt=0, lt=65536)
    path: str = Field(min_length=1, pattern=r"^/")
    public_url: str = Field(min_length=1)
    shutdown_timeout_sec: int = Field(gt=0)
    process: AgentProcessSection
    settings: SettingsLayer
    """Базовый слой settings.json каждого процесса: история (только
    postgres), системный промпт, права, лимиты, окружение. Профили моделей
    и владельца истории служба дописывает сама."""
    profiles: dict[str, AgentProfile] = Field(min_length=1)
    clients: dict[str, TrustedClient]
    """Доверенные клиенты OAuth по client_id: им разрешён вход proxy."""

    @field_validator("settings")
    @classmethod
    def _base_layer(cls, value: SettingsLayer) -> SettingsLayer:
        history = value.history
        if not isinstance(history, PostgresHistorySettings):
            msg = (
                "[agent.settings]: expects history of kind 'postgres', the store "
                "that keeps sessions per user; got "
                f"{type(history).__name__}"
            )
            raise ValueError(msg)

        if history.owner is not None:
            msg = (
                "[agent.settings.history]: owner is set by the service to the "
                f"login of each connection, remove owner = {history.owner!r}"
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


class AgentServiceConfig(ProcessConfig):
    """Секции процесса службы агента: секции процесса и [agent]."""

    agent: AgentSection


class AgentProfiles(ProfileCatalog):
    """Профили агента [agent.profiles] как каталог профилей входа: вход
    кладёт в области токена профили, выданные ролям вошедшего, а
    подключение пишет процессу только их. Создаётся сборкой службы."""

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


@dataclass
class Admission:
    """Вход подключения на время запроса, который его открывает: кто вошёл
    и какое подключение роутер для него создал."""

    caller: Caller
    link: ClientLink | None = None


class ServiceRouter(AgentProtocolRouter):
    """Роутер протокола SDK, который вместо агента в процессе службы отдаёт
    подключение-ретранслятор (ClientLink) вошедшего.

    Создаётся сборкой службы; его получают AcpServer (streamable HTTP) и
    handle_websocket (WebSocket) SDK. Вошедшего обработчик запроса кладёт в
    admitted() на время вызова SDK, который открывает подключение.
    """

    def __init__(
        self,
        launcher: AgentLauncher,
        auth_methods: list[dict[str, Any]],
        idle_sec: float,
    ) -> None:
        self._launcher = launcher
        self._auth_methods = auth_methods
        self._idle_sec = idle_sec
        self._admission: ContextVar[Admission] = ContextVar("agent_admission")
        self._links: set[ClientLink] = set()

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
        link = ClientLink(
            input_stream,
            admission.caller,
            self._launcher,
            self._auth_methods,
            self._idle_sec,
            listening,
        )
        admission.link = link
        self._links.add(link)
        link.on_closed(partial(self._forget, link))
        logger.info("connection of %r opened", admission.caller.login)

        return link

    async def _forget(self, link: ClientLink) -> None:
        self._links.discard(link)

    async def close_all(self) -> None:
        for link in list(self._links):
            await link.close()


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

    def __init__(self, router: ServiceRouter, callers: CallerTokens) -> None:
        self._router = router
        self._server = AcpServer(router)
        self._callers = callers
        self._owners: dict[str, str] = {}

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
                return await self._server.handle_delete(connection_id=connection_id)

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
        link = admission.link
        if opened is None:
            return response

        if link is None:
            return response

        self._owners[opened] = caller.login
        link.on_closed(partial(self._closed, opened))

        return response

    async def _closed(self, connection_id: str) -> None:
        self._owners.pop(connection_id, None)
        await self._server.handle_delete(connection_id=connection_id)

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
        или без профилей сокет закрывается кодом 1008 до принятия."""
        caller = self._callers.of(websocket)
        if caller is None:
            await websocket.close(code=1008, reason="the handshake carries no token")
            return

        if not caller.profiles:
            await websocket.close(code=1008, reason="no agent profile is granted")
            return

        async with self._router.admitted(Admission(caller=caller)):
            await handle_websocket(self._router, websocket)

    async def close(self) -> None:
        await self._router.close_all()
        await self._server.close()


class AgentServiceHost:
    """Сборка процесса службы: вход, процессы агента и приложение ASGI.

    Создаётся входом процесса из конфига; serve() слушает адрес [agent] до
    остановки и гасит подключения с их процессами на выходе.
    """

    def __init__(self, config: AgentServiceConfig) -> None:
        self._config = config
        section = config.agent
        self._container = Container(level="app")
        self._container.provide(providers.get_process_config, config)
        self._container.provide(
            providers.profile_catalog, AgentProfiles(section.profiles)
        )
        self._container.eager(providers.user_directory)
        self._container.eager(providers.sign_ins)
        self._quota = ProcessQuota(section.process.max_processes)
        self._launcher = AgentLauncher(
            section.process,
            AgentSettings(section.settings, section.profiles),
            self._quota,
        )

    def quota(self) -> ProcessQuota:
        return self._quota

    def resource(self) -> str:
        section = self._config.agent
        return f"{section.public_url.rstrip('/')}{section.path}"

    async def start(self) -> None:
        Container.set_root(self._container)
        await self._container.start()

    async def stop(self) -> None:
        Container.set_root(None)
        await self._container.aclose()

    def app(self) -> Starlette:
        """Приложение службы: маршруты входа, метаданные ресурса ACP, адрес
        ACP и проба готовности."""
        section = self._config.agent
        auth = self._auth()
        provider = auth.resource(ServiceMark.RESOURCE, [])
        metadata = build_resource_metadata_url(AnyHttpUrl(self.resource()))
        router = ServiceRouter(
            self._launcher, self._auth_methods(str(metadata)), section.process.idle_sec
        )
        endpoint = AcpEndpoint(router, CallerTokens())
        guarded = RequireAuthMiddleware(endpoint, [], metadata)
        routes: list[BaseRoute] = [
            Route(ServiceMark.HEALTH, self._health, methods=["GET"]),
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

    async def _health(self, request: Request) -> Response:
        """Проба готовности: процесс слушает; число процессов агента."""
        return JSONResponse({"agent_processes": self._quota.taken()})

    def _auth_methods(self, metadata: str) -> list[dict[str, Any]]:
        """Способ входа в ответе initialize: токен службы, который адрес ACP
        проверил до подключения; auth/login с ним принимается сразу."""
        method = schema.AgentAuthMethod(
            method_id=ServiceMark.AUTH_METHOD,
            name="Boba sign-in",
            description=(
                f"OAuth 2.1 bearer token of {self._config.agent.public_url}, "
                "checked on every HTTP request and on the WebSocket handshake; "
                f"resource metadata: {metadata}"
            ),
        )

        return [method.model_dump(mode="json", by_alias=True, exclude_none=True)]

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
            ServiceMark.TOKEN_SALT,
        )
        assertions = None
        if proxy := self._config.proxy():
            assertions = ProxyAssertions(
                proxy.secret.get_secret_value(), proxy.max_skew_sec
            )

        sealed = SealedValues(session.auth_secret, ServiceMark.SEALED_SALT)
        declared: dict[str, OAuthClientInformationFull] = {}
        for client_id, client in section.clients.items():
            declared[client_id] = client.registered(client_id)

        server = AuthServer(
            public_url,
            self._container.resolved(providers.sign_ins),
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
            ServiceMark.NAME,
            {ServiceMark.RESOURCE: self.resource()},
        )

    async def serve(self) -> None:
        """Слушает адрес [agent] до остановки процесса."""
        section = self._config.agent
        await self.start()
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
                "boba-agent-service listens on %s:%d, acp at %s",
                section.host,
                section.port,
                self.resource(),
            )
            await listener.serve_until_stopped()
        finally:
            await self.stop()


class AgentServiceEntry:
    """Вход процесса: `boba-agent-service --config <toml> --site <toml>`."""

    PROG: ClassVar[str] = "boba-agent-service"

    def config_files(self) -> ConfigFiles:
        parser = argparse.ArgumentParser(
            prog=self.PROG, description="Boba agent as a service over ACP"
        )

        return ConfigArguments(parser).files()

    async def run(self) -> None:
        config = AgentServiceConfig.load(self.config_files())
        logging.config.dictConfig(config.logger)

        await AgentServiceHost(config).serve()


async def main() -> None:
    try:
        await AgentServiceEntry().run()
    except Exception as exc:
        raise SystemExit(
            f"boba-agent-service failed: {type(exc).__name__}: {exc}"
        ) from exc


def cli() -> None:
    asyncio.run(main())


if __name__ == "__main__":
    asyncio.run(main())
