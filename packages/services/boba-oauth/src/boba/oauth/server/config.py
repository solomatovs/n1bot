"""Настройки подписания токенов сервера OAuth."""

from __future__ import annotations

from pydantic import Field

from boba.auth.policy import SessionPolicy


class TokenConfig(SessionPolicy):
    """Политика сессии и секрет подписания OAuth-токенов и кодов входа."""

    auth_secret: str = Field(
        min_length=1,
        description="Секрет подписи JWT и печати credentials.",
    )
