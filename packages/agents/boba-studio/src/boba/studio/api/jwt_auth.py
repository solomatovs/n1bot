"""Вход API по JWT сессии: токен читает boba.auth, строка users — портом.

Metadata входа (роли, запечатанный билет) берётся из токена — это то, что выдал
вход; строка users даёт только её id.

Ошибки: своих не выпускает — негодный токен или отсутствующая строка дают None.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Protocol

from fastapi import Response

from boba.auth import JwtTokens
from boba.identity.api import (
    AuthenticatedUser,
    Authenticator,
    PersistedUsers,
    UsersUpsert,
)
from boba.identity.context import DelegatedTicket
from boba.identity.token import CookieJar, CookieSpec, TokenRejectedError

__all__ = ["JwtAuthenticator", "SessionCookie"]


class JwtUsers(PersistedUsers, UsersUpsert, Protocol):
    """Строки users приложения для входа по токену: найти либо завести."""


class JwtAuthenticator(Authenticator):
    """Токен JWT входа -> пользователь входа со строкой users."""

    def __init__(self, tokens: JwtTokens, users: Callable[[], JwtUsers]) -> None:
        self._tokens = tokens
        self._users = users

    def ticket_of_token(self, token: str) -> DelegatedTicket | None:
        """Билет SSO-входа из JWT без строки users; None — токен негоден или не SSO."""
        try:
            claims = self._tokens.read(token)
        except TokenRejectedError:
            return None

        return claims.ticket()

    async def user_of_token(self, token: str) -> AuthenticatedUser | None:
        try:
            claims = self._tokens.read(token)
        except TokenRejectedError:
            return None

        stored = await self._users().get_user(claims.identifier)
        if stored is None:
            # токен выдан другим приложением на той же основе: строка users заводится
            # здесь при первом обращении, входить заново не нужно
            stored = await self._users().ensure_user(claims.signed())

        return AuthenticatedUser(
            id=stored.id,
            identifier=stored.identifier,
            metadata=claims.metadata,
        )


class SessionCookie:
    """Cookie входа в ответе HTTP: атрибуты из CookieSpec, чанки — CookieJar."""

    def __init__(self, spec: CookieSpec) -> None:
        self._spec = spec
        self._jar = CookieJar(spec.name)

    @property
    def jar(self) -> CookieJar:
        return self._jar

    def put(self, response: Response, present: Mapping[str, str], token: str) -> None:
        """Ставит токен и снимает чанки прежнего, более длинного токена."""
        pieces = self._jar.pieces(token)
        for key, value in pieces:
            self._set(response, key, value)

        for key in self._jar.stale(present, pieces):
            self._delete(response, key)

    def token_of(self, present: Mapping[str, str]) -> str | None:
        """Токен из cookie запроса: целиком либо из чанков."""
        return self._jar.token_of(present)

    def clear(self, response: Response, present: Mapping[str, str]) -> None:
        for key in self._jar.ours(present):
            self._delete(response, key)

    def _set(self, response: Response, key: str, value: str) -> None:
        response.set_cookie(
            key=key,
            value=value,
            httponly=True,
            secure=self._spec.secure,
            samesite=self._spec.samesite,
            max_age=self._spec.ttl_sec,
            path=self._spec.path,
        )

    def _delete(self, response: Response, key: str) -> None:
        response.delete_cookie(
            key=key,
            path=self._spec.path,
            secure=self._spec.secure,
            samesite=self._spec.samesite,
        )
