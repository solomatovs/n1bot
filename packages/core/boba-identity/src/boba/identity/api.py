"""Вход через API: пользователь входа, субъект под профилем, порт аутентификации.

Ошибки:
AuthenticationError — у входа нет строки users или её id не число.
"""

from __future__ import annotations

from abc import abstractmethod
from collections.abc import Iterable, Mapping
from typing import Protocol

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

__all__ = ["ApiSubject", "AuthenticatedUser", "Authenticator", "PersistedUsers"]


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


class Authenticator(Protocol):
    """Пользователь входа по токену запроса: cookie либо заголовок Authorization."""

    @abstractmethod
    async def user_of_token(self, token: str) -> AuthenticatedUser | None:
        """None — токен негоден или вход не сохранён слоем данных."""


class PersistedUsers(Protocol):
    """Строки users по идентификатору входа: id строки и сохранённые metadata."""

    @abstractmethod
    async def get_user(self, identifier: str) -> AuthenticatedUser | None:
        """None — вход ещё не заводил строку users."""
