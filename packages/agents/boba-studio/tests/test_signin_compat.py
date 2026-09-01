"""Токен входа общего формата: studio принимает токен, собранный по claims чата."""

from __future__ import annotations

import time
from uuid import uuid4

import jwt
import pytest

from boba.auth import AuthService, JwtTokens
from boba.identity.api import AuthenticatedUser, PersistedUsers, UsersUpsert
from boba.identity.signin import SignedIn, SignInMetadata
from boba.identity.token import CookieSpec, SessionRenewal
from boba.runtime.config import StudioRuntimeConfig

pytestmark = pytest.mark.anyio


class OneUser(PersistedUsers, UsersUpsert):
    """Строка users стенда: одна на любой identifier."""

    def __init__(self) -> None:
        self._id = uuid4()

    async def get_user(self, identifier: str) -> AuthenticatedUser | None:
        return AuthenticatedUser(
            id=self._id, identifier=identifier, sign_in=SignInMetadata()
        )

    async def ensure_user(self, signed: SignedIn) -> AuthenticatedUser:
        return AuthenticatedUser(
            id=self._id, identifier=signed.identifier, sign_in=signed.sign_in
        )


def _peer_token(secret: str) -> str:
    """Токен, как его выпускает соседнее приложение: identifier, display_name,
    metadata, exp."""
    claims = {
        "identifier": "alice",
        "display_name": "Alice",
        "metadata": {"roles": ["ADM"]},
        "exp": int(time.time()) + 60,
    }
    return jwt.encode(claims, secret, algorithm="HS256")


def _service(secret: str) -> AuthService:
    return AuthService(
        tokens=JwtTokens(secret, 60),
        cookie=CookieSpec(name="access_token", samesite="lax", ttl_sec=60),
        password=None,
        sso=None,
        users=OneUser(),
        renewal=SessionRenewal.of(60, 60 * 24),
    )


async def test_studio_accepts_a_peer_token(studio_config: StudioRuntimeConfig) -> None:
    secret = studio_config.session.auth_secret
    user = await _service(secret).user_of_token(_peer_token(secret))

    assert user is not None
    assert user.identifier == "alice"
    assert user.roles == frozenset({"ADM"})


def test_issued_token_carries_the_peer_claims(
    studio_config: StudioRuntimeConfig,
) -> None:
    """Выпущенный studio токен читается теми же claims, что и токен чата."""
    secret = studio_config.session.auth_secret
    token = JwtTokens(secret, 60).issue(
        SignedIn(
            identifier="alice",
            display_name="Alice",
            sign_in=SignInMetadata(roles=frozenset({"DEV"})),
        )
    )
    claims = jwt.decode(token, secret, algorithms=["HS256"])

    assert claims["identifier"] == "alice"
    assert claims["display_name"] == "Alice"
    assert claims["metadata"] == {"roles": ["DEV"]}
