"""Сборка FastAPI-приложения chainlit: авторизация, DI и отдача файлов.

API и страница workflow живут в процессе boba-studio; маршруты разводит nginx."""

import asyncio
import logging
import logging.config
import socket
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from engineio.payload import Payload
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from boba.access import GrantCheck
from boba.chainlit.auth.installer import ChainlitAuthInstaller
from boba.chainlit.infra import providers
from boba.chainlit.infra.config import (
    AppConfig,
    ChainlitExtendConfig,
)
from boba.chainlit.infra.log_context import RequestUserMiddleware, UserLogContext
from boba.chainlit.infra.session import ChainlitSessions, current_session
from boba.chainlit.infra.socket_events import SocketEvents
from boba.chainlit.infra.stale_action import StaleActionMiddleware
from boba.runtime import providers as runtime
from boba.runtime.config import RawConfig
from boba.runtime.di import Container
from boba.runtime.http import DomainErrorMiddleware
from boba.sandbox.zygote import ZygoteRegistry


def run_app(config_path: Path):
    """Запуск приложения; env chainlit к этому моменту выставлен AppEntry."""
    c = AppConfig.load(config_path)

    UserLogContext.install()
    logging.config.dictConfig(c.logger)

    app = FastAPI(lifespan=_run_container)

    _use_chainlit_middleware(app, c.chainlit)

    _use_file_serving(c)

    container = _use_di_container(app, c)
    app.state.container = container

    _use_stream_journal(c)

    _use_canvas_viewers()

    _use_auth(c)

    _use_domain_error(app)

    # добавлен последним — выполняется первым, покрывает access-лог всех запросов
    app.add_middleware(RequestUserMiddleware)

    async def start():
        # 0 отключает ws-пинг uvicorn: тогда живость сокета держит только
        # heartbeat engine.io, у которого свои интервалы
        ws_ping_interval = None
        if c.chainlit.ws_ping_interval:
            ws_ping_interval = c.chainlit.ws_ping_interval

        ws_ping_timeout = None
        if c.chainlit.ws_ping_timeout:
            ws_ping_timeout = c.chainlit.ws_ping_timeout

        uv_config = uvicorn.Config(
            app,
            host=c.chainlit.host,
            port=c.chainlit.port,
            ws=c.chainlit.ws_protocol,
            log_config=None,
            log_level=None,
            access_log=True,
            ws_ping_interval=ws_ping_interval,
            ws_ping_timeout=ws_ping_timeout,
            ws_per_message_deflate=c.chainlit.ws_per_message_deflate,
            ssl_keyfile=c.chainlit.ssl_key,
            ssl_certfile=c.chainlit.ssl_cert,
            ssl_ca_certs=c.chainlit.ssl_ca_certs,
            timeout_graceful_shutdown=c.chainlit.shutdown_timeout_sec,
        )
        server = uvicorn.Server(uv_config)
        await server.serve()

    try:
        asyncio.run(start())
    except KeyboardInterrupt:
        # uvicorn повторно кидает SIGINT после штатного shutdown — это не ошибка
        logging.getLogger(__name__).info("stopped by the user")


@asynccontextmanager
async def _run_container(app: FastAPI) -> AsyncGenerator[None, None]:
    container = app.state.container
    await container.start()

    try:
        yield
    finally:
        ZygoteRegistry.stop_all()
        Container.set_session_hook(None)
        Container.set_root(None)
        await container.aclose()


def _use_domain_error(app: FastAPI):
    from chainlit.server import app as chainlit_app  # noqa: PLC0415

    app.add_middleware(DomainErrorMiddleware)
    chainlit_app.add_middleware(DomainErrorMiddleware)
    # добавлен после обработчика ошибок — значит стоит перед ним и отсекает
    # действия мёртвых сессий до того, как они станут внутренней ошибкой
    chainlit_app.add_middleware(StaleActionMiddleware)


def _use_chainlit_middleware(app: FastAPI, config: ChainlitExtendConfig):
    import boba.chainlit.infra.callback  # type: ignore # noqa: F401, PLC0415
    from chainlit.markdown import init_markdown  # noqa: PLC0415
    from chainlit.server import app as chainlit_app  # noqa: PLC0415
    from chainlit.server import sio  # noqa: PLC0415

    sio.eio.ping_interval = config.ping_interval
    sio.eio.ping_timeout = config.ping_timeout
    Payload.max_decode_packets = config.max_decode_packets

    # хендлеры chainlit зарегистрированы импортом chainlit.server выше
    SocketEvents.install()

    init_markdown(config.root)

    class ChainlitMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next):
            if not request.url.path.startswith(config.url_prefix):
                return JSONResponse(status_code=404, content={"detail": "Not found"})

            return await call_next(request)

    class ElementsNoCacheMiddleware(BaseHTTPMiddleware):
        """Кастом-элементы правятся чаще фронта: браузеру их кэшировать нельзя,
        иначе после правки .jsx пользователи видят старый компонент."""

        async def dispatch(self, request: Request, call_next):
            response = await call_next(request)
            if "/public/elements/" in request.url.path:
                response.headers["Cache-Control"] = "no-cache"
            return response

    chainlit_app.add_middleware(ChainlitMiddleware)
    chainlit_app.add_middleware(ElementsNoCacheMiddleware)

    app.mount(config.url_prefix, chainlit_app)


def _use_file_serving(c: AppConfig) -> None:
    from boba.chainlit.data.data_layer import PostgresDataLayer  # noqa: PLC0415
    from boba.chainlit.data.storage import StorageFactory  # noqa: PLC0415
    from boba.chainlit.data.upload import (  # noqa: PLC0415
        AttachmentServing,
        CanvasServing,
        UploadPolicy,
        UploadRoute,
    )
    from boba.chainlit.domain.keys import (  # noqa: PLC0415
        AttachmentUrl,
        CanvasFileUrl,
    )
    from boba.identity.errors import InternalServiceError  # noqa: PLC0415
    from chainlit.data import get_data_layer  # noqa: PLC0415
    from chainlit.server import app as chainlit_app  # noqa: PLC0415

    storage = StorageFactory.create(c.storage)
    UploadRoute(storage, UploadPolicy()).install(chainlit_app)
    route_path = c.storage.public_prefix.removeprefix(c.chainlit.url_prefix)

    def data_layer() -> PostgresDataLayer:
        layer = get_data_layer()
        if not isinstance(layer, PostgresDataLayer):
            raise InternalServiceError(
                internal_detail=f"data layer is not PostgresDataLayer: {type(layer)}",
                user_detail="Attachment storage is not available",
            )
        return layer

    serving = AttachmentServing(storage, data_layer, UploadPolicy())
    chainlit_app.add_api_route(
        f"{route_path}{AttachmentUrl.ROUTE}", serving.serve, methods=["GET"]
    )
    chainlit_app.router.routes.insert(0, chainlit_app.router.routes.pop())

    canvas = CanvasServing(storage, UploadPolicy())
    chainlit_app.add_api_route(
        CanvasFileUrl.ROUTE, canvas.serve, methods=["GET"], include_in_schema=False
    )
    chainlit_app.router.routes.insert(0, chainlit_app.router.routes.pop())


def _use_stream_journal(c: AppConfig) -> None:
    """Роут скачивания журнала; сам журнал поднимает провайдер runtime."""
    from boba.chainlit.data.upload import (  # noqa: PLC0415
        StreamServing,
        UploadPolicy,
    )
    from boba.chainlit.domain.keys import StreamUrl  # noqa: PLC0415
    from chainlit.server import app as chainlit_app  # noqa: PLC0415

    if not c.stream_journal.enable:
        return

    serving = StreamServing(c.storage, UploadPolicy())
    chainlit_app.add_api_route(
        StreamUrl.ROUTE, serving.serve, methods=["GET"], include_in_schema=False
    )
    chainlit_app.router.routes.insert(0, chainlit_app.router.routes.pop())


def _use_canvas_viewers() -> None:
    """
    Вьюверы канваса — на старте: панель открывается кликом до первого хода
    """
    from boba.chainlit.canvas.panel import CanvasWatch  # noqa: PLC0415
    from boba.chainlit.infra.plugins import ChatPlugins  # noqa: PLC0415
    from boba.chainlit.infra.thread_room import CanvasRoomTransport  # noqa: PLC0415

    CanvasWatch.configure(CanvasRoomTransport())
    ChatPlugins.load(RawConfig.get(), runtime.runtime_refs())


def _use_auth(config: AppConfig) -> None:
    from chainlit.server import app as chainlit_app  # noqa: PLC0415

    installer = ChainlitAuthInstaller(config.chainlit.url_prefix, config.auth)
    installer.install(chainlit_app)


def _use_di_container(app: FastAPI, c: AppConfig) -> Container:
    from boba.chainlit.infra.kerberos_refresh import ChatRefreshSignal  # noqa: PLC0415
    from boba.chainlit.infra.plugins import ChatPlugins  # noqa: PLC0415

    container = Container(level="app")
    container.provide(providers.get_app_config, c)
    container.provide(runtime.get_runtime_config, c)
    container.provide(runtime.plugin_table, ChatPlugins.table)
    container.provide(runtime.refresh_signal, ChatRefreshSignal())
    container.provide(runtime.grant_check, GrantCheck.STRICT)
    container.provide(
        runtime.instance_name, f"{socket.gethostname()}:{c.chainlit.port}"
    )
    sessions = ChainlitSessions()
    ChainlitSessions.install(sessions)
    container.provide(providers.session_source, sessions)
    container.eager(providers.chainlit_data_layer)
    container.eager(providers.langchain_checkpoint_saver)
    container.eager(runtime.stream_journal)
    container.eager(runtime.kb_schema)
    container.eager(runtime.connection_store)
    container.eager(runtime.workflow_store)
    container.eager(runtime.workflow_recovery)
    # локальные модели грузятся на старте: первая сессия не ждёт веса
    container.eager(providers.local_chat_runtimes)
    Container.set_root(container)
    Container.set_session_hook(_get_or_create_session_container)
    _close_container_if_session_end()
    return container


def _get_or_create_session_container():
    session = current_session()
    if not session.present:
        return None

    container = session.value(Container.SESSION_KEY)
    if container is None:
        container = Container(level="session", parent=Container.root)
        session.remember(Container.SESSION_KEY, container)

    if not isinstance(container, Container):
        raise ValueError(
            f"UserSession used is not valid DI Container type: {type(container)}"
        )

    return container


def _close_container_if_session_end() -> None:
    from chainlit.config import config as cl_config  # noqa: PLC0415

    prev = cl_config.code.on_chat_end

    async def on_chat_end():

        try:
            if prev:
                await prev()
        finally:
            if container := current_session().value(Container.SESSION_KEY):
                await container.aclose()

    cl_config.code.on_chat_end = on_chat_end
