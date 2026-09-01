"""Вход через api: пароль [auth.local] → users, JWT и cookie; выход снимает cookie."""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import UUID

import jwt
import pytest
from httpx import ASGITransport, AsyncClient

from boba.auth import AuthService, JwtTokens
from boba.auth.config import LocalAuthConfig
from boba.auth.signin import PasswordSignIns
from boba.chat.http import HttpConfig
from boba.chat.profiles import ChatProfileConfig, ChatProfiles
from boba.chat.provider import OpenAiChatConfig
from boba.identity.admission import RoleExcludeConfig, RoleMappingConfig
from boba.identity.api import (
    AuthenticatedUser,
    ChosenProfiles,
    PersistedUsers,
    UsersUpsert,
)
from boba.identity.signin import SignedIn
from boba.identity.sso import OwnRequest
from boba.identity.token import CookieSpec, SessionRenewal
from boba.ldap import Ldap3Directory
from boba.stand.refs import StandRefs
from boba.studio.api.app import ApiAccess, ApiApp
from boba.studio.api.signin import PageUrls, SignInWiring
from boba.studio.api.urls import AccountUrl, ApiVersion, SignInUrl

pytestmark = pytest.mark.anyio

SECRET = "stand-secret-of-at-least-32-bytes-long"
COOKIE = "access_token"


class Users(PersistedUsers, UsersUpsert, ChosenProfiles):
    """Строки users стенда в памяти: id выдаётся по порядку входа."""

    def __init__(self) -> None:
        self.rows: dict[str, AuthenticatedUser] = {}

    async def ensure_user(self, signed: SignedIn) -> AuthenticatedUser:
        known = self.rows.get(signed.identifier)
        if known is not None:
            return known

        row = AuthenticatedUser(
            id=UUID(int=len(self.rows) + 1),
            identifier=signed.identifier,
            sign_in=signed.sign_in,
        )
        self.rows[signed.identifier] = row
        return row

    async def set_studio_profile(self, user_id: UUID, profile: str) -> None:
        return None

    async def get_user(self, identifier: str) -> AuthenticatedUser | None:
        return self.rows.get(identifier)


def _profiles() -> ChatProfiles:
    profile = ChatProfileConfig.model_validate(
        {
            "display_name": "Stand",
            "description": "stand profile",
            "default": True,
            "roles": ["*"],
            "tools": ["echo"],
            "provider": OpenAiChatConfig(
                kind="openai",
                http=HttpConfig(),
                base_url="https://fake-llm/v1",
                api_key="k",
            ),
            "model": "fake",
            "system_prompt": "stand",
        }
    )
    return ChatProfiles({"general": profile})


def _local() -> LocalAuthConfig:
    return LocalAuthConfig(
        users={"Alice": "pw", "bob": "pw", "eve": "pw"},
        roles=RoleMappingConfig(root={"Alice": ["DEV"], "eve": ["DEV"]}),
        roles_ex=RoleExcludeConfig(root=["eve"]),
    )


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    users = Users()
    auth = AuthService(
        tokens=JwtTokens(SECRET, 3600),
        cookie=CookieSpec(name=COOKIE, samesite="lax", ttl_sec=3600),
        password=PasswordSignIns.of([_local()], Ldap3Directory()),
        sso=None,
        users=users,
        renewal=SessionRenewal.of(3600, 3600 * 24),
    )
    wiring = SignInWiring(
        auth=auth,
        sso_url="/boba-debug/api/v1/auth/sso",
        page=PageUrls(
            root="/boba-debug/workflow",
            login="/boba-debug/workflow/login",
            home="/boba-debug/workflow/observe",
        ),
    )
    access = ApiAccess(auth, COOKIE, lambda: users)
    app = ApiApp.build(StandRefs.none(), access, _profiles(), wiring)
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://api",
        headers={OwnRequest.HEADER.value: OwnRequest.VALUE.value},
    ) as c:
        yield c


async def test_providers_report_password_and_no_sso(client: AsyncClient) -> None:
    reply = await client.get(f"{ApiVersion.V1}{SignInUrl.PROVIDERS}")

    assert reply.status_code == 200
    assert reply.json() == {"password": True, "sso_url": ""}


async def test_login_sets_a_chainlit_shaped_cookie_and_opens_me(
    client: AsyncClient,
) -> None:
    reply = await client.post(
        f"{ApiVersion.V1}{SignInUrl.LOGIN}",
        json={"username": "Alice", "password": "pw"},
    )

    assert reply.status_code == 204, reply.text
    token = reply.cookies[COOKIE]
    claims = jwt.decode(token, SECRET, algorithms=["HS256"])
    assert claims["identifier"] == "alice"
    assert claims["display_name"] == "Alice"
    assert claims["metadata"] == {"provider": "LocalAuth", "roles": ["DEV"]}
    assert set(claims) == {
        "identifier",
        "display_name",
        "metadata",
        "exp",
        "iat",
        "since",
    }
    assert "HttpOnly" in reply.headers["set-cookie"]

    me = await client.get(f"{ApiVersion.V1}{AccountUrl.ME}")

    assert me.status_code == 200, me.text
    assert me.json()["login"] == "alice"
    assert me.json()["roles"] == ["DEV"]


async def test_wrong_password_is_401(client: AsyncClient) -> None:
    reply = await client.post(
        f"{ApiVersion.V1}{SignInUrl.LOGIN}",
        json={"username": "Alice", "password": "nope"},
    )

    assert reply.status_code == 401
    assert COOKIE not in reply.cookies


async def test_excluded_and_roleless_users_are_403(client: AsyncClient) -> None:
    excluded = await client.post(
        f"{ApiVersion.V1}{SignInUrl.LOGIN}",
        json={"username": "eve", "password": "pw"},
    )
    roleless = await client.post(
        f"{ApiVersion.V1}{SignInUrl.LOGIN}",
        json={"username": "bob", "password": "pw"},
    )

    assert excluded.status_code == 403
    assert roleless.status_code == 403


async def test_logout_clears_the_cookie(client: AsyncClient) -> None:
    await client.post(
        f"{ApiVersion.V1}{SignInUrl.LOGIN}",
        json={"username": "Alice", "password": "pw"},
    )

    reply = await client.post(f"{ApiVersion.V1}{SignInUrl.LOGOUT}")

    assert reply.status_code == 204
    assert 'access_token=""' in reply.headers["set-cookie"]
    assert COOKIE not in client.cookies

    me = await client.get(f"{ApiVersion.V1}{AccountUrl.ME}")
    assert me.status_code == 401


async def test_login_without_own_mark_is_refused(client: AsyncClient) -> None:
    """Чужая форма не ставит метку своего запроса: вход и выход ей закрыты."""
    reply = await client.post(
        f"{ApiVersion.V1}{SignInUrl.LOGIN}",
        json={"username": "Alice", "password": "pw"},
        headers={OwnRequest.HEADER.value: ""},
    )

    assert reply.status_code == 403, reply.text

    reply = await client.post(
        f"{ApiVersion.V1}{SignInUrl.LOGOUT}", headers={OwnRequest.HEADER.value: ""}
    )

    assert reply.status_code == 403, reply.text


async def test_refresh_renews_a_password_session(client: AsyncClient) -> None:
    """Вход по паролю продлевается перевыпуском JWT: новая cookie с поздним exp."""
    signed = await client.post(
        f"{ApiVersion.V1}{SignInUrl.LOGIN}",
        json={"username": "Alice", "password": "pw"},
    )
    assert signed.status_code == 204, signed.text
    before = jwt.decode(signed.cookies[COOKIE], SECRET, algorithms=["HS256"])

    renewed = await client.post(f"{ApiVersion.V1}{SignInUrl.REFRESH}")

    assert renewed.status_code == 204, renewed.text
    after = jwt.decode(renewed.cookies[COOKIE], SECRET, algorithms=["HS256"])
    assert after["exp"] >= before["exp"]
    assert after["since"] == before["since"]
    assert after["identifier"] == "alice"


async def test_refresh_without_a_session_or_its_mark_is_refused(
    client: AsyncClient,
) -> None:
    no_session = await client.post(f"{ApiVersion.V1}{SignInUrl.REFRESH}")
    assert no_session.status_code == 403

    signed = await client.post(
        f"{ApiVersion.V1}{SignInUrl.LOGIN}",
        json={"username": "Alice", "password": "pw"},
    )
    assert signed.status_code == 204, signed.text
    foreign = await client.post(
        f"{ApiVersion.V1}{SignInUrl.REFRESH}", headers={OwnRequest.HEADER.value: ""}
    )
    assert foreign.status_code == 403
