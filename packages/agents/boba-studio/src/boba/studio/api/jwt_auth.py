"""Вход API по JWT сессии: подпись общим секретом [session].auth_secret, строка users — портом.

Metadata входа (роли, запечатанный билет) берётся из токена — это то, что выдал
вход; строка users даёт только её id.

Ошибки: своих не выпускает — негодный токен или отсутствующая строка дают None.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, ClassVar, Literal, Protocol

import jwt
from fastapi import Response

from boba.identity.api import (
    AuthenticatedUser,
    Authenticator,
    PersistedUsers,
    UsersUpsert,
)
from boba.identity.context import DelegatedTicket
from boba.identity.signin import SignedIn

__all__ = ["JwtAuthenticator", "JwtClaim", "JwtIssuer", "SameSite", "SessionCookie"]

SameSite = Literal["lax", "strict", "none"]


class JwtClaim(StrEnum):
    """Поля JWT входа: общий формат токена обоих приложений."""

    IDENTIFIER = "identifier"
    DISPLAY_NAME = "display_name"
    METADATA = "metadata"
    EXP = "exp"
    IAT = "iat"


class JwtUsers(PersistedUsers, UsersUpsert, Protocol):
    """Строки users приложения для входа по токену: найти либо завести."""


class JwtAuthenticator(Authenticator):
    """Токен JWT входа -> пользователь входа со строкой users."""

    ALGORITHM: ClassVar[str] = "HS256"

    def __init__(self, secret: str, users: Callable[[], JwtUsers]) -> None:
        if not secret:
            msg = "jwt secret is empty: [session].auth_secret is required"
            raise ValueError(msg)

        self._secret = secret
        self._users = users

    def ticket_of_token(self, token: str) -> DelegatedTicket | None:
        """Билет SSO-входа из JWT без строки users; None — токен негоден или не SSO."""
        try:
            claims = jwt.decode(token, self._secret, algorithms=[self.ALGORITHM])
        except jwt.PyJWTError:
            return None

        metadata = claims.get(JwtClaim.METADATA)
        if not isinstance(metadata, dict):
            return None

        return DelegatedTicket.of_metadata(metadata)

    async def user_of_token(self, token: str) -> AuthenticatedUser | None:
        if not token:
            return None

        try:
            claims = jwt.decode(token, self._secret, algorithms=[self.ALGORITHM])
        except jwt.PyJWTError:
            return None

        identifier = claims.get(JwtClaim.IDENTIFIER)
        if not isinstance(identifier, str) or not identifier:
            return None

        metadata = self._metadata_of(claims)
        stored = await self._users().get_user(identifier)
        if stored is None:
            # токен выдан другим приложением на той же основе: строка users заводится
            # здесь при первом обращении, входить заново не нужно
            signed = SignedIn(
                identifier=identifier, display_name=identifier, metadata=metadata
            )
            stored = await self._users().ensure_user(signed)

        return AuthenticatedUser(
            id=stored.id,
            identifier=stored.identifier,
            metadata=metadata,
        )

    @classmethod
    def _metadata_of(cls, claims: Mapping[str, Any]) -> Mapping[str, object]:
        metadata = claims.get(JwtClaim.METADATA)
        if not isinstance(metadata, Mapping):
            return {}

        return metadata


class JwtIssuer:
    """Выпуск JWT входа общего формата: identifier, display_name, metadata, exp."""

    ALGORITHM: ClassVar[str] = "HS256"

    def __init__(self, secret: str, ttl_sec: int) -> None:
        if not secret:
            msg = "jwt secret is empty"
            raise ValueError(msg)

        self._secret = secret
        self._ttl = timedelta(seconds=ttl_sec)

    def issue(self, signed: SignedIn) -> str:
        now = datetime.now(UTC)
        claims: dict[str, Any] = {
            JwtClaim.IDENTIFIER.value: signed.identifier,
            JwtClaim.DISPLAY_NAME.value: signed.display_name,
            JwtClaim.METADATA.value: dict(signed.metadata),
            JwtClaim.EXP.value: now + self._ttl,
            JwtClaim.IAT.value: now,
        }

        return jwt.encode(claims, self._secret, algorithm=self.ALGORITHM)


class SessionCookie:
    """Cookie входа общего формата: целиком либо чанками name_0..name_n по 3000."""

    CHUNK: ClassVar[int] = 3000
    PATH: ClassVar[str] = "/"

    def __init__(self, name: str, samesite: SameSite, ttl_sec: int) -> None:
        self._name = name
        self._samesite: SameSite = samesite
        self._secure = samesite == "none"
        self._ttl = ttl_sec

    def put(self, response: Response, present: Mapping[str, str], token: str) -> None:
        """Ставит токен и снимает чанки прежнего, более длинного токена."""
        stale = self._ours(present)

        if len(token) > self.CHUNK:
            pieces = range(0, len(token), self.CHUNK)
            for index, start in enumerate(pieces):
                key = f"{self._name}_{index}"
                self._set(response, key, token[start : start + self.CHUNK])
                stale.discard(key)
        else:
            self._set(response, self._name, token)
            stale.discard(self._name)

        for key in stale:
            self._delete(response, key)

    def token_of(self, present: Mapping[str, str]) -> str | None:
        """Токен из cookie запроса: целиком либо из чанков."""
        whole = present.get(self._name)
        if whole:
            return whole

        parts: list[str] = []
        index = 0
        while True:
            chunk = present.get(f"{self._name}_{index}")
            if chunk is None:
                break

            parts.append(chunk)
            index += 1

        joined = "".join(parts)
        if not joined:
            return None

        return joined

    def clear(self, response: Response, present: Mapping[str, str]) -> None:
        for key in self._ours(present):
            self._delete(response, key)

    def _ours(self, present: Mapping[str, str]) -> set[str]:
        ours: set[str] = set()
        for key in present:
            if key.startswith(self._name):
                ours.add(key)

        return ours

    def _set(self, response: Response, key: str, value: str) -> None:
        response.set_cookie(
            key=key,
            value=value,
            httponly=True,
            secure=self._secure,
            samesite=self._samesite,
            max_age=self._ttl,
            path=self.PATH,
        )

    def _delete(self, response: Response, key: str) -> None:
        response.delete_cookie(
            key=key, path=self.PATH, secure=self._secure, samesite=self._samesite
        )
