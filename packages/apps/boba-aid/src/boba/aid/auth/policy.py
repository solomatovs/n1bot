"""Сигнал браузеру о продлении и grace для опоздавшей вкладки."""

from __future__ import annotations

from enum import StrEnum
from typing import ClassVar

from pydantic import ConfigDict, Field

from boba.aid.auth.claims import SessionClaims
from boba.identity.lifetime import SessionLifetime


class RenewVerdict(StrEnum):
    """Можно ли перевыпустить токен без нового входа."""

    RENEWABLE = "renewable"
    EXPIRED = "expired"
    EXHAUSTED = "exhausted"


class SessionRenewal(SessionLifetime):
    """Правило продления сессии: срок токена, потолок сессии, порог сигнала и grace.

    Сигнал уходит, когда до конца токена меньше refresh_below_sec; перевыпуск
    принимает токен, чей exp прошёл не дольше grace_sec назад, и только пока сессия
    моложе max_sec от первого входа.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    REFRESH_BELOW_SEC: ClassVar[int] = 300
    GRACE_SEC: ClassVar[int] = 300

    refresh_below_sec: int = Field(gt=0)
    grace_sec: int = Field(ge=0)

    @classmethod
    def of(cls, ttl_sec: int, max_sec: int) -> SessionRenewal:
        return cls(
            ttl_sec=ttl_sec,
            max_sec=max_sec,
            refresh_below_sec=cls.REFRESH_BELOW_SEC,
            grace_sec=cls.GRACE_SEC,
        )

    def should_refresh(self, claims: SessionClaims, now: int) -> bool:
        return claims.exp - now < self.refresh_below_sec

    def verdict(self, claims: SessionClaims, now: int) -> RenewVerdict:
        if claims.exp + self.grace_sec < now:
            return RenewVerdict.EXPIRED

        if now >= self.deadline(claims.auth_time):
            return RenewVerdict.EXHAUSTED

        return RenewVerdict.RENEWABLE
