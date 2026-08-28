"""Сборка API-приложения: роутеры v1 (me, profiles, connections, tools, workflows),
socket.io и вход; процесс монтирует его под MOUNT."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from fastapi import APIRouter, FastAPI

from boba.chat.profiles import ChatProfiles
from boba.identity.api import Authenticator
from boba.runtime.config import StudioPath
from boba.runtime.http import DomainErrorMiddleware
from boba.runtime.refs import RuntimeRefs
from boba.studio.api.account import AccountApi
from boba.studio.api.auth import ApiAuth, TokenReader
from boba.studio.api.connections import ConnectionsApi
from boba.studio.api.signin import SignInApi, SignInWiring
from boba.studio.api.tools import ThreadsSource, ToolCalling
from boba.studio.api.urls import ApiVersion
from boba.studio.api.workflow_socket import WorkflowNamespace, WorkflowSocket
from boba.studio.api.workflows import WorkflowApi

__all__ = ["ApiAccess", "ApiApp"]


@dataclass(frozen=True)
class ApiAccess:
    """Как api узнаёт вызывающего: проверка токена, cookie входа, владение тредами."""

    authenticator: Authenticator
    cookie: str
    threads: ThreadsSource


class ApiApp:
    """Приложение API без знаний о хосте: всё нужное приходит аргументами build."""

    TITLE: ClassVar[str] = "boba api"
    MOUNT: ClassVar[str] = StudioPath.API
    """Куда процесс монтирует приложение относительно url_prefix."""
    OPENAPI: ClassVar[str] = "/openapi.json"
    DOCS: ClassVar[str] = "/docs"

    @classmethod
    def build(
        cls,
        refs: RuntimeRefs,
        access: ApiAccess,
        profiles: ChatProfiles,
        signin: SignInWiring | None,
    ) -> FastAPI:
        app = FastAPI(
            title=cls.TITLE, openapi_url=cls.OPENAPI, docs_url=cls.DOCS, redoc_url=None
        )
        ApiAuth(access.authenticator, TokenReader(access.cookie)).install(app)

        router = APIRouter(prefix=ApiVersion.V1.value)
        if signin is not None:
            SignInApi(signin).mount(router)

        AccountApi(profiles).mount(router)
        ConnectionsApi(refs.connection_store, profiles).mount(router)
        ToolCalling(refs.tool_registry, profiles, access.threads).mount(router)
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
