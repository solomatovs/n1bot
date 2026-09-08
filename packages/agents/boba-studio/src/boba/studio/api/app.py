"""Сборка API-приложения: роутеры v1 (me, profiles, connections, tools, workflows,
ресурсы хоста вроде каталога), socket.io и вход; процесс монтирует его под MOUNT."""

from __future__ import annotations

from abc import abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import ClassVar, Protocol

from fastapi import APIRouter, FastAPI

from boba.chat.profiles import ChatProfiles
from boba.connection_broker.api import ConnectionsApi
from boba.connection_broker.service import DeleteGuard, UserConnectionsService
from boba.identity.api import Authenticator
from boba.runtime.config import StudioPath
from boba.runtime.http import DomainErrorMiddleware, RequestTokens
from boba.runtime.refs import RuntimeRefs
from boba.studio.api.account import AccountApi, UsersSource
from boba.studio.api.auth import ApiAuth
from boba.studio.api.signin import SignInApi, SignInWiring
from boba.studio.api.streams import StreamApi
from boba.studio.api.tools import ToolCalling
from boba.studio.api.urls import ApiVersion
from boba.studio.api.workflow_socket import (
    StudioSessions,
    WorkflowNamespace,
    WorkflowSocket,
)
from boba.studio.api.workflows import WorkflowApi

__all__ = ["ApiAccess", "ApiApp", "ApiExtras", "ApiMount"]


class ApiMount(Protocol):
    """Ресурс хоста, который встаёт под версией api рядом с общими."""

    @abstractmethod
    def mount(self, app: FastAPI, router: APIRouter) -> None: ...


@dataclass(frozen=True)
class ApiExtras:
    """Что хост добавляет к общему api: свои ресурсы и охранники удаления
    соединений (строка, занятая ресурсом хоста, не удаляется)."""

    mounts: Sequence[ApiMount] = ()
    delete_guards: Sequence[DeleteGuard] = ()


@dataclass(frozen=True)
class ApiAccess:
    """Как api узнаёт вызывающего: проверка токена, cookie входа, строки users."""

    authenticator: Authenticator
    cookie: str
    users: UsersSource
    sessions: StudioSessions = field(default_factory=StudioSessions)
    """Реестр сокетов страницы: его же читает сторож сессий процесса."""


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
        extras: ApiExtras | None = None,
    ) -> FastAPI:
        if extras is None:
            extras = ApiExtras()

        app = FastAPI(
            title=cls.TITLE, openapi_url=cls.OPENAPI, docs_url=cls.DOCS, redoc_url=None
        )
        ApiAuth(access.authenticator, RequestTokens(access.cookie), profiles).install(
            app
        )

        router = APIRouter(prefix=ApiVersion.V1.value)
        if signin is not None:
            SignInApi(signin).mount(router)

        AccountApi(profiles, access.users, refs.message_bus).mount(router)
        ConnectionsApi(
            UserConnectionsService(refs.connection_store, extras.delete_guards),
            ApiAuth.subject_of,
            refs.credentials,
            refs.message_bus,
            refs.connection_types(),
        ).mount(app, router)
        ToolCalling(
            refs.tool_registry,
            profiles,
            refs.live_locks,
            refs.heartbeat_sec,
        ).mount(router)
        WorkflowApi(refs.workflow_service, profiles).mount(app, router)
        StreamApi(refs.workflow_service, profiles).mount(router)
        for mount in extras.mounts:
            mount.mount(app, router)

        app.include_router(router)

        auth = ApiAuth.of_app(app)
        namespace = WorkflowNamespace(
            refs.workflow_service,
            profiles,
            auth.socket_sign_in,
            refs.bus_watch,
            access.sessions,
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
