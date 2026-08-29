"""Токен studio принимает chainlit и наоборот: один секрет, одни claims."""

from __future__ import annotations

import os

import pytest
from chainlit.user import User as ChainlitUser

from boba.chainlit.infra.config import AppConfig
from boba.identity.api import AuthenticatedUser, PersistedUsers, UsersUpsert
from boba.identity.signin import SignedIn
from boba.studio.api.jwt_auth import JwtAuthenticator, JwtIssuer

pytestmark = pytest.mark.anyio


@pytest.fixture(autouse=True)
def chainlit_context() -> None:
    """Токены не зависят от сессии chainlit."""


class OneUser(PersistedUsers, UsersUpsert):
    async def get_user(self, identifier: str) -> AuthenticatedUser | None:
        return AuthenticatedUser(id="5", identifier=identifier, metadata={})

    async def ensure_user(self, signed: SignedIn) -> AuthenticatedUser:
        return AuthenticatedUser(
            id="5", identifier=signed.identifier, metadata=signed.metadata
        )


def _secret(app_config: AppConfig) -> str:
    secret = app_config.session.auth_secret
    os.environ["CHAINLIT_AUTH_SECRET"] = secret
    return secret


def test_chainlit_decodes_a_studio_token(app_config: AppConfig) -> None:
    from chainlit.auth.jwt import decode_jwt

    issuer = JwtIssuer(_secret(app_config), 60)
    signed = SignedIn(
        identifier="alice", display_name="Alice", metadata={"roles": ["DEV"]}
    )

    user = decode_jwt(issuer.issue(signed))

    assert user.identifier == "alice"
    assert user.display_name == "Alice"
    assert user.metadata == {"roles": ["DEV"]}


async def test_studio_accepts_a_chainlit_token(app_config: AppConfig) -> None:
    from chainlit.auth.jwt import create_jwt

    secret = _secret(app_config)
    token = create_jwt(ChainlitUser(identifier="alice", metadata={"roles": ["ADM"]}))

    user = await JwtAuthenticator(secret, OneUser).user_of_token(token)

    assert user is not None
    assert user.identifier == "alice"
    assert user.roles == frozenset({"ADM"})
