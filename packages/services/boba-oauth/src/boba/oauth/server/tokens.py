"""Выпуск и проверка access/refresh сервера OAuth."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

import jwt
from fastmcp.server.auth import AccessToken
from fastmcp.server.auth.jwt_issuer import derive_jwt_key
from mcp.server.auth.provider import RefreshToken, TokenError
from mcp.shared.auth import OAuthToken
from pydantic import ValidationError

from boba.identity.authentication import AccessGrant, AuthenticatedPrincipal
from boba.identity.context import Credential
from boba.identity.errors import AuthenticationError
from boba.identity.lifetime import SessionLifetime
from boba.oauth.claims import OAuthClaims, RefreshClaims, TokenClaims

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TokenHolder:
    """Сохранённый пользователь, credentials и пределы его OAuth-сессии."""

    principal: AuthenticatedPrincipal
    credential: Credential
    started: int
    resource: str

    def scopes(self) -> list[str]:
        return sorted(self.principal.profiles)


class ServiceTokens:
    """Выпуск и проверка JWT одного сервиса с ограниченным сроком сессии."""

    def __init__(
        self,
        issuer: str,
        secret: str,
        lifetime: SessionLifetime,
        generation: str,
        salt: str,
    ) -> None:
        if not issuer or not secret or not generation or not salt:
            raise ValueError("OAuth tokens require issuer, secret, generation and salt")
        self._key = derive_jwt_key(high_entropy_material=secret, salt=salt)
        self._issuer = issuer
        self._lifetime = lifetime
        self._generation = generation

    def access(self, holder: TokenHolder, client_id: str) -> OAuthToken:
        """Вход proxy повторяет обмен утверждения и не получает refresh."""
        return self._issue(holder, client_id, refresh=False)

    def session(self, holder: TokenHolder, client_id: str) -> OAuthToken:
        return self._issue(holder, client_id, refresh=True)

    def _issue(
        self, holder: TokenHolder, client_id: str, *, refresh: bool
    ) -> OAuthToken:
        now = int(time.time())
        deadline = self._lifetime.deadline(holder.started)
        try:
            expires = self._lifetime.expires_at(holder.started, now)
        except AuthenticationError as exc:
            raise TokenError("invalid_grant", str(exc)) from exc
        common = OAuthClaims(
            version=2,
            iss=self._issuer,
            aud=holder.resource,
            sub=holder.principal.id,
            identity=holder.principal.identity,
            credential=holder.credential,
            client_id=client_id,
            scope=" ".join(holder.scopes()),
            generation=self._generation,
            iat=now,
            exp=expires,
        )
        access = TokenClaims(
            **common.model_dump(), kind="access", roles=holder.principal.roles
        )
        renewal = None
        if refresh:
            renewal = RefreshClaims(
                **common.model_copy(update={"exp": deadline}).model_dump(),
                kind="refresh",
                auth_time=holder.started,
            )
        return OAuthToken(
            access_token=self._encode(access),
            token_type="Bearer",  # noqa: S106 — вид токена
            expires_in=expires - now,
            scope=common.scope,
            refresh_token=self._encode(renewal) if renewal is not None else None,
        )

    def _encode(self, claims: OAuthClaims) -> str:
        return jwt.encode(claims.model_dump(mode="json"), self._key, algorithm="HS256")

    def _decode(self, token: str) -> dict[str, Any]:
        # aud проверяет ResourceTokens: один издатель обслуживает несколько ресурсов.
        return jwt.decode(
            token,
            self._key,
            algorithms=["HS256"],
            issuer=self._issuer,
            options={
                "verify_aud": False,
                "require": ["iss", "aud", "sub", "iat", "exp"],
            },
        )

    def read(self, token: str) -> AccessToken | None:
        try:
            claims = TokenClaims.model_validate(self._decode(token))
        except (jwt.PyJWTError, ValidationError):
            logger.info("OAuth access-token validation failed")
            return None
        if claims.generation != self._generation:
            return None
        return AccessToken(
            token=token,
            client_id=claims.client_id,
            scopes=sorted(claims.profiles),
            expires_at=claims.exp,
            resource=claims.aud,
            claims=claims.model_dump(mode="json"),
        )

    def refreshed(self, token: str) -> RefreshToken | None:
        claims = self._refresh(token)
        if claims is None:
            return None
        return RefreshToken(
            token=token,
            client_id=claims.client_id,
            scopes=sorted(claims.profiles),
            expires_at=claims.exp,
        )

    def refresh_holder(self, refresh: RefreshToken) -> TokenHolder:
        claims = self._refresh(refresh.token)
        if claims is None:
            raise TokenError(
                "invalid_grant", "the refresh token is not accepted: sign in again"
            )
        if claims.client_id != refresh.client_id:
            raise TokenError(
                "invalid_grant", "the refresh token belongs to another client"
            )
        return TokenHolder(
            principal=AuthenticatedPrincipal(
                id=claims.sub,
                identity=claims.identity,
                access=AccessGrant(roles=frozenset(), profiles=claims.profiles),
            ),
            credential=claims.credential,
            started=claims.auth_time,
            resource=claims.aud,
        )

    def _refresh(self, token: str) -> RefreshClaims | None:
        try:
            claims = RefreshClaims.model_validate(self._decode(token))
        except (jwt.PyJWTError, ValidationError):
            logger.info("OAuth refresh-token validation failed")
            return None
        if (
            claims.generation != self._generation
            or claims.exp > self._lifetime.deadline(claims.auth_time)
        ):
            return None
        return claims
