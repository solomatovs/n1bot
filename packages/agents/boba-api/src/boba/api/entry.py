"""Точка входа api-процесса: конфиг, контейнер сервисов, приложение под {prefix}/api.

Ошибки:
RuntimeError — конфиг не найден или обязательная секция выключена.
"""

from __future__ import annotations

import logging.config
import os
import socket
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import ClassVar

import uvicorn
from fastapi import FastAPI

from boba.api.app import ApiApp
from boba.api.jwt_auth import JwtAuthenticator
from boba.chat.profiles import ChatProfiles
from boba.runtime import providers
from boba.runtime.config import RuntimeConfig
from boba.runtime.di import Container
from boba.runtime.plugins import CoreTools
from boba.sandbox.zygote import ZygoteRegistry

__all__ = ["ApiEntry", "ApiHost"]


class ApiHost:
    """Сборка процесса api: контейнер общих сервисов и корневое приложение."""

    @classmethod
    def build(cls, config: RuntimeConfig) -> FastAPI:
        container = Container(level="app")
        container.provide(providers.get_runtime_config, config)
        container.provide(providers.plugin_table, CoreTools.table)
        container.provide(
            providers.instance_name, f"{socket.gethostname()}:{config.api.port}"
        )
        container.eager(providers.kb_schema)
        container.eager(providers.connection_store)
        container.eager(providers.workflow_store)
        Container.set_root(container)

        table = providers.users_table(config)
        api = ApiApp.build(
            providers.runtime_refs(),
            JwtAuthenticator(config.api.auth_secret, lambda: table),
            lambda: table,
            ChatProfiles(config.profiles),
            config.api.cookie,
        )

        root = FastAPI(lifespan=cls._lifespan)
        root.state.container = container
        root.mount(config.api.mount_prefix(), api)

        return root

    @staticmethod
    @asynccontextmanager
    async def _lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
        container = app.state.container
        await container.start()

        try:
            yield
        finally:
            ZygoteRegistry.stop_all()
            Container.set_root(None)
            await container.aclose()


class ApiEntry:
    """python -m boba.api: конфиг из BOBA_CONFIG_PATH или BOBA_BASE/conf/config.toml."""

    CONFIG_ENV: ClassVar[str] = "BOBA_CONFIG_PATH"
    BASE_ENV: ClassVar[str] = "BOBA_BASE"
    CONFIG_RELATIVE: ClassVar[str] = "conf/config.toml"

    @classmethod
    def run(cls) -> None:
        config = RuntimeConfig.load(cls.config_path())
        logging.config.dictConfig(config.logger)

        app = ApiHost.build(config)
        uvicorn.run(
            app,
            host=config.api.host,
            port=config.api.port,
            log_config=None,
            log_level=None,
            access_log=True,
        )

    @classmethod
    def config_path(cls) -> Path:
        if config_path := os.environ.get(cls.CONFIG_ENV):
            return Path(config_path)

        base = os.environ.get(cls.BASE_ENV)
        if not base:
            msg = f"{cls.CONFIG_ENV} or {cls.BASE_ENV} is required"
            raise RuntimeError(msg)

        return Path(base) / cls.CONFIG_RELATIVE
