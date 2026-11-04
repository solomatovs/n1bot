"""Общие ограничения срока сессии, независимо от формата токена."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

from boba.identity.errors import AuthenticationError


class SessionLifetime(BaseModel):
    """Срок одного токена и абсолютный предел от первого входа."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    ttl_sec: int = Field(gt=0)
    max_sec: int = Field(gt=0)

    @model_validator(mode="after")
    def _ordered(self) -> SessionLifetime:
        if self.max_sec < self.ttl_sec:
            raise ValueError("session max_sec must cover ttl_sec")
        return self

    def deadline(self, auth_time: int) -> int:
        return auth_time + self.max_sec

    def expires_at(self, auth_time: int, now: int) -> int:
        deadline = self.deadline(auth_time)
        if now >= deadline:
            raise AuthenticationError(
                "the session lifetime is exhausted; sign in again"
            )
        return min(now + self.ttl_sec, deadline)
