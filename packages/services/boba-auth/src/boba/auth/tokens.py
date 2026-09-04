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
            msg = (
                "jwt tokens: [session].auth_secret is required to sign the "
                "sign-in token, got an empty string"
            )
            raise ValueError(msg)

        if ttl_sec <= 0:
            msg = (
                "jwt tokens: [session] token ttl must be a positive number of "
                f"seconds, got {ttl_sec}"
            )
            raise ValueError(msg)

        self._secret = secret
        self._ttl_sec = ttl_sec

    def issue(self, signed: SignedIn) -> str:
        claims = SessionClaims.of_signed(signed, int(time.time()), self._ttl_sec)

        return self._encode(claims)

    def renew(self, claims: SessionClaims) -> str:
        return self._encode(claims.renewed(int(time.time()), self._ttl_sec))

    def read(self, token: str) -> SessionClaims:
        if not token:
            msg = "sign-in token read: expected a JWT string, got an empty one"
            raise TokenRejectedError(TokenRejection.MALFORMED, msg)

        raw = self._decode(token, verify_exp=True)

        return SessionClaims.parse(raw)

    def read_stale(self, token: str, grace_sec: int) -> SessionClaims:
        if not token:
            msg = "sign-in token read: expected a JWT string, got an empty one"
            raise TokenRejectedError(TokenRejection.MALFORMED, msg)

        claims = SessionClaims.parse(self._decode(token, verify_exp=False))
        now = int(time.time())
        if claims.exp + grace_sec < now:
            msg = (
                f"sign-in token of {claims.identifier!r} expired at {claims.exp} "
                f"and the grace of {grace_sec}s is over at {now}"
            )
            raise TokenRejectedError(TokenRejection.EXPIRED, msg)

        return claims

    def _encode(self, claims: SessionClaims) -> str:
        return jwt.encode(claims.render(), self._secret, algorithm=TokenAlgorithm.HS256)

    def _decode(self, token: str, verify_exp: bool) -> dict[str, Any]:
        try:
            return jwt.decode(
                token,
                self._secret,
                algorithms=[TokenAlgorithm.HS256],
                options={"verify_exp": verify_exp},
            )
        except jwt.ExpiredSignatureError as exc:
            message = f"sign-in token expired: {exc}"
            raise TokenRejectedError(TokenRejection.EXPIRED, message) from exc
        except jwt.InvalidSignatureError as exc:
            message = (
                "sign-in token is signed with a secret other than "
                f"[session].auth_secret: {exc}"
            )
            raise TokenRejectedError(TokenRejection.SIGNATURE, message) from exc
        except jwt.PyJWTError as exc:
            message = f"sign-in token is not a valid HS256 JWT: {exc}"
            raise TokenRejectedError(TokenRejection.MALFORMED, message) from exc
