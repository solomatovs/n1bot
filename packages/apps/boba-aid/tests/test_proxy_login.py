"""HTTP adapter for signed proxy headers and its issued browser session."""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest
from aid_stand import StandTokens
from fastapi import FastAPI, Request
from pydantic import SecretStr

from boba.aid.auth.cookies import CookieSpec
from boba.aid.auth.policy import SessionRenewal
from boba.aid.auth.proxy import ProxyAuth
from boba.aid.auth.session import BrowserSessions
from boba.auth import SessionService, SignIns
from boba.auth.config import (
    HeaderRolesConfig,
    LocalRolesConfig,
    ProxyAuthConfig,
    ProxyRoleProviders,
)
from boba.auth.providers.proxy import ProxySignature
from boba.identity.admission import RoleMappingConfig
from boba.identity.api import StoredUser, UserRows
from boba.identity.errors import AuthenticationError, AuthorizationError
from boba.identity.principal import IdentityRule, UserIdentity
from boba.identity.signin import ProxyRequest
from boba.stand.signin import SignInStand

pytestmark = pytest.mark.anyio

COOKIE = "access_token"
SECRET = "proxy-route-secret"
CREATED_AT = datetime(2026, 1, 1, tzinfo=UTC)


class Users(UserRows):
    """In-memory user rows for this route test."""

    def __init__(self) -> None:
        self.rows: dict[UserIdentity, StoredUser] = {}

    async def stored(self, identity: UserIdentity) -> StoredUser | None:
        return self.rows.get(identity)

    async def stored_by_id(self, user_id: UUID) -> StoredUser | None:
        for row in self.rows.values():
            if row.id == user_id:
                return row
        return None

    async def upsert(
        self,
        identity: UserIdentity,
        roles: frozenset[str] | None = None,
    ) -> StoredUser:
        previous = self.rows.get(identity)
        assigned = frozenset() if previous is None else previous.roles
        if roles is not None:
            assigned = roles
        user_id = UUID(int=100 + len(self.rows)) if previous is None else previous.id
        stored = StoredUser(
            id=user_id,
            identity=identity,
            created_at=CREATED_AT,
            roles=assigned,
        )
        self.rows[identity] = stored
        return stored


def _config() -> ProxyAuthConfig:
    return ProxyAuthConfig(
        identity=IdentityRule(),
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
    profiles = SignInStand.profiles()
    backends = SignInStand.backends(profiles=profiles)
    sign_ins = SignIns(
        password=None,
        sso=None,
        proxy=backends.proxy(config),
    )
    auth = BrowserSessions(
        sign_ins=sign_ins,
        tokens=StandTokens.tokens(),
        cookie=CookieSpec(name=COOKIE, samesite="lax", ttl_sec=60),
        sessions=SessionService(Users(), profiles),
        renewal=SessionRenewal.of(60, 3600),
    )
    return ProxyAuth(config, auth)


def _request(
    login: str,
    roles: str = "",
    client: str = "172.18.0.20",
    secret: str = SECRET,
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
    route = _route(_config())
    response = await route.auth_proxy(_request("Maksimov.MA", roles="wrt"))

    assert response.status_code == 204
    set_cookie = response.headers.get("set-cookie", "")
    token = set_cookie.split(f"{COOKIE}=", 1)[1].split(";", 1)[0]
    claims = StandTokens.tokens().read(token)
    assert claims.identifier == "maksimov.ma"
    assert claims.principal().roles == {"read", "wrt"}
    assert claims.principal().profiles == {SignInStand.PROFILE}


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
