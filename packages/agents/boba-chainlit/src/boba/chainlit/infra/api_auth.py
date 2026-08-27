"""Пользователь chainlit -> пользователь входа; имя cookie входа chainlit."""

from __future__ import annotations

import os
from typing import ClassVar

from boba.chainlit.infra.session import ChainlitSession
from boba.identity.api import AuthenticatedUser
from chainlit.user import PersistedUser, User

__all__ = ["ChainlitCookie", "ChainlitUsers"]


class ChainlitUsers:
    """Пользователь chainlit -> пользователь входа; без строки users входа нет."""

    @staticmethod
    def of(user: User | PersistedUser | None) -> AuthenticatedUser | None:
        if not isinstance(user, PersistedUser):
            return None

        return AuthenticatedUser(
            id=user.id,
            identifier=user.identifier,
            metadata=ChainlitSession.metadata_of(user),
        )


class ChainlitCookie:
    """Имя cookie входа chainlit: env CHAINLIT_AUTH_COOKIE_NAME, иначе access_token."""

    NAME_ENV: ClassVar[str] = "CHAINLIT_AUTH_COOKIE_NAME"
    DEFAULT: ClassVar[str] = "access_token"

    @classmethod
    def name(cls) -> str:
        return os.environ.get(cls.NAME_ENV, cls.DEFAULT)
