"""Контракт JWT браузерной сессии AID и порты его выпуска и чтения.

Ошибки:
TokenRejectedError — разобранный токен не соответствует контракту сессии.
TokenError — непредвиденный сбой разбора данных сессии.
ValidationError — прямое создание модели сессии с неверными полями.
"""

from __future__ import annotations

from abc import abstractmethod
from collections.abc import Mapping
from enum import StrEnum
from typing import Any, Literal, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from boba.identity.authentication import (
    AccessGrant,
    AuthenticatedPrincipal,
    AuthenticatedSession,
    RenewalKind,
)
from boba.identity.context import Credential, DelegatedTicket
from boba.identity.principal import UserIdentity
from boba.identity.session import Login


class TokenAlgorithm(StrEnum):
    """Алгоритм подписи JWT входа."""

    HS256 = "HS256"


class TokenRejection(StrEnum):
    """Почему токен не принят."""

    EXPIRED = "expired"
    SIGNATURE = "signature"
    MALFORMED = "malformed"
    GENERATION = "generation"
    IDENTITY = "identity"


class TokenError(Exception):
    """Выпуск или чтение токена сорвались; причина сохранена для вызывающего."""


class TokenRejectedError(TokenError):
    """Токен входа не принят; причина различима для журнала и ответа."""

    def __init__(self, reason: TokenRejection, message: str) -> None:
        super().__init__(message)
        self.reason = reason


class SessionClaims(BaseModel):
    """Версия 2 браузерного JWT: личность, права, credentials и сроки."""

    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    version: Literal[2]
    kind: Literal["session"]
    iss: str = Field(min_length=1)
    aud: str = Field(min_length=1)
    sub: UUID
    identity: UserIdentity
    access: AccessGrant
    credential: Credential
    renewal: RenewalKind
    generation: str = Field(min_length=1)
    iat: int = Field(gt=0)
    exp: int = Field(gt=0)
    auth_time: int = Field(gt=0)

    @model_validator(mode="after")
    def _ordered_times(self) -> SessionClaims:
        if not self.auth_time <= self.iat < self.exp:
            raise ValueError("session times must satisfy auth_time <= iat < exp")
        return self

    @property
    def user_id(self) -> UUID:
        return self.sub

    @property
    def identifier(self) -> Login:
        return self.identity.identifier

    @classmethod
    def parse(cls, raw: Mapping[str, Any]) -> SessionClaims:
        try:
            return cls.model_validate(raw)
        except ValidationError as exc:
            msg = f"session token does not match the version 2 claims contract: {exc}"
            raise TokenRejectedError(TokenRejection.MALFORMED, msg) from exc
        except Exception as exc:
            raise TokenError(f"Parsing session token claims failed: {exc}") from exc

    def render(self) -> dict[str, Any]:
        return self.model_dump(mode="json")

    def principal(self) -> AuthenticatedPrincipal:
        return AuthenticatedPrincipal(
            id=self.sub, identity=self.identity, access=self.access
        )

    def session(self) -> AuthenticatedSession:
        return AuthenticatedSession(
            principal=self.principal(), credential=self.credential, renewal=self.renewal
        )

    def ticket(self) -> DelegatedTicket | None:
        return self.session().ticket()


class TokenIssuer(Protocol):
    """Подписывает сессию; отказ — TokenRejectedError, прочий сбой — TokenError."""

    @abstractmethod
    def issue(
        self,
        session: AuthenticatedSession,
        issued_at: int,
        *,
        auth_time: int | None = None,
    ) -> str: ...


class TokenReader(Protocol):
    """Читает сессию; отказ — TokenRejectedError, прочий сбой — TokenError."""

    @abstractmethod
    def read(self, token: str) -> SessionClaims: ...

    @abstractmethod
    def read_stale(self, token: str, grace_sec: int) -> SessionClaims:
        """Токен с верной подписью, чей exp прошёл не дольше grace_sec назад."""
