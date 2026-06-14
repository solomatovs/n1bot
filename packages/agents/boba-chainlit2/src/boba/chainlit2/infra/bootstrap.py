import asyncio
import logging
import logging.config
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from engineio.payload import Payload
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from boba.chainlit2.chat.auth import (
    CredentialsAuth,
    KerberosAuth,
    KerberosCredentialStore,
    LdapAuth,
)
from boba.chainlit2.infra import providers
from boba.chainlit2.infra.config import (
    AppConfig,
    AuthConfig,
    ChainlitExtendConfig,
    CredentialsAuthConfig,
    KerberosAuthConfig,
    LdapAuthConfig,
)
from boba.chainlit2.infra.di import Container


def run_app():
    # получаем настройки всего приложения
    if (config_path := os.environ.get("BOBA_CONFIG_PATH")) is None:
        raise ValueError("please pass env BOBA_CONFIG_PATH")

    c = providers.get_app_config(config_path=Path(config_path))

    # применяем настройки логирования
    logging.config.dictConfig(c.logger)

    app = FastAPI(lifespan=_run_container)

    # конфигурирует chainlit + DI из c (единственная точка конфигурации)
    _use_chainlit_middleware(app, c.chainlit)

    # единственная точка конфигурации DI (сама механика start/stop — в lifespan)
    container = _use_di_container(app, c)
    app.state.container = container

    # единственная точка подключения авторизации (стратегия выбирается конфигом);
    _use_auth(c.auth, container)

    # Start the server
    async def start():
        uv_config = uvicorn.Config(
            app,
            host=c.chainlit.run.host,
            port=c.chainlit.run.port,
            ws=c.chainlit.ws_protocol,
            # logger'у передаем None что бы
            # второй раз настройки логирования не применялись
            log_config=None,
            log_level=None,
            access_log=True,
            ws_per_message_deflate=c.chainlit.ws_per_message_deflate,
            ssl_keyfile=c.chainlit.run.ssl_key,
            ssl_certfile=c.chainlit.run.ssl_cert,
        )
        server = uvicorn.Server(uv_config)
        await server.serve()

    # Run the asyncio event loop instead of uvloop to enable re entrance
    asyncio.run(start())


@asynccontextmanager
async def _run_container(app: FastAPI) -> AsyncIterator[None]:
    "Механика DI-контейнера: прогрев eager-провайдеров на старте, teardown на стопе"
    container = app.state.container
    await container.start()

    try:
        yield
    finally:
        Container.set_session_hook(None)
        Container.set_root(None)
        await container.aclose()


def _use_chainlit_middleware(app: FastAPI, c: ChainlitExtendConfig):
    # импортируем chainlit
    import boba.chainlit2.chat  # type: ignore # noqa: F401, PLC0415

    # фронт берёт базовый путь для своих запросов (/user, /auth/config,
    # socket.io) из этого env: serve() подставляет его в index.html. Без
    # него фронт ходит в корень мимо маунта и ловит 404
    os.environ["CHAINLIT_ROOT_PATH"] = c.run.root_path
    # устанавливаю переменную окружения если передан auth_secret
    # это все потому, что chainlit только через переменную окружения
    # умеет доставать auth_secret
    if c.auth_secret:
        os.environ["CHAINLIT_AUTH_SECRET"] = c.auth_secret

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
    init_markdown(c.root)

    class ChainlitMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next):
            if not request.url.path.startswith(c.run.root_path):
                return JSONResponse(status_code=404, content={"detail": "Not found"})

            return await call_next(request)

    chainlit_app.add_middleware(ChainlitMiddleware)

    app.mount(c.run.root_path, chainlit_app)


def _use_auth(c: AuthConfig, container: Container) -> None:
    "Единая точка подключения авторизации chainlit; стратегия выбирается конфигом"
    from chainlit.server import app as chainlit_app  # noqa: PLC0415

    if isinstance(c, CredentialsAuthConfig):
        CredentialsAuth(c).install(chainlit_app)

    elif isinstance(c, KerberosAuthConfig):
        store = KerberosCredentialStore(
            renew=c.delegation.renew if c.delegation else False,
        )
        container.provide(providers.kerberos_credential_store, store)
        KerberosAuth(c, store).install(chainlit_app)

    elif isinstance(c, LdapAuthConfig):
        LdapAuth(c).install(chainlit_app)

    else:
        raise ValueError(f"unknown authorization type: {type(c).__name__}")


def _use_di_container(app: FastAPI, c: AppConfig) -> Container:
    "Конфигурирует DI"
    container = Container(level="app")
    container.provide(providers.get_app_config, c)
    Container.set_root(container)
    Container.set_session_hook(_get_or_create_session_container)
    _close_container_if_session_end()
    return container


# маркер Container в сессии chainlit
_SESSION_CONTAINER_KEY = "_di_session_container"


def _get_or_create_session_container():
    """
    Возвращает контейнер для текущей сессии chainlit
    """
    from chainlit.context import ChainlitContextException, get_context  # noqa: PLC0415
    from chainlit.user_session import user_session  # noqa: PLC0415

    try:
        # если функция вызвана вне chainlit сессии, то придет None
        get_context()
    except ChainlitContextException:
        # верну оригинальную ошибку
        return None

    container = user_session.get(_SESSION_CONTAINER_KEY)
    if container is None:
        container = Container(level="session", parent=Container.root)
        user_session.set(
            _SESSION_CONTAINER_KEY,
            container,
        )

    if not isinstance(container, Container):
        raise ValueError(
            f"UserSession used is not valid DI Container type: {type(container)}"
        )

    return container


def _close_container_if_session_end() -> None:
    """Закрыть session container когда закончиться сессия"""
    from chainlit.config import config as cl_config  # noqa: PLC0415
    from chainlit.user_session import user_session  # noqa: PLC0415

    # on_chat_end - если сработал, значит происходит завершение сессии пользователя
    # запоминаем существующий on_chat_end и делаем его prev
    prev = cl_config.code.on_chat_end

    # определяем wrapper над оригинальным on_chat_env
    async def on_chat_end():

        try:
            if prev:
                await prev()
        finally:
            if container := user_session.get(_SESSION_CONTAINER_KEY):
                await container.aclose()

    # заменяем собственным on_chat_env
    cl_config.code.on_chat_end = on_chat_end
