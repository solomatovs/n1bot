"""Вход через API: пользователь входа, субъект под профилем, порт аутентификации.

Ошибки:
AuthenticationError — у входа нет строки users или её id не число.
"""

from __future__ import annotations

from abc import abstractmethod
from collections.abc import Iterable, Mapping
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from boba.cancellation import RunCancellation
from boba.identity.context import (
    CallContext,
    Credential,
    DelegatedTicket,
    HumanInitiator,
    Scope,
    Subject,
)
from boba.identity.errors import AuthenticationError
from boba.identity.session import UserMetadataField
from boba.identity.signin import SignedIn

__all__ = [
    "ApiSubject",
    "AuthenticatedUser",
    "Authenticator",
    "PersistedUsers",
    "StoredUser",
    "StudioProfiles",
    "UserRows",
    "UsersColumn",
    "UserSettingsStore",
    "UsersUpsert",
]


class AuthenticatedUser(BaseModel):
    """Пользователь входа, сохранённый слоем данных: строка users и metadata входа."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    identifier: str
    metadata: Mapping[str, object] = {}

    @property
    def roles(self) -> frozenset[str]:
        return self.roles_in(self.metadata)

    @property
    def credential(self) -> Credential:
        """Делегированный билет входа либо причина его отсутствия."""
        return DelegatedTicket.credential_of(self.metadata)

    @staticmethod
    def roles_in(metadata: Mapping[str, object]) -> frozenset[str]:
        """Роли из metadata входа: строка, перечень либо ничего."""
        roles = metadata.get(UserMetadataField.ROLES)
        if not roles:
            return frozenset()

        if isinstance(roles, str):
            return frozenset({roles})

        if not isinstance(roles, Iterable):
            return frozenset()

        names: set[str] = set()
        for role in roles:
            names.add(str(role))

        return frozenset(names)


class ApiSubject(BaseModel):
    """Субъект вызова через API под выбранным профилем и его секреты."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    subject: Subject
    credential: Credential

    @classmethod
    def of(cls, user: AuthenticatedUser, profile: str) -> ApiSubject:
        try:
            subject = Subject.of_user(user.id, user.identifier, user.roles, profile)
        except ValueError as exc:
            raise AuthenticationError(str(exc)) from exc

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
            id=str(self.id), identifier=self.identifier, metadata=self.meta
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
