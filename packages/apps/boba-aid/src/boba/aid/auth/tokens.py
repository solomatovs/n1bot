"""JWT входа: подпись секретом [session].auth_secret, claims — boba.aid.auth.claims,
версия, назначение и поколение сессий — явные claims.

Ошибки:
TokenRejectedError — токен истёк, подписан другим секретом, выпущен при другом
    поколении сессий либо не разбирается.
TokenError — сбой кодека, построения сессии или другая непредвиденная ошибка.
ValueError — пустой секрет, поколение, issuer или audience при сборке.
"""

from __future__ import annotations

import time
from typing import Any

import jwt

from boba.aid.auth.claims import (
    SessionClaims,
    TokenAlgorithm,
    TokenError,
    TokenIssuer,
    TokenReader,
    TokenRejectedError,
    TokenRejection,
)
from boba.identity.authentication import AuthenticatedSession
from boba.identity.errors import AuthenticationError
from boba.identity.lifetime import SessionLifetime

__all__ = ["JwtTokens"]


class JwtTokens(TokenIssuer, TokenReader):
    """Выпуск и чтение токена входа одним секретом, сроком и поколением сессий.

    Поколение пишется в отдельный claim при выпуске и перевыпуске; токен
    другого поколения — отказ, так рестарт процесса или смена значения в
    конфиге разлогинивает всех.
    """

    def __init__(
        self,
        secret: str,
        lifetime: SessionLifetime,
        generation: str,
        *,
        issuer: str,
        audience: str,
    ) -> None:
        if not secret:
            msg = (
                "jwt tokens: [session].auth_secret is required to sign the "
                "sign-in token, got an empty string"
            )
            raise ValueError(msg)

        if not generation:
            msg = (
                "jwt tokens: a session generation is required to mark the "
                "sign-in token, got an empty string"
            )
            raise ValueError(msg)

        if not issuer:
            raise ValueError("JWT requires non-empty issuer")

        if not audience:
            raise ValueError("JWT requires non-empty audience")

        self._issuer = issuer
        self._audience = audience
        self._lifetime = lifetime
        self._secret = secret
        self._generation = generation

    @property
    def generation(self) -> str:
        return self._generation

    @property
    def ttl_sec(self) -> int:
        return self._lifetime.ttl_sec

    def issue(
        self,
        session: AuthenticatedSession,
        issued_at: int,
        *,
        auth_time: int | None = None,
    ) -> str:
        try:
            started = auth_time
            if started is None:
                started = issued_at
            try:
                expires = self._lifetime.expires_at(started, issued_at)
            except AuthenticationError as exc:
                raise TokenRejectedError(TokenRejection.EXPIRED, str(exc)) from exc
            claims = SessionClaims(
                version=2,
                kind="session",
                iss=self._issuer,
                aud=self._audience,
                sub=session.principal.id,
                identity=session.principal.identity,
                access=session.principal.access,
                credential=session.credential,
                renewal=session.renewal,
                generation=self._generation,
                iat=issued_at,
                exp=expires,
                auth_time=started,
            )
            return self._encode(claims)
        except TokenError:
            raise
        except Exception as exc:
            raise TokenError(
                f"Session token codec for issuer {self._issuer!r}: "
                f"issuing token failed: {exc}"
            ) from exc

    def read(self, token: str) -> SessionClaims:
        try:
            if not token:
                msg = "sign-in token read: expected a JWT string, got an empty one"
                raise TokenRejectedError(TokenRejection.MALFORMED, msg)

            raw = self._decode(token, verify_exp=True)

            return self._current(SessionClaims.parse(raw))
        except TokenError:
            raise
        except Exception as exc:
            raise TokenError(
                f"Session token codec for issuer {self._issuer!r}: "
                f"reading token failed: {exc}"
            ) from exc

    def read_stale(self, token: str, grace_sec: int) -> SessionClaims:
        try:
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

            return self._current(claims)
        except TokenError:
            raise
        except Exception as exc:
            raise TokenError(
                f"Session token codec for issuer {self._issuer!r}: "
                f"reading a stale token failed: {exc}"
            ) from exc

    def _current(self, claims: SessionClaims) -> SessionClaims:
        if claims.generation != self._generation:
            raise TokenRejectedError(
                TokenRejection.GENERATION,
                "the session generation has changed; sign in again",
            )
        if claims.exp > self._lifetime.deadline(claims.auth_time):
            raise TokenRejectedError(
                TokenRejection.MALFORMED, "token expiry exceeds the session lifetime"
            )
        return claims

    def _encode(self, claims: SessionClaims) -> str:
        return jwt.encode(claims.render(), self._secret, algorithm=TokenAlgorithm.HS256)

    def _decode(self, token: str, verify_exp: bool) -> dict[str, Any]:
        try:
            return jwt.decode(
                token,
                self._secret,
                algorithms=[TokenAlgorithm.HS256],
                issuer=self._issuer,
                audience=self._audience,
                options={
                    "verify_exp": verify_exp,
                    "require": ["iss", "aud", "sub", "iat", "exp"],
                },
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
