"""Сборка FastAPI-приложения: chainlit, авторизация, DI и отдача файлов."""

import asyncio
import logging
import logging.config
import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from engineio.payload import Payload
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from boba.auth import ChainlitAuthInstaller
from boba.auth.errors import DomainErrorMiddleware
from boba.chainlit2.infra import providers
from boba.chainlit2.infra.config import (
    AppConfig,
    ChainlitExtendConfig,
)
from boba.chainlit2.infra.di import Container


def run_app():
    if (config_path := os.environ.get("BOBA_CONFIG_PATH")) is None:
        raise ValueError("please pass env BOBA_CONFIG_PATH")

    c = providers.get_app_config(config_path=Path(config_path))

    logging.config.dictConfig(c.logger)

    app = FastAPI(lifespan=_run_container)

    _use_chainlit_middleware(app, c.chainlit)

    _use_file_serving(c)

    container = _use_di_container(app, c)
    app.state.container = container

    _use_auth(c, container)

    _use_domain_error(app)

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

    asyncio.run(start())


@asynccontextmanager
async def _run_container(app: FastAPI) -> AsyncGenerator[None]:
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
    import boba.chainlit2.chat.callback  # type: ignore # noqa: F401, PLC0415

    os.environ["CHAINLIT_APP_ROOT"] = config.root
    os.environ["CHAINLIT_ROOT_PATH"] = config.url_prefix
    if config.auth_secret:
        os.environ["CHAINLIT_AUTH_SECRET"] = config.auth_secret

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

    chainlit_app.add_middleware(ChainlitMiddleware)

    app.mount(config.url_prefix, chainlit_app)


def _use_file_serving(c: AppConfig) -> None:
    from typing import Annotated  # noqa: PLC0415

    from chainlit.auth import get_current_user  # noqa: PLC0415
    from chainlit.server import app as chainlit_app  # noqa: PLC0415
    from chainlit.user import PersistedUser, User  # noqa: PLC0415
    from fastapi import Depends, HTTPException  # noqa: PLC0415
    from fastapi.responses import FileResponse, Response  # noqa: PLC0415

    from boba.chainlit2.chat.data.storage import LocalStorageClient  # noqa: PLC0415

    storage = LocalStorageClient.from_config(c.storage)
    serve_from_disk = c.storage.kind == "local"
    base_dir = Path(c.storage.files_dir).resolve() if serve_from_disk else None
    route_path = c.storage.public_prefix.removeprefix(c.chainlit.url_prefix)

    async def serve_upload(
        object_key: str,
        current_user: Annotated[
            User | PersistedUser | None, Depends(get_current_user)
        ],
    ) -> Response:
        if not isinstance(current_user, PersistedUser):
            raise HTTPException(status_code=401, detail="Unauthorized")

        if object_key.split("/", maxsplit=1)[0] != current_user.id:
            raise HTTPException(status_code=403, detail="Forbidden")

        if base_dir is not None:
            path = base_dir.joinpath(object_key).resolve()

            if not path.is_relative_to(base_dir):
                raise HTTPException(status_code=404, detail="File not found")

            if not path.is_file():
                raise HTTPException(status_code=404, detail="File not found")

            return FileResponse(path)

        try:
            content = await storage.read_file(object_key)
        except (FileNotFoundError, ValueError) as e:
            raise HTTPException(status_code=404, detail="File not found") from e

        return Response(content=content, media_type="application/octet-stream")

    chainlit_app.add_api_route(
        f"{route_path}/{{object_key:path}}", serve_upload, methods=["GET"]
    )
    chainlit_app.router.routes.insert(0, chainlit_app.router.routes.pop())


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
