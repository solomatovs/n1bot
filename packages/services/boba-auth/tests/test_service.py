"""AuthService: пользователь по токену, чужой токен не заводит users, вход паролем."""

from __future__ import annotations

import time
from uuid import UUID

import jwt
import pytest

from boba.auth import AuthService, JwtTokens
from boba.auth.config import LocalAuthConfig
from boba.auth.signin import PasswordSignIns
from boba.identity.admission import RoleMappingConfig
from boba.identity.api import AuthenticatedUser, PersistedUsers, UsersUpsert
from boba.identity.errors import AuthenticationError, ExternalServiceError
from boba.identity.signin import SignedIn, SignInMetadata
from boba.identity.token import CookieSpec, SessionRenewal

pytestmark = pytest.mark.anyio

SECRET = "stand-secret"


class Users(PersistedUsers, UsersUpsert):
    """Строки users стенда в памяти: считает обращения."""

    def __init__(self) -> None:
        self.asked: list[str] = []
        self.rows = {
            "reader": AuthenticatedUser(
                id=UUID(int=7), identifier="reader", sign_in=SignInMetadata()
            )
        }

    async def get_user(self, identifier: str) -> AuthenticatedUser | None:
        self.asked.append(identifier)
        return self.rows.get(identifier)

    async def ensure_user(self, signed: SignedIn) -> AuthenticatedUser:
        created = AuthenticatedUser(
            id=UUID(int=100 + len(self.rows)),
            identifier=signed.identifier,
            sign_in=signed.sign_in,
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
        users=users,
        renewal=SessionRenewal.of(60, 60 * 24),
    )


def _token(secret: str, **claims: object) -> str:
    payload = {
        "identifier": "reader",
        "metadata": {"roles": ["read"]},
        "exp": int(time.time()) + 60,
        "iat": int(time.time()),
    }
    payload.update(claims)
    return jwt.encode(payload, secret, algorithm="HS256")


async def test_valid_token_yields_the_users_row_with_token_metadata() -> None:
    users = Users()
    user = await _service(users).user_of_token(_token(SECRET))

    assert (user.id, user.identifier) == (UUID(int=7), "reader")
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


async def test_password_sign_in_alone_touches_no_users_row() -> None:
    users = Users()
    signed = await _service(users, password=True).sign_in("alice", "pw")

    assert signed.identifier == "alice"
    assert "alice" not in users.rows


async def test_wrong_password_is_authentication_error() -> None:
    with pytest.raises(AuthenticationError):
        await _service(Users(), password=True).by_password("alice", "nope")


async def test_password_not_configured_is_external_error() -> None:
    with pytest.raises(ExternalServiceError):
        await _service(Users()).by_password("alice", "pw")


class TestRenew:
    """Перевыпуск JWT без нового входа: потолок, grace, kerberos-вход и строка users."""

    @staticmethod
    def _service(users: Users, ttl: int = 60, max_sec: int = 3600) -> AuthService:
        return AuthService(
            tokens=JwtTokens(SECRET, ttl),
            cookie=CookieSpec(name="access_token", samesite="lax", ttl_sec=ttl),
            password=None,
            sso=None,
            users=users,
            renewal=SessionRenewal.of(ttl, max_sec),
        )

    async def test_live_token_is_renewed_with_a_later_expiry(self) -> None:
        users = Users()
        service = self._service(users)
        first = _token(SECRET, identifier="reader", metadata={"roles": ["read"]})

        renewed = await service.renew(first)

        before = JwtTokens(SECRET, 60).read(first)
        after = JwtTokens(SECRET, 60).read(renewed.token)
        assert after.exp >= before.exp
        assert after.started_at() == before.started_at()
        assert renewed.user.identifier == "reader"
        assert renewed.user.roles == frozenset({"read"})

    async def test_token_within_grace_is_still_renewed(self) -> None:
        service = self._service(Users())
        stale = _token(SECRET, identifier="reader", exp=int(time.time()) - 30)

        renewed = await service.renew(stale)

        assert JwtTokens(SECRET, 60).read(renewed.token).identifier == "reader"

    async def test_token_beyond_grace_is_refused(self) -> None:
        service = self._service(Users())
        dead = _token(SECRET, identifier="reader", exp=int(time.time()) - 3600)

        with pytest.raises(AuthenticationError, match="expired"):
            await service.renew(dead)

    async def test_session_older_than_the_cap_is_refused(self) -> None:
        service = self._service(Users(), ttl=60, max_sec=120)
        old = jwt.encode(
            {
                "identifier": "reader",
                "metadata": {},
                "exp": int(time.time()) + 30,
                "iat": int(time.time()) - 30,
                "since": int(time.time()) - 600,
            },
            SECRET,
            algorithm="HS256",
        )

        with pytest.raises(AuthenticationError, match="exhausted"):
            await service.renew(old)

    async def test_kerberos_sign_in_renews_only_by_sso(self) -> None:
        service = self._service(Users())
        sso = _token(
            SECRET,
            identifier="reader",
            metadata={"provider": "KerberosAuth", "principal": "reader@X"},
        )

        with pytest.raises(AuthenticationError, match="SPNEGO"):
            await service.renew(sso)

    async def test_unknown_identifier_is_refused(self) -> None:
        service = self._service(Users())
        stranger = _token(SECRET, identifier="stranger")

        with pytest.raises(AuthenticationError, match="not persisted"):
            await service.renew(stranger)
