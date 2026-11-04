"""Типизированные claims OAuth: общий контракт без серверных библиотек."""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from boba.identity.context import Credential
from boba.identity.principal import UserIdentity


class TokenClaimsError(Exception):
    """Токен принят, но вызывающего по нему не узнать."""


class OAuthClaims(BaseModel):
    """Общие claims. aud — конкретный ресурс, а sub — UUID записи users."""

    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    version: Literal[2]
    iss: str = Field(min_length=1)
    aud: str = Field(min_length=1)
    sub: UUID
    client_id: str = Field(min_length=1)
    scope: str
    identity: UserIdentity
    credential: Credential
    generation: str = Field(min_length=1)
    iat: int = Field(gt=0)
    exp: int = Field(gt=0)

    @model_validator(mode="after")
    def _lifetime(self) -> OAuthClaims:
        if self.iat >= self.exp:
            raise ValueError("token expiry must follow its issuance")
        return self

    @property
    def user_id(self) -> UUID:
        return self.sub

    @property
    def login(self) -> str:
        return self.identity.identifier

    @property
    def profiles(self) -> frozenset[str]:
        return frozenset(self.scope.split())


class TokenClaims(OAuthClaims):
    """Самодостаточный токен доступа; scope — единственный список профилей."""

    kind: Literal["access"]
    roles: frozenset[str]


class RefreshClaims(OAuthClaims):
    """Refresh не хранит роли: перед продлением их читают из users/user_roles."""

    kind: Literal["refresh"]
    auth_time: int = Field(gt=0)

    @model_validator(mode="after")
    def _started(self) -> RefreshClaims:
        if self.auth_time > self.iat:
            raise ValueError("authentication time must not follow issuance")
        return self
