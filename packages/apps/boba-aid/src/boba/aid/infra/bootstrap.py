"""Сборка приложения aid поверх FastAPI chainlit: авторизация, DI и отдача файлов.

Ошибки:
ConfigError — песочнице приложений MCP Apps разрешён CORS-доступ к чату либо
    в app_root нет страницы-прокси песочницы.
ServerStartError — порт чата или песочницы не удалось занять либо приложение
    не поднялось.
RuntimeError — провайдер контейнера отдал не тот компонент.
"""

import asyncio
import logging
import logging.config
from collections.abc import AsyncGenerator, Sequence
from contextlib import asynccontextmanager
from enum import StrEnum
from pathlib import Path
from urllib.parse import urljoin

import uvicorn
from chainlit.config import config as chainlit_config
from engineio.payload import Payload
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from omegaconf import DictConfig
from starlette.middleware.base import BaseHTTPMiddleware

from boba.aid.auth.http import DomainErrorMiddleware, StaleSessionMiddleware
from boba.aid.auth.installer import ChainlitAuthInstaller
from boba.aid.auth.session import BrowserSessions
from boba.aid.config.app import AppConfig
from boba.aid.config.models import AidSection
from boba.aid.domain.keys import AppPrefix
from boba.aid.infra import providers, services
from boba.aid.infra.di import Container
from boba.aid.infra.log_context import RequestUserMiddleware, UserLogContext
from boba.aid.infra.sandbox import AidListeners
from boba.aid.infra.session import (
    ChainlitSessions,
    SessionContainers,
    current_session,
)
from boba.aid.infra.socket_events import SocketEvents
from boba.aid.infra.stale_action import StaleActionMiddleware
from boba.aid.infra.turns import StaleTurnCloser
from boba.auth.config import KerberosAuthConfig, ProxyAuthConfig
from boba.cancellation import ProcessStop, StopReason
from boba.chat.messages import ChatMessages
from boba.config import ConfigError, bind
from boba.http_server import SignalledServer
from boba.identity.context import CallContexts
from boba.krb.layout import InstanceLayout
from boba.messaging.config import AppName
from boba.storage.client import StorageClient


def run_app(raw: DictConfig, conf_dir: Path):
    """Запуск приложения по загруженному дереву конфига; env chainlit к этому
    моменту выставлен AppEntry. conf_dir — каталог главного файла конфига."""
    c = bind(raw, "app", AppConfig)
    layout = InstanceLayout(conf_dir, Path(c.paths.data_dir), Path(c.tls.ca_file))
    layout.apply()
    cors = SandboxCors(chainlit_config.project.allow_origins)
    cors.check(c)

    UserLogContext.install()
    logging.config.dictConfig(c.logger)
    # ссылки на файлы и потоки строятся с префиксом приложения из конфига
    AppPrefix.install(c.server.url_prefix)

    app = FastAPI(lifespan=_run_container)

    _use_chainlit_middleware(app, c.server)

    storage = providers.storage_provider(c, providers.file_owners())
    _use_file_serving(c, storage)

    container = _use_di_container(app, c)
    container.provide(services.get_raw_config, raw)
    container.provide(providers.storage_provider, storage)
    # токен песочницы один на процесс: его получают и страница-прокси, и лента
    sandbox = providers.sandbox_links(c)
    container.provide(providers.sandbox_links, sandbox)
    app.state.container = container

    _use_stream_journal()

    _use_canvas_viewers()

    _use_domain_error(app)

    # добавлен последним — выполняется первым, покрывает access-лог всех запросов
    app.add_middleware(
        RequestUserMiddleware,
        tokens=services.session_tokens(c),
        cookie=c.session.cookie,
    )

    async def start():
        # 0 отключает ws-пинг uvicorn: тогда живость сокета держит только
        # heartbeat engine.io, у которого свои интервалы
        ws_ping_interval = None
        if c.server.ws_ping_interval:
            ws_ping_interval = c.server.ws_ping_interval

        ws_ping_timeout = None
        if c.server.ws_ping_timeout:
            ws_ping_timeout = c.server.ws_ping_timeout

        listeners = AidListeners(
            c, app, Path(chainlit_config.root).resolve(), sandbox.token
        )
        uv_config = uvicorn.Config(
            listeners,
            host=c.server.host,
            port=c.server.port,
            ws=c.server.ws_protocol,
            log_config=None,
            log_level=None,
            access_log=True,
            ws_ping_interval=ws_ping_interval,
            ws_ping_timeout=ws_ping_timeout,
            ws_per_message_deflate=c.server.ws_per_message_deflate,
            ssl_keyfile=c.server.ssl_key,
            ssl_certfile=c.server.ssl_cert,
            ssl_ca_certs=c.server.ssl_ca_certs,
            timeout_graceful_shutdown=c.server.shutdown_timeout_sec,
        )
        server = SignalledServer(uv_config, ProcessStop())
        await server.serve_until_stopped(listeners.sockets())

    asyncio.run(start())


class SandboxOrigin(StrEnum):
    """Источники, которые в allow_origins chainlit открыли бы API чата
    приложениям песочницы: любой и непрозрачный — им представляется
    iframe приложения без allow-same-origin."""

    ANY = "*"
    OPAQUE = "null"


class SandboxCors:
    """Проверка старта: CORS chainlit закрыт песочнице приложений MCP Apps.

    Скрипт приложения сервера не должен ходить в API чата с куками
    пользователя: allow_origins chainlit не содержит ни "*", ни "null", ни
    источника единственной песочницы платформы. Создаёт run_app со списком
    allow_origins, который chainlit разобрал из своего конфига.
    """

    def __init__(self, allowed: Sequence[str]) -> None:
        self._allowed = allowed

    def check(self, config: AppConfig) -> None:
        """Ошибки:
        ConfigError — CORS chainlit открыт песочнице.
        """
        origin = config.server.sandbox_origin()
        for opened in (*SandboxOrigin, origin):
            if opened not in self._allowed:
                continue

            msg = (
                f"[aid].sandbox_port {config.server.sandbox_port}: chainlit "
                f"[project].allow_origins {list(self._allowed)} lets {opened!r} "
                "call the chat API with the cookies of the user, so the apps of "
                f"the sandbox {origin} could act as the user; list in "
                "allow_origins only the origins that embed the chat, without '*' "
                "and without the sandbox origin"
            )
            raise ConfigError(msg)


@asynccontextmanager
async def _run_container(app: FastAPI) -> AsyncGenerator[None, None]:
    container = app.state.container
    await container.start()
    # роуты и колбэк входа ставятся до первого запроса: сервис входа живёт в контейнере
    _use_auth(container)
    _use_thread_deletion(container)
    _use_mcp_oauth_callback(container)

    try:
        yield
    finally:
        container.resolved(services.runs).stop_all(StopReason.SHUTDOWN)
        Container.set_session_hook(None)
        await SessionContainers.close_all()
        Container.set_root(None)
        await container.aclose()


def _use_thread_deletion(container: Container) -> None:
    """Удаление треда снимает и сессию агента: слушатель слоя данных."""
    from boba.aid.data.data_layer import PostgresDataLayer  # noqa: PLC0415

    layer = container.resolved(providers.chainlit_data_layer)
    if not isinstance(layer, PostgresDataLayer):
        msg = (
            "thread deletion setup expects a PostgresDataLayer data layer, "
            f"got {type(layer).__name__}"
        )
        raise RuntimeError(msg)

    layer.on_thread_deleted(container.resolved(providers.thread_deleter))


def _use_domain_error(app: FastAPI):
    from chainlit.server import app as chainlit_app  # noqa: PLC0415

    app.add_middleware(DomainErrorMiddleware)
    chainlit_app.add_middleware(DomainErrorMiddleware)
    # добавлен после обработчика ошибок — значит стоит перед ним и отсекает
    # действия мёртвых сессий до того, как они станут внутренней ошибкой
    chainlit_app.add_middleware(StaleActionMiddleware)


def _use_chainlit_middleware(app: FastAPI, config: AidSection):
    from chainlit.markdown import init_markdown  # noqa: PLC0415
    from chainlit.server import app as chainlit_app  # noqa: PLC0415
    from chainlit.server import sio  # noqa: PLC0415

    from boba.aid.infra.callback import ChatCallbacks  # noqa: PLC0415

    callbacks = ChatCallbacks()
    callbacks.install()
    sio.eio.ping_interval = config.ping_interval
    sio.eio.ping_timeout = config.ping_timeout
    Payload.max_decode_packets = config.max_decode_packets

    # хендлеры chainlit зарегистрированы импортом chainlit.server выше
    SocketEvents.install()

    init_markdown(chainlit_config.root)

    # адрес аватара фронт ставит в <img> как есть, а префикс приложения chainlit
    # добавляет только адресам index.html: относительный ведём от корня приложения
    if avatar := chainlit_config.ui.default_avatar_file_url:
        app_url = f"{AppPrefix.render()}/"
        chainlit_config.ui.default_avatar_file_url = urljoin(app_url, avatar)

    class AppPrefixMiddleware(BaseHTTPMiddleware):
        """Отвечает 404 на пути вне префикса приложения [aid].url_prefix."""

        async def dispatch(self, request: Request, call_next):
            if not request.url.path.startswith(config.url_prefix):
                detail = (
                    f"path {request.url.path} is outside the app prefix "
                    f"{config.url_prefix}"
                )
                return JSONResponse(status_code=404, content={"detail": detail})

            return await call_next(request)

    class ElementsNoCacheMiddleware(BaseHTTPMiddleware):
        """Кастом-элементы правятся чаще фронта: браузеру их кэшировать нельзя,
        иначе после правки .jsx пользователи видят старый компонент."""

        async def dispatch(self, request: Request, call_next):
            response = await call_next(request)
            if "/public/elements/" in request.url.path:
                response.headers["Cache-Control"] = "no-cache"
            return response

    chainlit_app.add_middleware(AppPrefixMiddleware)
    chainlit_app.add_middleware(ElementsNoCacheMiddleware)

    app.mount(config.url_prefix, chainlit_app)


def _use_file_serving(c: AppConfig, storage: StorageClient) -> None:
    from chainlit.server import app as chainlit_app  # noqa: PLC0415

    from boba.aid.data.upload import (  # noqa: PLC0415
        CanvasServing,
        UploadPolicy,
        UploadRoute,
    )
    from boba.aid.domain.keys import CanvasFileUrl  # noqa: PLC0415

    policy = UploadPolicy()
    uploads = UploadRoute(storage, policy)
    uploads.install(chainlit_app)

    canvas = CanvasServing(storage, UploadPolicy())
    chainlit_app.add_api_route(
        CanvasFileUrl.ROUTE, canvas.serve, methods=["GET"], include_in_schema=False
    )
    chainlit_app.router.routes.insert(0, chainlit_app.router.routes.pop())


def _use_stream_journal() -> None:
    """Роут скачивания журнала вызова: сам журнал лежит у MCP-сервера."""
    from chainlit.server import app as chainlit_app  # noqa: PLC0415

    from boba.aid.data.upload import StreamServing  # noqa: PLC0415
    from boba.aid.domain.keys import StreamUrl  # noqa: PLC0415

    serving = StreamServing(providers.remote_journals_ref, providers.mcp_servers_ref)
    chainlit_app.add_api_route(
        StreamUrl.ROUTE, serving.serve, methods=["GET"], include_in_schema=False
    )
    chainlit_app.router.routes.insert(0, chainlit_app.router.routes.pop())


def _use_mcp_oauth_callback(container: Container) -> None:
    """Register the authenticated browser return after OAuth is ready."""
    from chainlit.server import app as chainlit_app  # noqa: PLC0415

    from boba.aid.mcp_servers.oauth import (  # noqa: PLC0415
        McpOAuthCallback,
        McpOAuthClientDocument,
    )

    config = container.resolved(providers.get_app_config)
    oauth = container.resolved(providers.mcp_browser_oauth)
    tokens = container.resolved(services.session_tokens)
    callback = McpOAuthCallback(oauth, tokens, config.session.cookie)
    chainlit_app.add_api_route(
        McpOAuthCallback.ROUTE, callback.serve, methods=["GET"], include_in_schema=False
    )
    chainlit_app.router.routes.insert(0, chainlit_app.router.routes.pop())
    metadata = McpOAuthClientDocument(oauth)
    chainlit_app.add_api_route(
        McpOAuthClientDocument.ROUTE,
        metadata.serve,
        methods=["GET"],
        include_in_schema=False,
    )
    chainlit_app.router.routes.insert(0, chainlit_app.router.routes.pop())


def _use_canvas_viewers() -> None:
    """Вьюверы канваса — на старте: панель открывается кликом до первого хода."""
    from boba.aid.canvas.panel import CanvasWatch  # noqa: PLC0415
    from boba.aid.canvas.tools import CanvasViewers  # noqa: PLC0415
    from boba.aid.infra.thread_room import CanvasRoomTransport  # noqa: PLC0415

    CanvasWatch.configure(CanvasRoomTransport())
    CanvasViewers.register_all()


def _use_auth(container: Container) -> None:
    from chainlit.server import app as chainlit_app  # noqa: PLC0415

    config = container.resolved(providers.get_app_config)
    if not isinstance(config, AppConfig):
        msg = (
            "auth setup expects AppConfig from the app config provider, "
            f"got {type(config).__name__}"
        )
        raise RuntimeError(msg)

    auth = container.resolved(services.auth_service)
    if not isinstance(auth, BrowserSessions):
        msg = (
            "auth setup expects BrowserSessions from the auth service provider, "
            f"got {type(auth).__name__}"
        )
        raise RuntimeError(msg)

    sessions = container.resolved(providers.session_source)
    if not isinstance(sessions, ChainlitSessions):
        msg = (
            "auth setup expects ChainlitSessions from the session source "
            f"provider, got {type(sessions).__name__}"
        )
        raise RuntimeError(msg)

    installer = ChainlitAuthInstaller(
        config.server.url_prefix,
        config.auth_entries(KerberosAuthConfig),
        config.auth_entries(ProxyAuthConfig),
        auth,
        config.session.session_ttl_sec,
        sessions,
        Path(chainlit_config.root).resolve(),
    )
    installer.install(chainlit_app)

    # cookie прежнего поколения сессий отсекается до маршрутов chainlit: его
    # декодер поколения не знает и принял бы старую сессию после рестарта
    chainlit_app.add_middleware(
        StaleSessionMiddleware, tokens=auth.tokens, cookie=auth.cookie()
    )


def _use_di_container(app: FastAPI, c: AppConfig) -> Container:
    from boba.aid.infra.thread_room import ThreadSurfaces  # noqa: PLC0415

    container = Container(level="app")
    container.provide(providers.get_app_config, c)
    container.provide(providers.thread_surfaces, ThreadSurfaces())
    container.provide(services.application_config, c)
    contexts = CallContexts()
    container.provide(services.call_contexts, contexts)
    container.provide(services.app_name, AppName("aid"))
    messages = ChatMessages()
    container.provide(services.message_codec, messages.codec)
    container.provide(services.messaging_backends, c.MESSAGING_BACKENDS)
    container.provide(services.stale_observer, StaleTurnCloser)
    tokens = services.session_tokens(c)
    container.provide(services.session_tokens, tokens)
    sessions = ChainlitSessions(tokens)
    ChainlitSessions.install(sessions)
    container.provide(providers.session_source, sessions)
    container.provide(services.live_sessions, sessions)
    container.eager(providers.get_app_config)
    container.eager(services.postgres_pool)
    container.eager(services.users_table)
    container.eager(services.auth_service)
    container.eager(services.messaging_runtime)
    container.eager(services.session_keeper)
    container.eager(providers.chat_profiles_registry)
    container.eager(providers.chainlit_data_layer)
    container.eager(services.call_ambient)
    container.eager(
        providers.agent_launcher,
        providers.thread_deleter,
        providers.thread_agents_index,
    )
    container.eager(providers.remote_journals)
    container.eager(providers.sandbox_links)
    container.eager(providers.mcp_servers)
    container.eager(providers.mcp_updates)
    container.eager(providers.agent_feeds)
    container.eager(services.command_runner)
    Container.set_root(container)
    Container.set_session_hook(_session_container)
    return container


def _session_container() -> Container | None:
    session = current_session()
    if not session.present:
        return None

    return SessionContainers.of(session.id)
