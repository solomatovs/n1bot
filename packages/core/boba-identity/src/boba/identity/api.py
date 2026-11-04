"""Вход через API: пользователь входа, строки users и порт аутентификации.

Ошибки:
AuthenticationError — у входа нет строки users.
"""

from __future__ import annotations

from abc import abstractmethod
from collections.abc import Iterable
from datetime import datetime
from typing import Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from boba.identity.context import (
    Credential,
)
from boba.identity.errors import AuthenticationError
from boba.identity.principal import UserIdentity
from boba.identity.session import Login
from boba.identity.signin import SignedIn, SignInMetadata

__all__ = [
    "AuthenticatedUser",
    "Authenticator",
    "PersistedUsers",
    "RoleCatalog",
    "StoredUser",
    "UserIdentityAssignment",
    "UserIdentityChange",
    "UserIdentityMigration",
    "UserIdentitySnapshot",
    "UserRows",
    "UsersUpsert",
]


class AuthenticatedUser(BaseModel):
    """Пользователь и роли, записанные в подтверждённой сессии."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID
    identifier: Login
    created_at: datetime
    sign_in: SignInMetadata

    @property
    def roles(self) -> frozenset[str]:
        return self.sign_in.roles

    @property
    def profiles(self) -> frozenset[str]:
        return self.sign_in.profiles

    @property
    def credential(self) -> Credential:
        """Делегированный билет входа либо причина его отсутствия."""
        return self.sign_in.credential()


class StoredUser(BaseModel):
    """Пользователь и его членство в ролях; данные сессии здесь не хранятся."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID
    identifier: Login
    created_at: datetime
    identity: UserIdentity | None = None
    roles: frozenset[str] = frozenset()

    def authenticated(self) -> AuthenticatedUser:
        sign_in = SignInMetadata(identity=self.identity, roles=self.roles)
        try:
            sign_in.identity_for(self.identifier)
        except ValueError as exc:
            msg = f"stored user {self.id} cannot be authenticated: {exc}"
            raise AuthenticationError(msg) from exc

        return AuthenticatedUser(
            id=self.id,
            identifier=self.identifier,
            created_at=self.created_at,
            sign_in=sign_in,
        )


class UserIdentityAssignment(BaseModel):
    """Явное соответствие прежней строки users подтверждённой личности."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    user_id: UUID
    previous_identifier: Login
    identity: UserIdentity


class UserIdentityMigration(BaseModel):
    """Вход миграции users: соответствия администратора без угадывания бэкенда."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    users: tuple[UserIdentityAssignment, ...] = Field(min_length=1)
    seed_missing: bool = False

    @model_validator(mode="after")
    def _one_assignment_per_user(self) -> UserIdentityMigration:
        ids: set[UUID] = set()
        identifiers: set[Login] = set()
        for user in self.users:
            if user.user_id in ids:
                msg = f"identity migration repeats user id {user.user_id}"
                raise ValueError(msg)

            if user.identity.identifier in identifiers:
                msg = (
                    "identity migration assigns the same identifier "
                    f"{user.identity.identifier!r} to multiple users"
                )
                raise ValueError(msg)

            ids.add(user.user_id)
            identifiers.add(user.identity.identifier)

        return self


class UserIdentityChange(BaseModel):
    """Строка users до и после миграции; before отсутствует только при импорте."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    before: StoredUser | None
    after: StoredUser

    @model_validator(mode="after")
    def _same_user(self) -> UserIdentityChange:
        SignInMetadata(identity=self.after.identity).identity_for(self.after.identifier)
        if self.before is None:
            return self

        if self.before.id != self.after.id:
            msg = "identity migration must preserve the users UUID"
            raise ValueError(msg)

        if self.before.created_at != self.after.created_at:
            msg = "identity migration must preserve the users creation time"
            raise ValueError(msg)

        if self.after.roles != self.before.roles:
            msg = "identity migration must preserve role membership"
            raise ValueError(msg)

        return self


class UserIdentitySnapshot(BaseModel):
    """Проверяемый план миграции с исходными строками для атомарного отката."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_name: str = Field(min_length=1)
    changes: tuple[UserIdentityChange, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique_users(self) -> UserIdentitySnapshot:
        ids = {change.after.id for change in self.changes}
        identifiers = {change.after.identifier for change in self.changes}
        if len(ids) != len(self.changes):
            msg = "identity snapshot repeats a users UUID"
            raise ValueError(msg)

        if len(identifiers) != len(self.changes):
            msg = "identity snapshot assigns one identifier to multiple users"
            raise ValueError(msg)

        return self


class UserRows(Protocol):
    """Строки users целиком: чтение по логину и id, upsert."""

    @abstractmethod
    async def stored(self, identifier: Login) -> StoredUser | None:
        """None — строки с таким логином нет."""

    @abstractmethod
    async def stored_by_id(self, user_id: UUID) -> StoredUser | None:
        """None — строки с таким id нет."""

    @abstractmethod
    async def upsert(
        self,
        identifier: Login,
        identity: UserIdentity | None = None,
        roles: frozenset[str] | None = None,
    ) -> StoredUser:
        """Создаёт пользователя, сохраняя UUID и членство существующей строки."""


class Authenticator(Protocol):
    """Пользователь по подписанному токену запроса: cookie либо bearer header."""

    @abstractmethod
    async def user_of_token(self, token: str) -> AuthenticatedUser:
        """AuthenticationError — подпись, срок, identity или поколение не приняты."""


class UsersUpsert(Protocol):
    """Пользователь после входа: личность и актуальное членство."""

    @abstractmethod
    async def ensure_user(self, signed: SignedIn) -> AuthenticatedUser: ...


class RoleCatalog(Protocol):
    """Существующие роли для автоматического назначения и ручное членство."""

    async def resolve_roles(
        self, identity: UserIdentity, candidates: Iterable[str]
    ) -> frozenset[str]: ...


class PersistedUsers(Protocol):
    """Пользователь по идентификатору: UUID, личность и членство в ролях."""

    @abstractmethod
    async def get_user(self, identifier: Login) -> AuthenticatedUser | None:
        """None — вход ещё не заводил строку users."""
