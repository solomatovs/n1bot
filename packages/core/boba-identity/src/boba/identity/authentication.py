"""Подтверждённая личность, права и credentials сессии без wire-формата."""

from __future__ import annotations

from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from boba.identity.context import Credential, DelegatedTicket
from boba.identity.principal import UserIdentity
from boba.identity.session import Login


class AccessGrant(BaseModel):
    """Снимок членства и разрешённых профилей; выбор профиля относится к вызову."""

    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    roles: frozenset[str]
    profiles: frozenset[str]


class RenewalKind(StrEnum):
    """Как разрешено продолжить подтверждённую сессию."""

    JWT = "jwt"
    KERBEROS = "kerberos"
    REAUTHENTICATE = "reauthenticate"


class AuthenticatedPrincipal(BaseModel):
    """Кто выполняет запрос и какие права подтверждены для него."""

    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    id: UUID
    identity: UserIdentity
    access: AccessGrant

    @property
    def identifier(self) -> Login:
        return self.identity.identifier

    @property
    def roles(self) -> frozenset[str]:
        return self.access.roles

    @property
    def profiles(self) -> frozenset[str]:
        return self.access.profiles


class AuthenticatedSession(BaseModel):
    """Подтверждённый principal, credentials и способ продления сессии."""

    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    principal: AuthenticatedPrincipal
    credential: Credential
    renewal: RenewalKind

    def ticket(self) -> DelegatedTicket | None:
        if isinstance(self.credential, DelegatedTicket):
            return self.credential
        return None
