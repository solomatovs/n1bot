"""Сборка API-приложения: роутеры v1, socket.io и вход; хост монтирует его под MOUNT."""

from __future__ import annotations

from typing import ClassVar

from fastapi import APIRouter, FastAPI

from boba.api.auth import ApiAuth, TokenReader
from boba.api.errors import DomainErrorMiddleware
from boba.api.tools import ThreadsSource, ToolCalling
from boba.api.urls import ApiVersion
from boba.api.workflow_socket import WorkflowNamespace, WorkflowSocket
from boba.api.workflows import WorkflowApi
from boba.chat.profiles import ChatProfiles
from boba.identity.api import Authenticator
from boba.runtime.refs import RuntimeRefs

__all__ = ["ApiApp"]


class ApiApp:
    """Приложение API без знаний о хосте: всё нужное приходит аргументами build."""

    TITLE: ClassVar[str] = "boba api"
    MOUNT: ClassVar[str] = "/api"
    """Куда хост монтирует приложение относительно своего url_prefix."""
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
    def v1_prefix(cls, url_prefix: str) -> str:
        """Полный префикс REST v1 для страницы: {prefix}/api/v1."""
        return f"{url_prefix}{cls.MOUNT}{ApiVersion.V1.value}"
