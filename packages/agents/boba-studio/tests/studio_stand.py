"""Стенд api без базы: вход по одному токену, входы приложения — заглушки.

Имя модуля своё: conftest.py у пакетов сталкиваются в общей сессии pytest."""

from __future__ import annotations

from collections.abc import Iterable
from uuid import uuid4

from boba.chat.profiles import ChatProfiles
from boba.identity.api import AuthenticatedUser
from boba.identity.signin import SignInMetadata
from boba.runtime.config import StudioRuntimeConfig


class StandProfiles:
    """Роли и профили стенда из конфига studio."""

    @staticmethod
    def roles(config: StudioRuntimeConfig) -> list[str]:
        return sorted(config.roles)

    @staticmethod
    def profiles(config: StudioRuntimeConfig) -> ChatProfiles:
        return ChatProfiles(config.profiles)

    @classmethod
    def profile(cls, config: StudioRuntimeConfig) -> str:
        """Первый профиль, видимый ролям стенда."""
        visible = cls.profiles(config).visible_for(frozenset(cls.roles(config)))
        names = sorted(visible)
        if not names:
            raise RuntimeError("stand config has no profile visible to its roles")

        return names[0]

    @staticmethod
    def user(
        config: StudioRuntimeConfig, extra_roles: Iterable[str] = ()
    ) -> AuthenticatedUser:
        """Пользователь стенда со всеми ролями конфига."""
        roles = [*sorted(config.roles), *extra_roles]
        return AuthenticatedUser(
            id=uuid4(),
            identifier="user-1",
            sign_in=SignInMetadata(roles=frozenset(roles)),
        )
