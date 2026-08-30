"""Точка входа студии: конфиг, контейнер сервисов, api под {prefix}/api и страница.

Ошибки:
RuntimeError — конфиг не найден или обязательная секция выключена.
"""

from __future__ import annotations

import logging.config
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI

from boba.auth import AuthService
from boba.cancellation import StopReason
from boba.chat.profiles import ChatProfiles
from boba.identity.run import RunRegistry
from boba.runtime import providers
from boba.runtime.config import (
    AppName,
    ConfigLocator,
    DevPage,
    StudioConfig,
    StudioPath,
    StudioRuntimeConfig,
)
from boba.runtime.di import Container
from boba.runtime.plugins import CoreTools
from boba.sandbox.zygote import ZygoteRegistry
from boba.studio.api.app import ApiAccess, ApiApp
from boba.studio.api.signin import PageUrls, SignInWiring
from boba.studio.api.urls import ApiVersion, SignInUrl
from boba.studio.page import WorkflowDevPage, WorkflowPage

__all__ = ["StudioEntry", "StudioHost"]


class StudioHost:
    """Сборка процесса: контейнер общих сервисов, api-приложение и страница workflow."""

    @classmethod
    def build(cls, config: StudioRuntimeConfig) -> FastAPI:
        container = Container(level="app")
        container.provide(providers.get_runtime_config, config)
        container.provide(providers.plugin_table, CoreTools.table)
        container.provide(providers.app_name, AppName.STUDIO)
        container.eager(providers.message_bus)
        container.eager(providers.stream_journal)
        container.eager(providers.kb_schema)
        container.eager(providers.connection_store)
        container.eager(providers.workflow_store)
        container.eager(providers.workflow_recovery)
        container.eager(providers.live_locks)
        container.eager(providers.lock_reaper)
        container.eager(providers.command_runner)
        # реестр инструментов грузится на старте: ленивая загрузка в обработчике запроса
        # держит event loop дольше ping-таймаута socket.io, и вкладки теряют сокет
        container.eager(providers.tool_registry)
        Container.set_root(container)

        table = providers.users_table(config)
        auth = providers.auth_service(config, table)
        access = ApiAccess(
            authenticator=auth,
            cookie=config.session.cookie,
            users=lambda: table,
        )
        api = ApiApp.build(
            providers.runtime_refs(),
            access,
            ChatProfiles(config.profiles),
            cls.signin_of(config, auth),
        )

        root = FastAPI(lifespan=cls._lifespan, openapi_url=None, docs_url=None)
        root.state.container = container
        root.state.users = table
        cls.page_of(config.studio).mount(root)
        root.mount(config.studio.api_prefix(), api)

        return root

    @staticmethod
    def signin_of(config: StudioRuntimeConfig, auth: AuthService) -> SignInWiring:
        """Вход над сервисом входа; SSO на своём URL под api."""
        studio = config.studio
        page_root = f"{studio.url_prefix}{StudioPath.PAGE}"

        return SignInWiring(
            auth=auth,
            sso_url=f"{studio.api_prefix()}{ApiVersion.V1}{SignInUrl.SSO}",
            page=PageUrls(
                root=page_root,
                login=f"{page_root}/login",
                home=f"{page_root}/observe",
            ),
        )

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
        await app.state.users.setup()

        try:
            yield
        finally:
            RunRegistry.stop_all(StopReason.SHUTDOWN)
            ZygoteRegistry.stop_all()
            Container.set_root(None)
            await container.aclose()


class StudioEntry:
    """python -m boba.studio: конфиг по ConfigLocator, uvicorn на [studio] host/port."""

    @classmethod
    def run(cls) -> None:
        config = StudioRuntimeConfig.load(ConfigLocator.path())
        logging.config.dictConfig(config.logger)

        app = StudioHost.build(config)
        uvicorn.run(
            app,
            host=config.studio.host,
            port=config.studio.port,
            ws=config.studio.ws_protocol,
            log_config=None,
            log_level=None,
            access_log=True,
        )
