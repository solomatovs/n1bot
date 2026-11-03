"""Вход через API: пользователь входа, строки users и порт аутентификации.

Ошибки:
AuthenticationError — у входа нет строки users.
"""

from __future__ import annotations

from abc import abstractmethod
from collections.abc import Mapping
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from boba.core.validation import ValidationText
from boba.identity.context import (
    Credential,
)
from boba.identity.errors import AuthenticationError
from boba.identity.principal import UserIdentity
from boba.identity.session import Login, UserMetadataField
from boba.identity.signin import SignedIn, SignInMetadata

__all__ = [
    "AuthenticatedUser",
    "Authenticator",
    "PersistedUsers",
    "StoredUser",
    "UserIdentityAssignment",
    "UserIdentityChange",
    "UserIdentityMigration",
    "UserIdentitySnapshot",
    "UserRows",
    "UsersColumn",
    "UsersUpsert",
]


class AuthenticatedUser(BaseModel):
    """Пользователь входа: строка users, metadata этого входа и настройки строки."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID
    identifier: Login
    sign_in: SignInMetadata
    settings: Mapping[str, object] = {}
    """Строка users как есть: metadata входа."""

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


class UsersColumn(StrEnum):
    """Колонки users."""

    ID = "id"
    IDENTIFIER = "identifier"
    CREATED_AT = "created_at"
    META = "meta"


class StoredUser(BaseModel):
    """Строка users как её хранит база: id, логин, время создания и metadata."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID
    identifier: Login
    created_at: datetime
    meta: Mapping[str, Any] = {}

    def authenticated(self) -> AuthenticatedUser:
        try:
            sign_in = SignInMetadata.parse(self.meta)
            sign_in.identity_for(self.identifier)
        except ValueError as exc:
            reason = str(exc)
            if isinstance(exc, ValidationError):
                reason = ValidationText.of(exc)

            msg = f"stored user {self.id} cannot be authenticated: {reason}"
            raise AuthenticationError(msg) from exc

        return AuthenticatedUser(
            id=self.id,
            identifier=self.identifier,
            sign_in=sign_in,
            settings=self.meta,
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
        identity = SignInMetadata.parse(self.after.meta).identity_for(
            self.after.identifier
        )
        if self.before is None:
            return self

        if self.before.id != self.after.id:
            msg = "identity migration must preserve the users UUID"
            raise ValueError(msg)

        if self.before.created_at != self.after.created_at:
            msg = "identity migration must preserve the users creation time"
            raise ValueError(msg)

        expected = dict(self.before.meta)
        expected[UserMetadataField.IDENTITY] = identity.model_dump(mode="json")
        if self.after.meta != expected:
            msg = "identity migration may change only identity metadata"
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
    async def upsert(self, identifier: Login, meta: Mapping[str, Any]) -> StoredUser:
        """Новая строка либо metadata поверх прежней."""


class Authenticator(Protocol):
    """Пользователь входа по токену запроса: cookie либо заголовок Authorization."""

    @abstractmethod
    async def user_of_token(self, token: str) -> AuthenticatedUser:
        """AuthenticationError — токен негоден или вход не сохранён слоем данных."""


class UsersUpsert(Protocol):
    """Строка users по итогу входа: создаётся или обновляет metadata."""

    @abstractmethod
    async def ensure_user(self, signed: SignedIn) -> AuthenticatedUser: ...


class PersistedUsers(Protocol):
    """Строки users по идентификатору входа: id строки и сохранённые metadata."""

    @abstractmethod
    async def get_user(self, identifier: Login) -> AuthenticatedUser | None:
        """None — вход ещё не заводил строку users."""
