"""Сборка приложения aid поверх FastAPI chainlit: авторизация, DI и отдача файлов.

Ошибки:
ConfigError — серверу с приложениями MCP Apps разрешён CORS-доступ к чату.
RuntimeError — провайдер контейнера отдал не тот компонент.
"""

import asyncio
import logging
import logging.config
from collections.abc import AsyncGenerator, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import ClassVar
from urllib.parse import urljoin

import uvicorn
from chainlit.config import config as chainlit_config
from engineio.payload import Payload
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from boba.access import GrantCheck
from boba.aid.auth.installer import ChainlitAuthInstaller
from boba.aid.domain.keys import AppPrefix
from boba.aid.infra import providers
from boba.aid.infra.config import (
    AidSection,
    AppConfig,
)
from boba.aid.infra.log_context import RequestUserMiddleware, UserLogContext
from boba.aid.infra.session import (
    ChainlitSessions,
    SessionContainers,
    current_session,
)
from boba.aid.infra.socket_events import SocketEvents
from boba.aid.infra.stale_action import StaleActionMiddleware
from boba.auth import AuthService
from boba.cancellation import ProcessStop, StopReason
from boba.config.section import ConfigError
from boba.connections.sealed import SealKeys
from boba.identity.context import CallContexts
from boba.identity.run import Runs
from boba.runtime import providers as runtime
from boba.runtime.config import AppName, ConfigFiles
from boba.runtime.di import Container
from boba.runtime.http import (
    DomainErrorMiddleware,
    SignalledServer,
    StaleSessionMiddleware,
)
from boba.runtime.storage import StorageClient
from boba.toolrun.streams import CallJournals


def run_app(files: ConfigFiles):
    """Запуск приложения; env chainlit к этому моменту выставлен AppEntry."""
    c = AppConfig.load(files)
    SandboxCors(chainlit_config.project.allow_origins).check(c)

    UserLogContext.install()
    logging.config.dictConfig(c.logger)
    # ссылки на файлы и потоки строятся с префиксом приложения из конфига
    AppPrefix.install(c.aid.url_prefix)

    app = FastAPI(lifespan=_run_container)

    _use_chainlit_middleware(app, c.aid)

    storage = providers.storage_provider(
        c, providers.file_owners(providers.chat_profiles_registry(c))
    )
    _use_file_serving(c, storage)

    container = _use_di_container(app, c)
    container.provide(providers.storage_provider, storage)
    app.state.container = container

    _use_stream_journal()

    _use_canvas_viewers()

    _use_domain_error(app)

    # добавлен последним — выполняется первым, покрывает access-лог всех запросов
    app.add_middleware(
        RequestUserMiddleware,
        tokens=runtime.session_tokens(c),
        cookie=c.session.cookie,
    )

    async def start():
        # 0 отключает ws-пинг uvicorn: тогда живость сокета держит только
        # heartbeat engine.io, у которого свои интервалы
        ws_ping_interval = None
        if c.aid.ws_ping_interval:
            ws_ping_interval = c.aid.ws_ping_interval

        ws_ping_timeout = None
        if c.aid.ws_ping_timeout:
            ws_ping_timeout = c.aid.ws_ping_timeout

        uv_config = uvicorn.Config(
            app,
            host=c.aid.host,
            port=c.aid.port,
            ws=c.aid.ws_protocol,
            log_config=None,
            log_level=None,
            access_log=True,
            ws_ping_interval=ws_ping_interval,
            ws_ping_timeout=ws_ping_timeout,
            ws_per_message_deflate=c.aid.ws_per_message_deflate,
            ssl_keyfile=c.aid.ssl_key,
            ssl_certfile=c.aid.ssl_cert,
            ssl_ca_certs=c.aid.ssl_ca_certs,
            timeout_graceful_shutdown=c.aid.shutdown_timeout_sec,
        )
        server = SignalledServer(uv_config, ProcessStop())
        await server.serve_until_stopped()

    asyncio.run(start())


class SandboxCors:
    """Проверка старта: CORS chainlit закрыт песочницам приложений MCP Apps.

    Скрипт песочницы приложений сервера не должен ходить в API чата с куками
    пользователя: при секции apps у любого сервера allow_origins chainlit не
    содержит ни "*", ни источника песочницы. Создаёт run_app со списком
    allow_origins, который chainlit разобрал из своего конфига.
    """

    ANY: ClassVar[str] = "*"

    def __init__(self, allowed: Sequence[str]) -> None:
        self._allowed = allowed

    def check(self, config: AppConfig) -> None:
        """Ошибки:
        ConfigError — CORS chainlit открыт песочнице.
        """
        for name, server in config.mcp.servers.items():
            apps = server.apps
            if apps is None:
                continue

            origin = apps.origin()
            for opened in (self.ANY, origin):
                if opened not in self._allowed:
                    continue

                msg = (
                    f"[mcp.servers.{name}.apps]: chainlit [project].allow_origins "
                    f"{list(self._allowed)} lets {opened!r} call the chat API with "
                    f"the cookies of the user, so the apps of the sandbox {origin} "
                    "could act as the user; list in allow_origins only the origins "
                    "that embed the chat, without '*' and without the sandbox origin"
                )
                raise ConfigError(msg)


@asynccontextmanager
async def _run_container(app: FastAPI) -> AsyncGenerator[None, None]:
    container = app.state.container
    await container.start()
    # роуты и колбэк входа ставятся до первого запроса: сервис входа живёт в контейнере
    _use_auth(container)
    _use_thread_deletion(container)

    try:
        yield
    finally:
        container.resolved(runtime.runs).stop_all(StopReason.SHUTDOWN)
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

    ChatCallbacks().install()
    sio.eio.ping_interval = config.ping_interval
    sio.eio.ping_timeout = config.ping_timeout
    Payload.max_decode_packets = config.max_decode_packets

    # хендлеры chainlit зарегистрированы импортом chainlit.server выше
    SocketEvents.install()

    init_markdown(config.root)

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

    UploadRoute(storage, UploadPolicy()).install(chainlit_app)

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

    auth = container.resolved(runtime.auth_service)
    if not isinstance(auth, AuthService):
        msg = (
            "auth setup expects AuthService from the auth service provider, "
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

    sso_path = ""
    if auth.providers().sso:
        sso_path = config.sso_path()

    installer = ChainlitAuthInstaller(
        config.aid.url_prefix,
        sso_path,
        config.proxy(),
        auth,
        config.session.session_ttl_sec,
        sessions,
        Path(config.aid.root).resolve(),
    )
    installer.install(chainlit_app)

    # cookie прежнего поколения сессий отсекается до маршрутов chainlit: его
    # декодер поколения не знает и принял бы старую сессию после рестарта
    chainlit_app.add_middleware(
        StaleSessionMiddleware, tokens=auth.tokens, cookie=auth.cookie()
    )


def _use_di_container(app: FastAPI, c: AppConfig) -> Container:
    from boba.aid.infra.plugins import ChatPlugins  # noqa: PLC0415
    from boba.aid.infra.thread_room import ThreadSurfaces  # noqa: PLC0415

    container = Container(level="app")
    container.provide(providers.get_app_config, c)
    container.provide(providers.thread_surfaces, ThreadSurfaces())
    container.provide(runtime.get_runtime_config, c)
    contexts = CallContexts()
    runs = Runs(contexts)
    plugins = ChatPlugins(contexts)
    container.provide(runtime.plugin_table, plugins.table)
    container.provide(runtime.call_contexts, contexts)
    container.provide(runtime.runs, runs)
    # журналов у чата нет, но реестр собственных инструментов читает их держатель
    container.provide(runtime.call_journals, CallJournals(None, runs))
    container.provide(
        runtime.own_tools, plugins.own_tools(runtime.connection_store_ref)
    )
    container.provide(runtime.grant_check, GrantCheck.STRICT)
    container.provide(runtime.app_name, AppName.AID)
    tokens = runtime.session_tokens(c)
    container.provide(runtime.session_tokens, tokens)
    sessions = ChainlitSessions(tokens)
    ChainlitSessions.install(sessions)
    container.provide(providers.session_source, sessions)
    container.provide(runtime.live_sessions, sessions)
    # реестр типов нужен загрузчику плагинов ещё до старта контейнера:
    # по нему параметры-соединения получают вид; сам реестр — чистый discover
    container.provide(runtime.connection_types, runtime.connection_types())
    container.provide(runtime.seal_keys, SealKeys())
    container.eager(providers.get_app_config)
    container.eager(runtime.postgres_pool)
    container.eager(providers.harness_storage)
    container.eager(runtime.users_table)
    container.eager(runtime.auth_service)
    container.eager(runtime.credential_source)
    container.eager(runtime.session_keeper)
    container.eager(providers.chat_profiles_registry)
    container.eager(providers.chainlit_data_layer)
    container.eager(runtime.message_bus)
    container.eager(runtime.payload_store)
    container.eager(runtime.call_ambient)
    container.eager(runtime.connection_store)
    # инструменты собираются на старте: конфиг плагинов проверяется до сессий
    container.eager(runtime.tool_launchers)
    container.eager(runtime.tool_registry)
    # запуск агента — после реестра: ретранслятор отдаёт его инструменты сервером
    # chat; запуск процессов поднимается на старте; индекс агентов рендереры
    # тредов читают синхронно
    container.eager(
        providers.agent_launcher,
        providers.thread_deleter,
        providers.thread_agents_index,
    )
    container.eager(providers.remote_journals)
    container.eager(providers.mcp_servers)
    container.eager(providers.agent_feeds)
    # инстанс регистрируется после блокирующего старта зигот и моделей: иначе
    # его пульс молчит дольше lock_ttl, и соседи снимают его как мёртвый
    container.eager(runtime.live_locks)
    container.eager(runtime.lock_reaper)
    container.eager(runtime.command_runner)
    Container.set_root(container)
    Container.set_session_hook(_session_container)
    return container


def _session_container() -> Container | None:
    session = current_session()
    if not session.present:
        return None

    return SessionContainers.of(session.id)
