"""POST /auth/proxy studio: подписанные заголовки бэкенда → cookie сессии,
дальше /me как у обычного входа; отказы — статусами middleware."""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from uuid import UUID

import jwt
import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr

from boba.auth import AuthService, JwtTokens
from boba.auth.config import (
    HeaderRolesConfig,
    LocalRolesConfig,
    ProxyAuthConfig,
    ProxyRoleProviders,
)
from boba.auth.proxy import ProxySignature
from boba.chat.http import HttpConfig
from boba.chat.profiles import ChatProfileConfig, ChatProfiles
from boba.chat.provider import OpenAiChatConfig
from boba.identity.admission import RoleMappingConfig
from boba.identity.api import (
    AuthenticatedUser,
    ChosenProfiles,
    PersistedUsers,
    UsersUpsert,
)
from boba.identity.session import Login
from boba.identity.signin import ProxyRequest, SignedIn
from boba.identity.sso import OwnRequest
from boba.identity.token import CookieSpec, SessionRenewal
from boba.stand.refs import StandRefs
from boba.stand.signin import SignInStand
from boba.studio.api.app import ApiAccess, ApiApp
from boba.studio.api.signin import PageUrls, SignInWiring
from boba.studio.api.urls import AccountUrl, ApiVersion, SignInUrl

pytestmark = pytest.mark.anyio

SECRET = "stand-secret-of-at-least-32-bytes-long"
PROXY_SECRET = "proxy-api-secret"
COOKIE = "access_token"


class Users(PersistedUsers, UsersUpsert, ChosenProfiles):
    """Строки users стенда в памяти: заводит любого вошедшего."""

    def __init__(self) -> None:
        self.rows: dict[str, AuthenticatedUser] = {}

    async def ensure_user(self, signed: SignedIn) -> AuthenticatedUser:
        created = AuthenticatedUser(
            id=UUID(int=100 + len(self.rows)),
            identifier=signed.identifier,
            sign_in=signed.sign_in,
        )
        self.rows[signed.identifier] = created

        return created

    async def set_studio_profile(self, user_id: UUID, profile: str) -> None:
        return None

    async def get_user(self, identifier: Login) -> AuthenticatedUser | None:
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


def _proxy() -> ProxyAuthConfig:
    return ProxyAuthConfig(
        secret=SecretStr(PROXY_SECRET),
        roles=ProxyRoleProviders(
            local=LocalRolesConfig(
                mapping=RoleMappingConfig(root={"maksimov.ma": ["read"]})
            ),
            header=HeaderRolesConfig(name="X-Remote-Roles"),
        ),
    )


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    users = Users()
    proxy = _proxy()
    auth = AuthService(
        tokens=JwtTokens(SECRET, 3600, "stand-generation"),
        cookie=CookieSpec(name=COOKIE, samesite="lax", ttl_sec=3600),
        password=None,
        sso=None,
        proxy=SignInStand.assembly(_profiles()).proxy(proxy),
        users=users,
        renewal=SessionRenewal.of(3600, 3600 * 24),
    )
    wiring = SignInWiring(
        auth=auth,
        sso_url="",
        proxy=proxy,
        page=PageUrls(
            root="/boba-debug/workflow",
            login="/boba-debug/workflow/login",
            home="/boba-debug/workflow/workflow",
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


def _headers(login: str, roles: str = "", secret: str = PROXY_SECRET) -> dict[str, str]:
    stamp = str(int(time.time()))
    unsigned = ProxyRequest(login=login, timestamp=stamp, roles=roles)
    headers = {
        "X-Remote-User": login,
        "X-Boba-Timestamp": stamp,
        "X-Boba-Signature": ProxySignature(secret).sign(unsigned),
    }
    if roles:
        headers["X-Remote-Roles"] = roles

    return headers


async def test_proxy_login_sets_the_cookie_and_opens_me(client: AsyncClient) -> None:
    reply = await client.post(
        f"{ApiVersion.V1}{SignInUrl.PROXY}", headers=_headers("Maksimov.MA", "wrt")
    )

    assert reply.status_code == 204, reply.text
    claims = jwt.decode(reply.cookies[COOKIE], SECRET, algorithms=["HS256"])
    assert claims["identifier"] == "maksimov.ma"
    assert claims["metadata"] == {
        "provider": "ProxyAuth",
        "roles": ["read", "wrt"],
        "profiles": ["general"],
        "generation": "stand-generation",
    }

    me = await client.get(f"{ApiVersion.V1}{AccountUrl.ME}")
    assert me.status_code == 200, me.text
    assert me.json()["login"] == "maksimov.ma"
    assert me.json()["roles"] == ["read", "wrt"]


async def test_wrong_signature_is_401(client: AsyncClient) -> None:
    reply = await client.post(
        f"{ApiVersion.V1}{SignInUrl.PROXY}",
        headers=_headers("maksimov.ma", secret="other"),
    )

    assert reply.status_code == 401, reply.text
    assert COOKIE not in reply.cookies


async def test_login_without_roles_is_403(client: AsyncClient) -> None:
    reply = await client.post(
        f"{ApiVersion.V1}{SignInUrl.PROXY}", headers=_headers("stranger")
    )

    assert reply.status_code == 403, reply.text


async def test_providers_do_not_advertise_proxy(client: AsyncClient) -> None:
    reply = await client.get(f"{ApiVersion.V1}{SignInUrl.PROVIDERS}")

    assert reply.status_code == 200
    assert reply.json() == {"password": False, "sso_url": ""}
