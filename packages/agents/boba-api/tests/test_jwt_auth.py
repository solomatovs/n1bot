"""JWT chainlit на входе api: подпись, срок, идентификатор — до строки users."""

from __future__ import annotations

import time

import jwt
import pytest

from boba.api.jwt_auth import JwtAuthenticator
from boba.identity.api import AuthenticatedUser, PersistedUsers

pytestmark = pytest.mark.anyio

SECRET = "stand-secret"


class Users(PersistedUsers):
    """Строки users стенда в памяти: считает обращения."""

    def __init__(self) -> None:
        self.asked: list[str] = []
        self.rows = {
            "reader": AuthenticatedUser(id="7", identifier="reader", metadata={})
        }

    async def get_user(self, identifier: str) -> AuthenticatedUser | None:
        self.asked.append(identifier)
        return self.rows.get(identifier)


def _token(secret: str, **claims: object) -> str:
    payload = {
        "identifier": "reader",
        "metadata": {"roles": ["read"]},
        "exp": int(time.time()) + 60,
    }
    payload.update(claims)
    return jwt.encode(payload, secret, algorithm="HS256")


async def test_valid_token_yields_the_users_row_with_token_metadata() -> None:
    users = Users()
    user = await JwtAuthenticator(SECRET, lambda: users).user_of_token(_token(SECRET))

    if user is None:
        raise AssertionError("valid token must authenticate")
    if (user.id, user.identifier) != ("7", "reader"):
        raise AssertionError(user)
    if user.roles != frozenset({"read"}):
        raise AssertionError(f"metadata must come from the token: {user.metadata}")
    if users.asked != ["reader"]:
        raise AssertionError(users.asked)


@pytest.mark.parametrize(
    "token",
    [
        "",
        "not-a-jwt",
        _token("another-secret"),
        _token(SECRET, exp=int(time.time()) - 5),
        _token(SECRET, identifier=""),
    ],
    ids=["empty", "garbage", "foreign-secret", "expired", "no-identifier"],
)
async def test_bad_tokens_are_refused_before_users(token: str) -> None:
    users = Users()
    if await JwtAuthenticator(SECRET, lambda: users).user_of_token(token) is not None:
        raise AssertionError("bad token must not authenticate")
    if users.asked:
        raise AssertionError(f"users must not be consulted: {users.asked}")


async def test_unknown_identifier_is_refused() -> None:
    users = Users()
    token = _token(SECRET, identifier="stranger")
    if await JwtAuthenticator(SECRET, lambda: users).user_of_token(token) is not None:
        raise AssertionError("a sign-in without a users row must not authenticate")


def test_empty_secret_is_a_build_error() -> None:
    with pytest.raises(ValueError, match="secret"):
        JwtAuthenticator("", Users)
