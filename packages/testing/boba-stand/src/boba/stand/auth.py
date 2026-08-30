"""Заглушки входа для стендов api: один известный токен, пользователи в памяти,
отказывающие источники там, где стенду часть сервисов не нужна.

Ошибки:
AuthenticationError — токен стенда не совпал или пользователя у стенда нет.
RuntimeError — стенд попросили сервис, которого у него нет.
"""

from __future__ import annotations

from typing import ClassVar
from uuid import UUID

from boba.chat.threads import ThreadOwnership
from boba.identity.api import AuthenticatedUser, Authenticator, UserSettingsStore
from boba.identity.errors import AuthenticationError
from boba.identity.session import UserMetadataField

__all__ = ["MemoryUsers", "NoThreads", "NoUsers", "StubAuthenticator"]


class StubAuthenticator(Authenticator):
    """Вход стенда: один известный токен -> заданный пользователь."""

    COOKIE: ClassVar[str] = "access_token"
    TOKEN: ClassVar[str] = "stand-token"  # noqa: S105 — токен стенда, не секрет

    def __init__(self, user: AuthenticatedUser | None) -> None:
        self._user = user

    async def user_of_token(self, token: str) -> AuthenticatedUser:
        if token != self.TOKEN:
            raise AuthenticationError("stand token mismatch")

        if self._user is None:
            raise AuthenticationError("stand has no signed-in user")

        return self._user

    @classmethod
    def cookies(cls) -> dict[str, str]:
        return {cls.COOKIE: cls.TOKEN}


class MemoryUsers(UserSettingsStore):
    """Пользователи стенда в памяти: одна строка и выбранный профиль studio."""

    def __init__(self, user: AuthenticatedUser | None) -> None:
        self._user = user
        self.chosen: dict[UUID, str] = {}

    async def get_user(self, identifier: str) -> AuthenticatedUser | None:
        if self._user is None:
            return None

        if self._user.identifier != identifier:
            return None

        return self._user

    async def set_studio_profile(self, user_id: UUID, profile: str) -> None:
        self.chosen[user_id] = profile
        if self._user is None:
            return

        metadata = {**self._user.metadata, UserMetadataField.STUDIO_PROFILE: profile}
        self._user = self._user.model_copy(update={"metadata": metadata})

    def source(self) -> UserSettingsStore:
        return self


class NoThreads:
    """Владение тредами стендам api без тредов не нужно."""

    @staticmethod
    def source() -> ThreadOwnership:
        msg = "thread ownership is not part of this stand"
        raise RuntimeError(msg)


class NoUsers:
    """Хранилище пользователей стендам api без /me не нужно."""

    @staticmethod
    def source() -> UserSettingsStore:
        msg = "users store is not part of this stand"
        raise RuntimeError(msg)
