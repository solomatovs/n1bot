"""Настройки браузерной сессии AID: подпись JWT и cookie."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from boba.aid.auth.cookies import CookieSpec
from boba.aid.auth.policy import SessionRenewal
from boba.auth.policy import SessionPolicy


class SessionConfig(SessionPolicy):
    """Секция [session] приложения с браузером: к токену сессии добавлена
    cookie входа.
    """

    auth_secret: str = Field(
        min_length=1,
        description="Секрет подписи JWT и печати credentials.",
    )

    cookie: str = Field(min_length=1, description="Имя cookie входа.")
    cookie_samesite: Literal["lax", "strict", "none"] = Field(
        description="SameSite cookie входа; none включает Secure."
    )

    def cookie_spec(self) -> CookieSpec:
        return CookieSpec(
            name=self.cookie,
            samesite=self.cookie_samesite,
            ttl_sec=self.session_ttl_sec,
        )

    def renewal(self) -> SessionRenewal:
        return SessionRenewal.of(self.session_ttl_sec, self.session_max_sec)
