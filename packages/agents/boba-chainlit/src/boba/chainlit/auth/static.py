"""Статическая (in-memory) реализация `UserRepository`."""

from __future__ import annotations

import hmac
from typing import ClassVar

from boba.chainlit.auth.use_case import UserRepository
from boba.chainlit.models import User

__all__ = ["StaticUserRepository"]


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
        ok = hmac.compare_digest(expected, password)
        if ok and username in self._users:
            return User(username=username)
        return None
