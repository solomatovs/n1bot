"""AuthService: пользователь по токену, чужой токен не заводит users, вход паролем."""

from __future__ import annotations

import time
from uuid import UUID

import jwt
import pytest

from boba.auth import AuthService, JwtTokens
from boba.auth.config import LocalAuthConfig
from boba.auth.signin import PasswordSignIns
from boba.identity.api import AuthenticatedUser, PersistedUsers, UsersUpsert
from boba.identity.errors import AuthenticationError, ExternalServiceError
from boba.identity.roles import RoleMappingConfig
from boba.identity.signin import SignedIn
from boba.identity.token import CookieSpec

pytestmark = pytest.mark.anyio

SECRET = "stand-secret"


class Users(PersistedUsers, UsersUpsert):
    """Строки users стенда в памяти: считает обращения."""

    def __init__(self) -> None:
        self.asked: list[str] = []
        self.rows = {
            "reader": AuthenticatedUser(
                id=str(UUID(int=7)), identifier="reader", metadata={}
            )
        }

    async def get_user(self, identifier: str) -> AuthenticatedUser | None:
        self.asked.append(identifier)
        return self.rows.get(identifier)

    async def ensure_user(self, signed: SignedIn) -> AuthenticatedUser:
        created = AuthenticatedUser(
            id=str(100 + len(self.rows)),
            identifier=signed.identifier,
            metadata=signed.metadata,
        )
        self.rows[signed.identifier] = created
        return created


def _service(users: Users, password: bool = False) -> AuthService:
    provider = None
    if password:
        config = LocalAuthConfig(
            users={"alice": "pw"}, roles=RoleMappingConfig(root={"alice": ["DEV"]})
        )
        provider = PasswordSignIns.of([config])

    return AuthService(
        tokens=JwtTokens(SECRET, 60),
        cookie=CookieSpec(name="access_token", samesite="lax", ttl_sec=60),
        password=provider,
        sso=None,
        users=lambda: users,
    )


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
    user = await _service(users).user_of_token(_token(SECRET))

    assert (user.id, user.identifier) == (str(UUID(int=7)), "reader")
    assert user.roles == frozenset({"read"})
    assert users.asked == ["reader"]


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
    with pytest.raises(AuthenticationError):
        await _service(users).user_of_token(token)

    assert not users.asked


async def test_unknown_identifier_is_refused_and_not_created() -> None:
    """Токен подтверждает роли, но личность не заводит: строки users нет — входа нет."""
    users = Users()
    token = _token(SECRET, identifier="stranger", metadata={"roles": ["DEV"]})

    with pytest.raises(AuthenticationError, match="stranger"):
        await _service(users).user_of_token(token)

    assert "stranger" not in users.rows


async def test_password_sign_in_issues_session_and_users_row() -> None:
    users = Users()
    session = await _service(users, password=True).by_password("alice", "pw")

    assert session.user.identifier == "alice"
    assert "alice" in users.rows
    assert (await _service(users).user_of_token(session.token)).roles == {"DEV"}


async def test_wrong_password_is_authentication_error() -> None:
    with pytest.raises(AuthenticationError):
        await _service(Users(), password=True).by_password("alice", "nope")


async def test_password_not_configured_is_external_error() -> None:
    with pytest.raises(ExternalServiceError):
        await _service(Users()).by_password("alice", "pw")
