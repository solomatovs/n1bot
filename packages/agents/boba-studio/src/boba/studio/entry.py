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

from boba.chat.profiles import ChatProfiles
from boba.runtime import providers
from boba.runtime.config import (
    AppName,
    ConfigLocator,
    DevPage,
    RuntimeConfig,
    StudioConfig,
    StudioPath,
)
from boba.runtime.di import Container
from boba.runtime.plugins import CoreTools
from boba.runtime.signin import PasswordSignIns
from boba.runtime.sso import SpnegoGate, SsoSignIn
from boba.runtime.users import UsersTable
from boba.sandbox.zygote import ZygoteRegistry
from boba.studio.api.app import ApiAccess, ApiApp
from boba.studio.api.jwt_auth import JwtAuthenticator, JwtIssuer, SessionCookie
from boba.studio.api.signin import PageUrls, SignInWiring
from boba.studio.api.urls import ApiVersion, SignInUrl
from boba.studio.page import WorkflowDevPage, WorkflowPage

__all__ = ["StudioEntry", "StudioHost"]


class StudioHost:
    """Сборка процесса: контейнер общих сервисов, api-приложение и страница workflow."""

    @classmethod
    def build(cls, config: RuntimeConfig) -> FastAPI:
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
        Container.set_root(container)

        table = providers.users_table(config)
        access = ApiAccess(
            authenticator=JwtAuthenticator(config.studio.auth_secret, lambda: table),
            cookie=config.studio.cookie,
            threads=lambda: table,
        )
        api = ApiApp.build(
            providers.runtime_refs(),
            access,
            ChatProfiles(config.profiles),
            cls.signin_of(config, table),
        )

        root = FastAPI(lifespan=cls._lifespan, openapi_url=None, docs_url=None)
        root.state.container = container
        cls.page_of(config.studio).mount(root)
        root.mount(config.studio.api_prefix(), api)

        return root

    @staticmethod
    def signin_of(config: RuntimeConfig, users: UsersTable) -> SignInWiring:
        """Пароли и SPNEGO — провайдеры services; SSO на своём URL под api."""
        studio = config.studio
        authenticator = JwtAuthenticator(studio.auth_secret, lambda: users)
        gate = None
        if kerberos := config.kerberos():
            gate = SpnegoGate(SsoSignIn(kerberos, studio.auth_secret))

        page_root = f"{studio.url_prefix}{StudioPath.PAGE}"

        return SignInWiring(
            password=PasswordSignIns.of(config.auth),
            sso=gate,
            sso_url=f"{studio.api_prefix()}{ApiVersion.V1}{SignInUrl.SSO}",
            page=PageUrls(
                root=page_root,
                login=f"{page_root}/login",
                home=f"{page_root}/observe",
            ),
            issuer=JwtIssuer(studio.auth_secret, studio.session_ttl_sec),
            authenticator=authenticator,
            cookie=SessionCookie(
                studio.cookie, studio.cookie_samesite, studio.session_ttl_sec
            ),
            users=users,
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
            ws=config.studio.ws_protocol,
            log_config=None,
            log_level=None,
            access_log=True,
        )
