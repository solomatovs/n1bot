import asyncio
import logging
import logging.config
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import uvicorn
from engineio.payload import Payload
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

# side-effect: регистрирует chainlit-callback'и (@cl.on_message и пр.) в
# глобальном config.code. Цикла нет: chat -> providers/di -> config, не bootstrap
import boba.chainlit2.chat  # noqa: F401  # pyright: ignore[reportUnusedImport]
from boba.chainlit2.infra import providers
from boba.chainlit2.infra.config import AppConfig, get_app_config
from boba.chainlit2.infra.di import Container
from boba.chainlit2.infra.lifespan import Lifespans

SESSION_CONTAINER_KEY = "_di_session_container"


def use_chainlit_middleware(app: FastAPI, c: AppConfig):
    # фронт берёт базовый путь для своих запросов (/user, /auth/config,
    # socket.io) из этого env: serve() подставляет его в index.html. Без
    # него фронт ходит в корень мимо маунта и ловит 404
    os.environ["CHAINLIT_ROOT_PATH"] = c.chainlit_config.run.root_path

    from chainlit.markdown import init_markdown  # noqa: PLC0415
    from chainlit.server import app as chainlit_app  # noqa: PLC0415
    from chainlit.server import sio  # noqa: PLC0415

    # engine.io heartbeat: даём клиенту пережить долгие паузы на брейкпоинтах
    sio.eio.ping_interval = c.ping_interval
    sio.eio.ping_timeout = c.ping_timeout
    # за паузу на брейкпоинте клиент копит события и шлёт их одним
    # polling-POST; дефолтных 16 пакетов не хватает
    Payload.max_decode_packets = c.max_decode_packets

    # Create the chainlit.md file if it doesn't exist
    init_markdown(c.chainlit_config.root)

    class ChainlitMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next):
            if not request.url.path.startswith(c.chainlit_config.run.root_path):
                return JSONResponse(status_code=404, content={"detail": "Not found"})

            return await call_next(request)

    chainlit_app.add_middleware(ChainlitMiddleware)

    app.mount(c.chainlit_config.run.root_path, chainlit_app)

    # единственная точка конфигурации DI (сама механика start/stop — в lifespan)
    use_di_container(app, c)


def use_di_container(app: FastAPI, c: AppConfig):
    "Конфигурирует DI из AppConfig: засев конфига, scope-хуки, список прогрева"
    root = Container(level="app")
    # c — единственный источник конфига: засеваем его, чтобы провайдеры
    # не строили AppConfig повторно через get_app_config()
    root.provide(providers.config, c)
    # что прогреть на старте — решает конфигуратор, а не lifespan
    root.eager(
        providers.openai_client,
        providers.debug_client,
        providers.client_settings,
    )
    app.state.container = root
    Container.set_root(root)
    Container.set_session_hook(_session_container)
    # session-контейнеры закрываются в on_chat_end (callback'и уже зарегистрированы)
    _install_session_teardown()


def _session_container():
    "get-or-create session-контейнер в cl.user_session (None вне chainlit-контекста)"
    from chainlit.context import ChainlitContextException, get_context  # noqa: PLC0415
    from chainlit.user_session import user_session  # noqa: PLC0415

    try:
        get_context()
    except ChainlitContextException:
        return None

    container = user_session.get(SESSION_CONTAINER_KEY)
    if container is None:
        container = Container(level="session", parent=Container.root)
        user_session.set(SESSION_CONTAINER_KEY, container)
    return container


def _install_session_teardown() -> None:
    "Чейнит chainlit on_chat_end: закрывает session-контейнер, не отнимая хук у юзера"
    from chainlit.config import config as cl_config  # noqa: PLC0415
    from chainlit.user_session import user_session  # noqa: PLC0415

    prev = cl_config.code.on_chat_end

    async def on_chat_end():
        try:
            if prev:
                await prev()
        finally:
            if container := user_session.get(SESSION_CONTAINER_KEY):
                await container.aclose()

    cl_config.code.on_chat_end = on_chat_end


@asynccontextmanager
async def run_container(app: FastAPI) -> AsyncIterator[None]:
    "Механика DI-контейнера: прогрев eager-провайдеров на старте, teardown на стопе"
    container = app.state.container
    # прогрев в lifespan: нужен event loop (async-провайдеры), cache-hit делает
    # резолв конкурентных сообщений atomic
    await container.start()

    try:
        yield
    finally:
        Container.set_session_hook(None)
        Container.set_root(None)
        await container.aclose()


def run_app():
    # получаем настройки всего приложения
    c = get_app_config()

    # применяем настройки логирования
    logging.config.dictConfig(c.logger_config)

    app = FastAPI(lifespan=Lifespans([run_container]))

    # конфигурирует chainlit + DI из c (единственная точка конфигурации)
    use_chainlit_middleware(app, c)

    # Start the server
    async def start():
        uv_config = uvicorn.Config(
            app,
            host=c.chainlit_config.run.host,
            port=c.chainlit_config.run.port,
            ws=c.ws_protocol,
            # logger'у передаем None что бы
            # второй раз настройки логирования не применялись
            log_config=None,
            log_level=None,
            access_log=True,
            ws_per_message_deflate=c.ws_per_message_deflate,
            ssl_keyfile=c.chainlit_config.run.ssl_key,
            ssl_certfile=c.chainlit_config.run.ssl_cert,
        )
        server = uvicorn.Server(uv_config)
        await server.serve()

    # Run the asyncio event loop instead of uvloop to enable re entrance
    asyncio.run(start())
