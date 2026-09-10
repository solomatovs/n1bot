"""POST [auth.proxy].path в chainlit: подписанные заголовки бэкенда → cookie
сессии; отказы уходят ошибками сервиса входа (их переводит middleware)."""

from __future__ import annotations

import time
from typing import Any
from uuid import UUID

import jwt
import pytest
from chainlit_stand import StandTokens
from fastapi import FastAPI, Request
from pydantic import SecretStr

from boba.auth import AuthService, JwtTokens
from boba.auth.config import (
    HeaderRolesConfig,
    LocalRolesConfig,
    ProxyAuthConfig,
    ProxyRoleProviders,
)
from boba.auth.proxy import ProxySignature
from boba.chainlit.auth.proxy import ProxyAuth
from boba.identity.admission import RoleMappingConfig
from boba.identity.api import AuthenticatedUser, PersistedUsers, UsersUpsert
from boba.identity.errors import AuthenticationError, AuthorizationError
from boba.identity.signin import ProxyRequest, SignedIn
from boba.identity.token import CookieSpec, SessionRenewal
from boba.stand.signin import SignInStand

pytestmark = pytest.mark.anyio

COOKIE = "access_token"
SECRET = "proxy-route-secret"


class Users(PersistedUsers, UsersUpsert):
    """Строки users стенда: заводит любого вошедшего."""

    def __init__(self) -> None:
        self.rows: dict[str, AuthenticatedUser] = {}

    async def get_user(self, identifier: str) -> AuthenticatedUser | None:
        return self.rows.get(identifier)

    async def ensure_user(self, signed: SignedIn) -> AuthenticatedUser:
        created = AuthenticatedUser(
            id=UUID(int=100 + len(self.rows)),
            identifier=signed.identifier,
            sign_in=signed.sign_in,
        )
        self.rows[signed.identifier] = created

        return created


@pytest.fixture(autouse=True)
def chainlit_context() -> None:
    """Маршрут зовётся напрямую: сессия chainlit не нужна."""


def _config() -> ProxyAuthConfig:
    return ProxyAuthConfig(
        secret=SecretStr(SECRET),
        allowed_clients=["172.18.0.0/24"],
        roles=ProxyRoleProviders(
            local=LocalRolesConfig(
                mapping=RoleMappingConfig(root={"maksimov.ma": ["read"]})
            ),
            header=HeaderRolesConfig(name="X-Remote-Roles"),
        ),
    )


def _route(config: ProxyAuthConfig) -> ProxyAuth:
    auth = AuthService(
        tokens=JwtTokens(StandTokens.secret(), 60, StandTokens.GENERATION),
        cookie=CookieSpec(name=COOKIE, samesite="lax", ttl_sec=60),
        password=None,
        sso=None,
        proxy=SignInStand.assembly().proxy(config),
        users=Users(),
        renewal=SessionRenewal.of(60, 3600),
    )

    return ProxyAuth(config, auth)


def _request(
    login: str, roles: str = "", client: str = "172.18.0.20", secret: str = SECRET
) -> Request:
    stamp = str(int(time.time()))
    unsigned = ProxyRequest(login=login, timestamp=stamp, roles=roles, client=client)
    signature = ProxySignature(secret).sign(unsigned)
    headers: list[tuple[bytes, bytes]] = [
        (b"x-remote-user", login.encode()),
        (b"x-boba-timestamp", stamp.encode()),
        (b"x-boba-signature", signature.encode()),
        (b"x-real-ip", client.encode()),
    ]
    if roles:
        headers.append((b"x-remote-roles", roles.encode()))

    scope: dict[str, Any] = {
        "type": "http",
        "method": "POST",
        "path": "/auth/proxy",
        "query_string": b"",
        "headers": headers,
        "client": ("127.0.0.1", 1234),
    }

    return Request(scope)


async def test_signed_headers_yield_a_session_cookie() -> None:
    response = await _route(_config()).auth_proxy(_request("Maksimov.MA", roles="wrt"))

    assert response.status_code == 204
    set_cookie = response.headers.get("set-cookie", "")
    token = set_cookie.split(f"{COOKIE}=", 1)[1].split(";", 1)[0]
    claims = jwt.decode(token, StandTokens.secret(), algorithms=["HS256"])
    assert claims["identifier"] == "maksimov.ma"
    assert claims["display_name"] == "Maksimov.MA"
    assert claims["metadata"] == {
        "provider": "ProxyAuth",
        "roles": ["read", "wrt"],
        "profiles": [SignInStand.PROFILE],
        "generation": StandTokens.GENERATION,
    }


async def test_wrong_signature_is_refused() -> None:
    with pytest.raises(AuthenticationError):
        await _route(_config()).auth_proxy(_request("maksimov.ma", secret="other"))


async def test_client_outside_networks_is_refused() -> None:
    with pytest.raises(AuthorizationError):
        await _route(_config()).auth_proxy(_request("maksimov.ma", client="10.0.0.1"))


async def test_route_is_mounted_first_on_the_configured_path() -> None:
    app = FastAPI()
    app.add_api_route("/auth/proxy", lambda: None, methods=["POST"])

    _route(_config()).install(app)

    first = app.router.routes[0]
    assert getattr(first, "path", "") == "/auth/proxy"
    assert getattr(first, "methods", set()) == {"POST"}
