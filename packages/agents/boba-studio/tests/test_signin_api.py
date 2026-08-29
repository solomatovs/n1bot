"""Вход через api: пароль [auth.local] → users, JWT и cookie; выход снимает cookie."""

from __future__ import annotations

from collections.abc import AsyncIterator

import jwt
import pytest
from httpx import ASGITransport, AsyncClient
from studio_stand import NoRefs

from boba.chat.openai import OpenAiConfig
from boba.chat.profiles import ChatProfileConfig, ChatProfiles
from boba.chat.provider import OpenAiChatConfig
from boba.identity.api import (
    AuthenticatedUser,
    PersistedUsers,
    StudioProfiles,
    UsersUpsert,
)
from boba.identity.roles import RoleExcludeConfig, RoleMappingConfig
from boba.identity.signin import SignedIn
from boba.runtime.auth_config import LocalAuthConfig
from boba.runtime.signin import PasswordSignIns
from boba.studio.api.app import ApiAccess, ApiApp
from boba.studio.api.jwt_auth import JwtAuthenticator, JwtIssuer, SessionCookie
from boba.studio.api.signin import PageUrls, SignInWiring
from boba.studio.api.urls import AccountUrl, ApiVersion, SignInUrl

pytestmark = pytest.mark.anyio

SECRET = "stand-secret-of-at-least-32-bytes-long"
COOKIE = "access_token"


class Users(PersistedUsers, UsersUpsert, StudioProfiles):
    """Строки users стенда в памяти: id выдаётся по порядку входа."""

    def __init__(self) -> None:
        self.rows: dict[str, AuthenticatedUser] = {}

    async def ensure_user(self, signed: SignedIn) -> AuthenticatedUser:
        known = self.rows.get(signed.identifier)
        if known is not None:
            return known

        row = AuthenticatedUser(
            id=str(len(self.rows) + 1),
            identifier=signed.identifier,
            metadata=dict(signed.metadata),
        )
        self.rows[signed.identifier] = row
        return row

    async def set_studio_profile(self, user_id: int, profile: str) -> None:
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
            "backend": OpenAiChatConfig(
                provider="openai",
                openai=OpenAiConfig(base_url="https://fake-llm/v1", api_key="k"),
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
    wiring = SignInWiring(
        password=PasswordSignIns.of([_local()]),
        sso=None,
        sso_url="/boba-debug/api/v1/auth/sso",
        page=PageUrls(
            root="/boba-debug/workflow",
            login="/boba-debug/workflow/login",
            home="/boba-debug/workflow/observe",
        ),
        issuer=JwtIssuer(SECRET, 3600),
        authenticator=JwtAuthenticator(SECRET, lambda: users),
        cookie=SessionCookie(COOKIE, "lax", 3600),
        users=users,
    )
    access = ApiAccess(
        JwtAuthenticator(SECRET, lambda: users),
        COOKIE,
        NoRefs.store,  # type: ignore[arg-type]
        lambda: users,
    )
    app = ApiApp.build(NoRefs.refs(), access, _profiles(), wiring)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://api"
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
    assert set(claims) == {"identifier", "display_name", "metadata", "exp", "iat"}
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
