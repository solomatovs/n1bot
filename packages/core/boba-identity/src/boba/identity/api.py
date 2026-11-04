"""Вход через API: пользователь входа, строки users и порт аутентификации.

Идентификатор подтверждённого пользователя вычисляется из его личности.
"""

from __future__ import annotations

from abc import abstractmethod
from collections.abc import Iterable
from datetime import datetime
from typing import Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from boba.identity.authentication import AuthenticatedPrincipal
from boba.identity.principal import UserIdentity
from boba.identity.session import Login

__all__ = [
    "Authenticator",
    "RoleCatalog",
    "StoredUser",
    "UserRows",
]


class StoredUser(BaseModel):
    """Пользователь: UUID, подтверждённая личность и текущее членство."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID
    created_at: datetime
    identity: UserIdentity
    roles: frozenset[str] = frozenset()

    @property
    def identifier(self) -> Login:
        return self.identity.identifier


class UserRows(Protocol):
    """Строки users целиком: чтение по логину и id, upsert."""

    @abstractmethod
    async def stored(self, identity: UserIdentity) -> StoredUser | None:
        """None — строки с таким логином нет."""

    @abstractmethod
    async def stored_by_id(self, user_id: UUID) -> StoredUser | None:
        """None — строки с таким id нет."""

    @abstractmethod
    async def upsert(
        self,
        identity: UserIdentity,
        roles: frozenset[str] | None = None,
    ) -> StoredUser:
        """Создаёт пользователя; roles обновляет только автоматические назначения.

        None сохраняет членство, пустой набор снимает автоматические роли.
        """


class Authenticator(Protocol):
    """Пользователь по подписанному токену запроса: cookie либо bearer header."""

    @abstractmethod
    async def user_of_token(self, token: str) -> AuthenticatedPrincipal:
        """AuthenticationError — подпись, срок, identity или поколение не приняты."""


class RoleCatalog(Protocol):
    """Существующие роли для автоматического назначения и ручное членство."""

    async def resolve_roles(
        self, identity: UserIdentity, candidates: Iterable[str]
    ) -> frozenset[str]: ...
