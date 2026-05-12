"""Auth: доменный User + порт UserRepository + use case AuthenticateUser."""

from __future__ import annotations

import hmac
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import ClassVar

__all__ = [
    "AuthenticateUser",
    "StaticUserRepository",
    "User",
    "UserRepository",
]


@dataclass(frozen=True)
class User:
    """Доменный пользователь приложения."""

    username: str


class UserRepository(ABC):
    """Источник credentials. Реализация решает где они хранятся."""

    @abstractmethod
    def verify(self, username: str, password: str) -> User | None:
        """User если credentials совпали; None если нет."""
        ...


class StaticUserRepository(UserRepository):
    """In-memory таблица username -> password из конфига."""

    _DUMMY: ClassVar[str] = "x" * 32

    def __init__(self, users: dict[str, str]) -> None:
        if not users:
            msg = "StaticUserRepository: empty users mapping"
            raise ValueError(msg)
        self._users = dict(users)

    def verify(self, username: str, password: str) -> User | None:
        expected = self._users.get(username, self._DUMMY)
        # Сравниваем всегда — даже если user'а нет, чтобы время ответа не
        # зависело от наличия пользователя (защита от timing-атак).
        ok = hmac.compare_digest(expected, password)
        if ok and username in self._users:
            return User(username=username)
        return None


class AuthenticateUser:
    """Use case: проверить credentials и вернуть User или None."""

    def __init__(self, repository: UserRepository) -> None:
        self._repository = repository

    def execute(self, username: str, password: str) -> User | None:
        return self._repository.verify(username, password)
