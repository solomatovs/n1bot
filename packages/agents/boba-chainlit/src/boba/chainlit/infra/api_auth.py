"""Вход API на chainlit: пользователь входа из JWT-cookie и его строка users."""

from __future__ import annotations

from abc import abstractmethod
from collections.abc import Callable
from typing import Protocol

from boba.chainlit.infra.session import ChainlitSession
from boba.identity.api import AuthenticatedUser, Authenticator
from chainlit.user import PersistedUser, User

__all__ = ["ChainlitAuthenticator", "ChainlitUsers", "PersistedUsers"]


class PersistedUsers(Protocol):
    """Строки users слоя данных chainlit по идентификатору входа."""

    @abstractmethod
    async def get_user(self, identifier: str) -> PersistedUser | None: ...


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


class ChainlitAuthenticator(Authenticator):
    """Токен JWT chainlit -> строка users через слой данных."""

    def __init__(self, users: Callable[[], PersistedUsers]) -> None:
        self._users = users

    async def user_of_token(self, token: str) -> AuthenticatedUser | None:
        login = ChainlitSession.user_of_token(token)
        if login is None:
            return None

        persisted = await self._users().get_user(login.identifier)

        return ChainlitUsers.of(persisted)
