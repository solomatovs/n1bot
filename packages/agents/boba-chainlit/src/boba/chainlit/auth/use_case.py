"""Порт `UserRepository` + use case `AuthenticateUser`."""

from __future__ import annotations

from abc import ABC, abstractmethod

from boba.chainlit.models import User

__all__ = ["AuthenticateUser", "UserRepository"]


class UserRepository(ABC):
    """Источник credentials. Реализация решает где они хранятся."""

    @abstractmethod
    def verify(self, username: str, password: str) -> User | None:
        """User если credentials совпали; None если нет."""
        ...


class AuthenticateUser:
    """Use case: проверить credentials и вернуть User или None."""

    def __init__(self, repository: UserRepository) -> None:
        self._repository = repository

    def execute(self, username: str, password: str) -> User | None:
        return self._repository.verify(username, password)
