"""JWT входа: подпись секретом [session].auth_secret, claims — boba.identity.token.

Ошибки:
TokenRejectedError — токен истёк, подписан другим секретом либо не разбирается.
ValueError — пустой секрет при сборке.
"""

from __future__ import annotations

import time
from typing import Any

import jwt

from boba.identity.signin import SignedIn
from boba.identity.token import (
    SessionClaims,
    TokenAlgorithm,
    TokenIssuer,
    TokenReader,
    TokenRejectedError,
    TokenRejection,
)

__all__ = ["JwtTokens"]


class JwtTokens(TokenIssuer, TokenReader):
    """Выпуск и чтение токена входа одним секретом и сроком."""

    def __init__(self, secret: str, ttl_sec: int) -> None:
        if not secret:
            msg = "jwt secret is empty: [session].auth_secret is required"
            raise ValueError(msg)

        if ttl_sec <= 0:
            msg = f"jwt ttl must be positive, got {ttl_sec}"
            raise ValueError(msg)

        self._secret = secret
        self._ttl_sec = ttl_sec

    def issue(self, signed: SignedIn) -> str:
        claims = SessionClaims.of_signed(signed, int(time.time()), self._ttl_sec)

        return jwt.encode(claims.render(), self._secret, algorithm=TokenAlgorithm.HS256)

    def read(self, token: str) -> SessionClaims:
        if not token:
            raise TokenRejectedError(TokenRejection.MALFORMED, "token is empty")

        raw = self._decode(token)

        return SessionClaims.parse(raw)

    def _decode(self, token: str) -> dict[str, Any]:
        try:
            return jwt.decode(token, self._secret, algorithms=[TokenAlgorithm.HS256])
        except jwt.ExpiredSignatureError as exc:
            raise TokenRejectedError(TokenRejection.EXPIRED, "token expired") from exc
        except jwt.InvalidSignatureError as exc:
            message = "token signature mismatch"
            raise TokenRejectedError(TokenRejection.SIGNATURE, message) from exc
        except jwt.PyJWTError as exc:
            message = f"token malformed: {exc}"
            raise TokenRejectedError(TokenRejection.MALFORMED, message) from exc
