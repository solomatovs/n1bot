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

from boba.chainlit2.chat.auth import PasswordAuthCallbackInstaller
from boba.chainlit2.errors import DomainErrorMiddleware
from boba.chainlit2.infra import providers
from boba.chainlit2.infra.config import (
    AppConfig,
    ChainlitExtendConfig,
    FixAuthConfig,
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

    # отдача файлов вложений с диска (роут на chainlit_app под его авторизацией)
    _use_file_serving(c)

    # единственная точка конфигурации DI (сама механика start/stop — в lifespan)
    container = _use_di_container(app, c)
    app.state.container = container

    # единственная точка подключения авторизации (стратегия выбирается конфигом);
    _use_auth(c, container)

    # единная точка обработки ошибок
    _use_domain_error(app)

    # Start the server
    async def start():
        uv_config = uvicorn.Config(
            app,
            host=c.chainlit.host,
            port=c.chainlit.port,
            ws=c.chainlit.ws_protocol,
            # logger'у передаем None что бы
            # второй раз настройки логирования не применялись
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


def _use_domain_error(app: FastAPI):
    from chainlit.server import app as chainlit_app  # noqa: PLC0415

    app.add_middleware(DomainErrorMiddleware)
    chainlit_app.add_middleware(DomainErrorMiddleware)


def _use_chainlit_middleware(app: FastAPI, config: ChainlitExtendConfig):
    # импортируем chainlit
    import boba.chainlit2.chat.callback  # type: ignore # noqa: F401, PLC0415

    # устанавливаю директорию относительно которой chainlit размещает свои файлы:
    # .files, .chainlit, public, public, config.toml, translations
    os.environ["CHAINLIT_APP_ROOT"] = config.root
    # фронт берёт базовый путь для своих запросов (/user, /auth/config,
    # socket.io) из этого env: serve() подставляет его в index.html. Без
    # него фронт ходит в корень мимо маунта и ловит 404
    os.environ["CHAINLIT_ROOT_PATH"] = config.url_prefix
    # устанавливаю переменную окружения если передан auth_secret
    # это все потому, что chainlit только через переменную окружения
    # умеет доставать auth_secret
    if config.auth_secret:
        os.environ["CHAINLIT_AUTH_SECRET"] = config.auth_secret

    from chainlit.markdown import init_markdown  # noqa: PLC0415
    from chainlit.server import app as chainlit_app  # noqa: PLC0415
    from chainlit.server import sio  # noqa: PLC0415

    # engine.io heartbeat: даём клиенту пережить долгие паузы на брейкпоинтах
    sio.eio.ping_interval = config.ping_interval
    sio.eio.ping_timeout = config.ping_timeout
    # за паузу на брейкпоинте клиент копит события и шлёт их одним
    # polling-POST; дефолтных 16 пакетов не хватает
    Payload.max_decode_packets = config.max_decode_packets

    # Create the chainlit.md file if it doesn't exist
    init_markdown(config.root)

    class ChainlitMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next):
            if not request.url.path.startswith(config.url_prefix):
                return JSONResponse(status_code=404, content={"detail": "Not found"})

            return await call_next(request)

    chainlit_app.add_middleware(ChainlitMiddleware)

    app.mount(config.url_prefix, chainlit_app)


def _use_file_serving(c: AppConfig) -> None:
    """
    Отдаёт файлы вложений с диска роутом на chainlit_app
    """
    from typing import Annotated  # noqa: PLC0415

    from chainlit.auth import get_current_user  # noqa: PLC0415
    from chainlit.server import app as chainlit_app  # noqa: PLC0415
    from chainlit.user import PersistedUser, User  # noqa: PLC0415
    from fastapi import Depends, HTTPException  # noqa: PLC0415
    from fastapi.responses import FileResponse  # noqa: PLC0415

    base_dir = Path(c.storage.files_dir).resolve()
    route_path = c.storage.public_prefix.removeprefix(c.chainlit.url_prefix)

    async def serve_upload(
        object_key: str,
        current_user: Annotated[
            User | PersistedUser | None, Depends(get_current_user)
        ],
    ) -> FileResponse:
        if not isinstance(current_user, PersistedUser):
            raise HTTPException(status_code=401, detail="Unauthorized")

        # проверяем владельца файла через путь к этому файлу.
        # папка с названием current_user.id хранит файлы пользователя
        if object_key.split("/", maxsplit=1)[0] != current_user.id:
            raise HTTPException(status_code=403, detail="Forbidden")

        # формируем путь к файлу и проверяем что он внутри base_dir
        path = base_dir.joinpath(object_key).resolve()

        if not path.is_relative_to(base_dir):
            raise HTTPException(status_code=404, detail="File not found")

        if not path.is_file():
            raise HTTPException(status_code=404, detail="File not found")

        return FileResponse(path)

    chainlit_app.add_api_route(
        f"{route_path}/{{object_key:path}}", serve_upload, methods=["GET"]
    )
    # поднимаем роут выше chainlit SPA catch-all "/{full_path:path}"
    chainlit_app.router.routes.insert(0, chainlit_app.router.routes.pop())


def _use_auth(config: AppConfig, container: Container) -> None:
    "Единая точка подключения авторизации chainlit"
    from chainlit.server import app as chainlit_app  # noqa: PLC0415

    password_callback = PasswordAuthCallbackInstaller()

    for auth in config.auth:
        if isinstance(auth, KerberosAuthConfig):
            from boba.chainlit2.chat.auth.kerberos import (  # noqa: PLC0415
                KerberosAuth,
            )

            KerberosAuth(config.chainlit.url_prefix, auth).install(chainlit_app)

        elif isinstance(auth, FixAuthConfig):
            from boba.chainlit2.chat.auth.fix import (  # noqa: PLC0415
                FixAuth,
            )

            password_callback.fix_auth_setup(FixAuth(auth))

        elif isinstance(auth, LdapAuthConfig):
            from boba.chainlit2.chat.auth.ldap import (  # noqa: PLC0415
                LdapAuth,
            )

            password_callback.ldap_auth_setup(LdapAuth(auth))

        else:
            raise ValueError(f"unknown authorization type: {type(auth).__name__}")

    # устанавливаю password callback если есть хотя бы один из вариантов
    password_callback.install_callback_if_any_exists()


def _use_di_container(app: FastAPI, c: AppConfig) -> Container:
    "Конфигурирует DI"
    container = Container(level="app")
    container.provide(providers.get_app_config, c)
    container.eager(providers.chainlit_data_layer)
    container.eager(providers.langchain_checkpoint_saver)
    Container.set_root(container)
    Container.set_session_hook(_get_or_create_session_container)
    _close_container_if_session_end()
    return container


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
            if container := user_session.get("_di_session_container"):
                await container.aclose()

    # заменяем собственным on_chat_env
    cl_config.code.on_chat_end = on_chat_end
