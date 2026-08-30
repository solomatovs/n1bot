"""Стенд api без базы: вход по одному токену, входы приложения — заглушки.

Имя модуля своё: conftest.py у пакетов сталкиваются в общей сессии pytest."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from typing import ClassVar
from uuid import UUID, uuid4

from boba.chat.profiles import ChatProfiles
from boba.chat.threads import ThreadOwnership
from boba.connection_broker.store import ConnectionStore
from boba.identity.api import AuthenticatedUser, Authenticator, UserSettingsStore
from boba.identity.locks import MemoryLiveLocks
from boba.identity.session import UserMetadataField
from boba.krb.seal import SsoTickets
from boba.messaging import MemoryMessageBus
from boba.runtime.bus import ListenerState, StaticBusWatch
from boba.runtime.config import StudioRuntimeConfig
from boba.runtime.refs import RuntimeRefs, StoreRef, TicketsRef
from boba.toolrun.registry import ToolRegistry
from boba.workflow_engine.service import WorkflowService


class StubAuthenticator(Authenticator):
    """Вход стенда: один известный токен -> заданный пользователь."""

    COOKIE: ClassVar[str] = "access_token"
    TOKEN: ClassVar[str] = "stand-token"

    def __init__(self, user: AuthenticatedUser | None) -> None:
        self._user = user

    async def user_of_token(self, token: str) -> AuthenticatedUser | None:
        if token != self.TOKEN:
            return None

        return self._user

    @classmethod
    def cookies(cls) -> dict[str, str]:
        return {cls.COOKIE: cls.TOKEN}


class MemoryUsers(UserSettingsStore):
    """Пользователи стенда в памяти: одна строка и выбранный профиль studio."""

    def __init__(self, user: AuthenticatedUser | None) -> None:
        self._user = user
        self.chosen: dict[UUID, str] = {}

    async def get_user(self, identifier: str) -> AuthenticatedUser | None:
        if self._user is None:
            return None

        if self._user.identifier != identifier:
            return None

        return self._user

    async def set_studio_profile(self, user_id: UUID, profile: str) -> None:
        self.chosen[user_id] = profile
        if self._user is None:
            return

        metadata = {**self._user.metadata, UserMetadataField.STUDIO_PROFILE: profile}
        self._user = self._user.model_copy(update={"metadata": metadata})

    def source(self) -> UserSettingsStore:
        return self


class NoRefs:
    """Входы приложения, которых у стенда нет: каждый отказывает RuntimeError."""

    @staticmethod
    async def registry() -> ToolRegistry:
        msg = "tool registry is not part of this stand"
        raise RuntimeError(msg)

    @staticmethod
    async def workflows() -> WorkflowService:
        msg = "workflow service is not part of this stand"
        raise RuntimeError(msg)

    @staticmethod
    def store() -> ConnectionStore:
        msg = "[connections] is disabled: user connections are unavailable"
        raise RuntimeError(msg)

    @staticmethod
    def tickets() -> SsoTickets | None:
        return None

    @classmethod
    def refs(cls) -> RuntimeRefs:
        return RuntimeRefs(
            tool_registry=cls.registry,
            workflow_service=cls.workflows,
            connection_store=cls.store,
            sso_tickets=cls.tickets,
            live_locks=lambda: MemoryLiveLocks("stand", 20),
            heartbeat_sec=1.0,
            bus_watch=lambda: StaticBusWatch(ListenerState.LISTENING),
            message_bus=lambda: MemoryMessageBus("stand"),
        )


class StubRefs:
    """Входы приложения для стендов API без части сервисов."""

    @staticmethod
    def services(
        tool_registry: Callable[[], Awaitable[ToolRegistry]],
        workflow_service: Callable[[], Awaitable[WorkflowService]],
    ) -> RuntimeRefs:
        """Реестр и сервис есть, соединений и билетов нет."""

        def no_store() -> ConnectionStore:
            msg = "connection store is not part of this stand"
            raise RuntimeError(msg)

        def no_tickets() -> None:
            return None

        return RuntimeRefs(
            tool_registry=tool_registry,
            workflow_service=workflow_service,
            connection_store=no_store,
            sso_tickets=no_tickets,
            live_locks=lambda: MemoryLiveLocks("stand", 20),
            heartbeat_sec=1.0,
            bus_watch=lambda: StaticBusWatch(ListenerState.LISTENING),
            message_bus=lambda: MemoryMessageBus("stand"),
        )

    @staticmethod
    def of(store: StoreRef, tickets: TicketsRef) -> RuntimeRefs:
        """Соединения и билеты есть, реестра и workflow нет."""

        async def no_registry() -> ToolRegistry:
            msg = "tool registry is not part of this stand"
            raise RuntimeError(msg)

        async def no_service() -> WorkflowService:
            msg = "workflow service is not part of this stand"
            raise RuntimeError(msg)

        return RuntimeRefs(
            tool_registry=no_registry,
            workflow_service=no_service,
            connection_store=store,
            sso_tickets=tickets,
            live_locks=lambda: MemoryLiveLocks("stand", 20),
            heartbeat_sec=1.0,
            bus_watch=lambda: StaticBusWatch(ListenerState.LISTENING),
            message_bus=lambda: MemoryMessageBus("stand"),
        )


class NoThreads:
    """Владение тредами стендам API не нужно."""

    @staticmethod
    def source() -> ThreadOwnership:
        msg = "thread ownership is not part of this stand"
        raise RuntimeError(msg)


class NoUsers:
    """Хранилище пользователей стендам API без /me не нужно."""

    @staticmethod
    def source() -> UserSettingsStore:
        msg = "users store is not part of this stand"
        raise RuntimeError(msg)


class StandProfiles:
    """Роли и профили стенда из конфига studio."""

    @staticmethod
    def roles(config: StudioRuntimeConfig) -> list[str]:
        return sorted(config.roles)

    @staticmethod
    def profiles(config: StudioRuntimeConfig) -> ChatProfiles:
        return ChatProfiles(config.profiles)

    @classmethod
    def profile(cls, config: StudioRuntimeConfig) -> str:
        """Первый профиль, видимый ролям стенда."""
        visible = cls.profiles(config).visible_for(frozenset(cls.roles(config)))
        names = sorted(visible)
        if not names:
            raise RuntimeError("stand config has no profile visible to its roles")

        return names[0]

    @staticmethod
    def user(config: StudioRuntimeConfig, extra_roles: Iterable[str] = ()) -> AuthenticatedUser:
        """Пользователь стенда со всеми ролями конфига."""
        roles = [*sorted(config.roles), *extra_roles]
        return AuthenticatedUser(
            id=str(uuid4()), identifier="user-1", metadata={"roles": roles}
        )
