"""Сборка FastAPI-приложения: chainlit, авторизация, DI и отдача файлов."""

import asyncio
import logging
import logging.config
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from engineio.payload import Payload
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from boba.chainlit.auth import ChainlitAuthInstaller
from boba.chainlit.infra import providers
from boba.chainlit.infra.config import (
    AppConfig,
    ChainlitExtendConfig,
)
from boba.chainlit.infra.di import Container
from boba.chainlit.infra.error_middleware import DomainErrorMiddleware
from boba.chainlit.infra.log_context import RequestUserMiddleware, UserLogContext


def run_app(config_path: Path):
    """Запуск приложения; env chainlit к этому моменту выставлен AppEntry."""
    c = providers.get_app_config(config_path=config_path)

    UserLogContext.install()
    logging.config.dictConfig(c.logger)

    app = FastAPI(lifespan=_run_container)

    _use_chainlit_middleware(app, c.chainlit)

    _use_file_serving(c)

    container = _use_di_container(app, c)
    app.state.container = container

    _use_stream_journal(c)

    _use_canvas_viewers()

    _use_auth(c, container)

    _use_domain_error(app)

    # добавлен последним — выполняется первым, покрывает access-лог всех запросов
    app.add_middleware(RequestUserMiddleware)

    async def start():
        uv_config = uvicorn.Config(
            app,
            host=c.chainlit.host,
            port=c.chainlit.port,
            ws=c.chainlit.ws_protocol,
            log_config=None,
            log_level=None,
            access_log=True,
            ws_per_message_deflate=c.chainlit.ws_per_message_deflate,
            ssl_keyfile=c.chainlit.ssl_key,
            ssl_certfile=c.chainlit.ssl_cert,
            ssl_ca_certs=c.chainlit.ssl_ca_certs,
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
        Container.set_session_hook(None)
        Container.set_root(None)
        await container.aclose()


def _use_domain_error(app: FastAPI):
    from chainlit.server import app as chainlit_app  # noqa: PLC0415

    app.add_middleware(DomainErrorMiddleware)
    chainlit_app.add_middleware(DomainErrorMiddleware)


def _use_chainlit_middleware(app: FastAPI, config: ChainlitExtendConfig):
    import boba.chainlit.infra.callback  # type: ignore # noqa: F401, PLC0415
    from chainlit.markdown import init_markdown  # noqa: PLC0415
    from chainlit.server import app as chainlit_app  # noqa: PLC0415
    from chainlit.server import sio  # noqa: PLC0415

    sio.eio.ping_interval = config.ping_interval
    sio.eio.ping_timeout = config.ping_timeout
    Payload.max_decode_packets = config.max_decode_packets

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
        UploadPolicy,
        UploadRoute,
    )
    from boba.chainlit.domain.errors import InternalServiceError  # noqa: PLC0415
    from boba.chainlit.domain.keys import AttachmentUrl  # noqa: PLC0415
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


def _use_stream_journal(c: AppConfig) -> None:
    """Журнал вывода инструментов; без секции в конфиге потоков нет."""
    from boba.chainlit.data.stream_journal import (  # noqa: PLC0415
        DirVault,
        StreamJournal,
    )
    from boba.chainlit.data.upload import (  # noqa: PLC0415
        StreamServing,
        UploadPolicy,
    )
    from boba.chainlit.domain.keys import StreamUrl  # noqa: PLC0415
    from boba.chainlit.rendering.stream_view import ToolStreams  # noqa: PLC0415
    from chainlit.server import app as chainlit_app  # noqa: PLC0415

    journal_cfg = c.stream_journal
    if not journal_cfg.enable:
        return

    vault = DirVault(journal_cfg.dir)

    ToolStreams.configure(StreamJournal(vault, journal_cfg.reserve_bytes))

    serving = StreamServing(c.storage, UploadPolicy())
    chainlit_app.add_api_route(
        StreamUrl.ROUTE, serving.serve, methods=["GET"], include_in_schema=False
    )
    chainlit_app.router.routes.insert(0, chainlit_app.router.routes.pop())


def _use_canvas_viewers() -> None:
    """
    Вьюверы канваса — на старте: панель открывается кликом до первого хода
    """
    from boba.chainlit.infra.plugins import load_tools  # noqa: PLC0415

    load_tools(providers.get_raw_config())


def _use_auth(config: AppConfig, container: Container) -> None:
    from chainlit.server import app as chainlit_app  # noqa: PLC0415

    ChainlitAuthInstaller(config.chainlit.url_prefix, config.auth).install(chainlit_app)


def _use_di_container(app: FastAPI, c: AppConfig) -> Container:
    container = Container(level="app")
    container.provide(providers.get_app_config, c)
    container.eager(providers.chainlit_data_layer)
    container.eager(providers.langchain_checkpoint_saver)
    container.eager(providers.kb_schema)
    container.eager(providers.connection_store)
    Container.set_root(container)
    Container.set_session_hook(_get_or_create_session_container)
    _close_container_if_session_end()
    return container


def _get_or_create_session_container():
    from chainlit.context import ChainlitContextException, get_context  # noqa: PLC0415
    from chainlit.user_session import user_session  # noqa: PLC0415

    try:
        get_context()
    except ChainlitContextException:
        return None

    container = user_session.get("_di_session_container")
    if container is None:
        container = Container(level="session", parent=Container.root)
        user_session.set(
            "_di_session_container",
            container,
        )

    if not isinstance(container, Container):
        raise ValueError(
            f"UserSession used is not valid DI Container type: {type(container)}"
        )

    return container


def _close_container_if_session_end() -> None:
    from chainlit.config import config as cl_config  # noqa: PLC0415
    from chainlit.user_session import user_session  # noqa: PLC0415

    prev = cl_config.code.on_chat_end

    async def on_chat_end():

        try:
            if prev:
                await prev()
        finally:
            if container := user_session.get("_di_session_container"):
                await container.aclose()

    cl_config.code.on_chat_end = on_chat_end
