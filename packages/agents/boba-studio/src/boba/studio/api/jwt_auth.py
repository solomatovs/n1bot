"""Вход API по JWT chainlit: подпись общим секретом, строка users — портом.

Metadata входа (роли, запечатанный билет) берётся из токена — это то, что выдал
вход; строка users даёт только её id.

Ошибки: своих не выпускает — негодный токен или отсутствующая строка дают None.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, ClassVar

import jwt

from boba.identity.api import AuthenticatedUser, Authenticator, PersistedUsers

__all__ = ["JwtAuthenticator"]


class JwtAuthenticator(Authenticator):
    """Токен JWT chainlit -> пользователь входа со строкой users."""

    ALGORITHM: ClassVar[str] = "HS256"
    IDENTIFIER: ClassVar[str] = "identifier"
    METADATA: ClassVar[str] = "metadata"

    def __init__(self, secret: str, users: Callable[[], PersistedUsers]) -> None:
        if not secret:
            msg = "jwt secret is empty: CHAINLIT_AUTH_SECRET is required"
            raise ValueError(msg)

        self._secret = secret
        self._users = users

    async def user_of_token(self, token: str) -> AuthenticatedUser | None:
        if not token:
            return None

        try:
            claims = jwt.decode(token, self._secret, algorithms=[self.ALGORITHM])
        except jwt.PyJWTError:
            return None

        identifier = claims.get(self.IDENTIFIER)
        if not isinstance(identifier, str) or not identifier:
            return None

        stored = await self._users().get_user(identifier)
        if stored is None:
            return None

        return AuthenticatedUser(
            id=stored.id,
            identifier=stored.identifier,
            metadata=self._metadata_of(claims),
        )

    @classmethod
    def _metadata_of(cls, claims: Mapping[str, Any]) -> Mapping[str, object]:
        metadata = claims.get(cls.METADATA)
        if not isinstance(metadata, Mapping):
            return {}

        return metadata
