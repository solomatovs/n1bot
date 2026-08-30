"""Стенд api без базы: вход по одному токену, входы приложения — заглушки.

Имя модуля своё: conftest.py у пакетов сталкиваются в общей сессии pytest."""

from __future__ import annotations

from typing import ClassVar
from uuid import UUID

from boba.connection_broker.store import ConnectionStore
from boba.identity.api import AuthenticatedUser, Authenticator, UserSettingsStore
from boba.identity.locks import MemoryLiveLocks
from boba.identity.session import UserMetadataField
from boba.krb.seal import SsoTickets
from boba.messaging import MemoryMessageBus
from boba.runtime.bus import ListenerState, StaticBusWatch
from boba.runtime.refs import RuntimeRefs
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
