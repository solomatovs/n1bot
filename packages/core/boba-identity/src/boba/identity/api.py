"""Вход через API: пользователь входа, субъект под профилем, порт аутентификации.

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

from pydantic import BaseModel, ConfigDict

from boba.cancellation import RunCancellation
from boba.identity.context import (
    CallContext,
    Credential,
    HumanInitiator,
    Scope,
    Subject,
)
from boba.identity.signin import SignedIn, SignInMetadata

__all__ = [
    "ApiSubject",
    "AuthenticatedUser",
    "Authenticator",
    "PersistedUsers",
    "StoredUser",
    "StudioProfiles",
    "UserRows",
    "UserSettingsStore",
    "UsersColumn",
    "UsersUpsert",
]


class AuthenticatedUser(BaseModel):
    """Пользователь входа: строка users, metadata этого входа и настройки строки."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID
    identifier: str
    sign_in: SignInMetadata
    settings: Mapping[str, object] = {}
    """Строка users как есть: настройки LLM по профилям, выбранный профиль studio."""

    @property
    def roles(self) -> frozenset[str]:
        return self.sign_in.roles

    @property
    def credential(self) -> Credential:
        """Делегированный билет входа либо причина его отсутствия."""
        return self.sign_in.credential()


class ApiSubject(BaseModel):
    """Субъект вызова через API под выбранным профилем и его секреты."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    subject: Subject
    credential: Credential

    @classmethod
    def of(cls, user: AuthenticatedUser, profile: str) -> ApiSubject:
        subject = Subject.of_user(user.id, user.identifier, user.roles, profile)

        return cls(subject=subject, credential=user.credential)

    def context(self, scope: Scope) -> CallContext:
        """Контекст вызова человека через API в заданной области."""
        return CallContext(
            subject=self.subject,
            scope=scope,
            initiator=HumanInitiator(via="api"),
            credential=self.credential,
            cancellation=RunCancellation(),
        )


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
    identifier: str
    created_at: datetime
    meta: Mapping[str, Any] = {}

    def authenticated(self) -> AuthenticatedUser:
        return AuthenticatedUser(
            id=self.id,
            identifier=self.identifier,
            sign_in=SignInMetadata.parse(self.meta),
            settings=self.meta,
        )


class UserRows(Protocol):
    """Строки users целиком: чтение по логину и id, upsert, настройки LLM профиля."""

    @abstractmethod
    async def stored(self, identifier: str) -> StoredUser | None:
        """None — строки с таким логином нет."""

    @abstractmethod
    async def stored_by_id(self, user_id: UUID) -> StoredUser | None:
        """None — строки с таким id нет."""

    @abstractmethod
    async def upsert(self, identifier: str, meta: Mapping[str, Any]) -> StoredUser:
        """Новая строка либо metadata поверх прежней."""

    @abstractmethod
    async def set_llm_settings(
        self, user_id: UUID, profile: str, values: Mapping[str, Any]
    ) -> None:
        """Настройки LLM профиля в metadata; пустые значения снимают ключ профиля."""


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
    async def get_user(self, identifier: str) -> AuthenticatedUser | None:
        """None — вход ещё не заводил строку users."""


class StudioProfiles(Protocol):
    """Выбранный пользователем профиль studio в metadata его строки users."""

    @abstractmethod
    async def set_studio_profile(self, user_id: UUID, profile: str) -> None: ...


class UserSettingsStore(PersistedUsers, StudioProfiles, Protocol):
    """Что нужно ресурсу /me: строка пользователя и запись выбранного профиля."""
