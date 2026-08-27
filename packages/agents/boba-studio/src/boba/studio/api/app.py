"""Сборка API-приложения: роутеры v1 (me, profiles, connections, tools, workflows),
socket.io и вход; процесс монтирует его под MOUNT."""

from __future__ import annotations

from typing import ClassVar

from fastapi import APIRouter, FastAPI

from boba.chat.profiles import ChatProfiles
from boba.identity.api import Authenticator
from boba.runtime.config import StudioConfig
from boba.runtime.http import DomainErrorMiddleware
from boba.runtime.refs import RuntimeRefs
from boba.studio.api.account import AccountApi
from boba.studio.api.auth import ApiAuth, TokenReader
from boba.studio.api.connections import ConnectionsApi
from boba.studio.api.tools import ThreadsSource, ToolCalling
from boba.studio.api.urls import ApiVersion
from boba.studio.api.workflow_socket import WorkflowNamespace, WorkflowSocket
from boba.studio.api.workflows import WorkflowApi

__all__ = ["ApiApp"]


class ApiApp:
    """Приложение API без знаний о хосте: всё нужное приходит аргументами build."""

    TITLE: ClassVar[str] = "boba api"
    MOUNT: ClassVar[str] = StudioConfig.MOUNT
    """Куда процесс монтирует приложение относительно url_prefix."""
    OPENAPI: ClassVar[str] = "/openapi.json"
    DOCS: ClassVar[str] = "/docs"

    @classmethod
    def build(
        cls,
        refs: RuntimeRefs,
        authenticator: Authenticator,
        threads: ThreadsSource,
        profiles: ChatProfiles,
        cookie: str,
    ) -> FastAPI:
        app = FastAPI(
            title=cls.TITLE, openapi_url=cls.OPENAPI, docs_url=cls.DOCS, redoc_url=None
        )
        ApiAuth(authenticator, TokenReader(cookie)).install(app)

        router = APIRouter(prefix=ApiVersion.V1.value)
        AccountApi(profiles).mount(router)
        ConnectionsApi(refs.connection_store, profiles).mount(router)
        ToolCalling(refs.tool_registry, profiles, threads).mount(router)
        WorkflowApi(refs.workflow_service, profiles).mount(router)
        app.include_router(router)

        auth = ApiAuth.of_app(app)
        namespace = WorkflowNamespace(
            refs.workflow_service, profiles, auth.user_of_environ
        )
        app.mount(WorkflowSocket.PATH, WorkflowSocket.build(namespace))

        app.add_middleware(DomainErrorMiddleware)

        return app

    @classmethod
    def socket_path(cls, url_prefix: str) -> str:
        """Полный путь socket.io API для страницы: {prefix}/api/socket.io."""
        return f"{url_prefix}{cls.MOUNT}{WorkflowSocket.PATH}"

    @classmethod
    def mount_prefix(cls, url_prefix: str) -> str:
        """Полный префикс API для страницы: {prefix}/api; версия — в путях схемы."""
        return f"{url_prefix}{cls.MOUNT}"
