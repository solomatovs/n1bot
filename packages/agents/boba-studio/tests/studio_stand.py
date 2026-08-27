"""Стенд api без базы: вход по одному токену, входы приложения — заглушки.

Имя модуля своё: conftest.py у пакетов сталкиваются в общей сессии pytest."""

from __future__ import annotations

from typing import ClassVar

from boba.connection_broker.store import ConnectionStore
from boba.identity.api import AuthenticatedUser, Authenticator
from boba.krb.seal import SsoTickets
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
        )
