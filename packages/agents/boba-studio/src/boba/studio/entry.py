"""Точка входа студии: конфиг, контейнер сервисов, api под {prefix}/api и страница.

Ошибки:
RuntimeError — конфиг не найден или обязательная секция выключена.
"""

from __future__ import annotations

import logging.config
import socket
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI

from boba.chat.profiles import ChatProfiles
from boba.runtime import providers
from boba.runtime.config import ConfigLocator, DevPage, RuntimeConfig, StudioConfig
from boba.runtime.di import Container
from boba.runtime.plugins import CoreTools
from boba.sandbox.zygote import ZygoteRegistry
from boba.studio.api.app import ApiApp
from boba.studio.api.jwt_auth import JwtAuthenticator
from boba.studio.page import WorkflowDevPage, WorkflowPage

__all__ = ["StudioEntry", "StudioHost"]


class StudioHost:
    """Сборка процесса: контейнер общих сервисов, api-приложение и страница workflow."""

    @classmethod
    def build(cls, config: RuntimeConfig) -> FastAPI:
        container = Container(level="app")
        container.provide(providers.get_runtime_config, config)
        container.provide(providers.plugin_table, CoreTools.table)
        container.provide(
            providers.instance_name, f"{socket.gethostname()}:{config.studio.port}"
        )
        container.eager(providers.kb_schema)
        container.eager(providers.connection_store)
        container.eager(providers.workflow_store)
        Container.set_root(container)

        table = providers.users_table(config)
        api = ApiApp.build(
            providers.runtime_refs(),
            JwtAuthenticator(config.studio.auth_secret, lambda: table),
            lambda: table,
            ChatProfiles(config.profiles),
            config.studio.cookie,
        )

        root = FastAPI(lifespan=cls._lifespan, openapi_url=None, docs_url=None)
        root.state.container = container
        cls.page_of(config.studio).mount(root)
        root.mount(config.studio.api_prefix(), api)

        return root

    @staticmethod
    def page_of(studio: StudioConfig) -> WorkflowPage | WorkflowDevPage:
        if isinstance(studio.page, DevPage):
            return WorkflowDevPage(studio.page.url, studio.url_prefix, studio)

        return WorkflowPage(studio.dist, studio.url_prefix, studio)

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


class StudioEntry:
    """python -m boba.studio: конфиг по ConfigLocator, uvicorn на [studio] host/port."""

    @classmethod
    def run(cls) -> None:
        config = RuntimeConfig.load(ConfigLocator.path())
        logging.config.dictConfig(config.logger)

        app = StudioHost.build(config)
        uvicorn.run(
            app,
            host=config.studio.host,
            port=config.studio.port,
            log_config=None,
            log_level=None,
            access_log=True,
        )
