"""HTTP refresh route for a password session and its page script."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from aid_stand import StandTokens
from fastapi import Request

from boba.aid.auth.cookies import CookieSpec
from boba.aid.auth.policy import SessionRenewal
from boba.aid.auth.refresh import PageUrls, SessionRefresh
from boba.aid.auth.session import BrowserSessions
from boba.aid.infra.session import ChainlitSessions
from boba.auth import SessionService, SignIns
from boba.auth.config import LocalAuthConfig, LocalRoleProviders, LocalRolesConfig
from boba.auth.transport.http import OwnRequest
from boba.identity.admission import RoleMappingConfig
from boba.identity.api import StoredUser, UserRows
from boba.identity.principal import IdentityRule, UserIdentity
from boba.stand.signin import SignInStand

pytestmark = pytest.mark.anyio

APP_ROOT = Path(__file__).resolve().parents[1] / "assets"
COOKIE = "access_token"
CREATED_AT = datetime(2026, 1, 1, tzinfo=UTC)


class Users(UserRows):
    """In-memory user table for the refresh scenario."""

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
        user_id = UUID(int=7) if previous is None else previous.id
        row = StoredUser(
            id=user_id,
            created_at=CREATED_AT,
            identity=identity,
            roles=assigned,
        )
        self.rows[identity] = row
        return row


def _refresh() -> SessionRefresh:
    profiles = SignInStand.profiles()
    backends = SignInStand.backends(profiles=profiles)
    config = LocalAuthConfig(
        identity=IdentityRule(realm="local-stand"),
        users={"alice": "pw"},
        roles=LocalRoleProviders(
            local=LocalRolesConfig(
                mapping=RoleMappingConfig(root={"alice": ["DEV"]})
            )
        ),
    )
    sign_ins = SignIns(
        password=backends.password([config]),
        sso={},
        proxy={},
    )
    auth = BrowserSessions(
        sign_ins=sign_ins,
        tokens=StandTokens.tokens(),
        cookie=CookieSpec(name=COOKIE, samesite="lax", ttl_sec=60),
        sessions=SessionService(Users(), profiles),
        renewal=SessionRenewal.of(60, 3600),
    )

    return SessionRefresh(
        PageUrls.of("/boba", ""), auth, ChainlitSessions(StandTokens()), APP_ROOT
    )


def _request(token: str, own_header: bool) -> Request:
    headers: list[tuple[bytes, bytes]] = [(b"cookie", f"{COOKIE}={token}".encode())]
    if own_header:
        headers.append((OwnRequest.HEADER.encode(), OwnRequest.VALUE.encode()))

    scope: dict[str, Any] = {
        "type": "http",
        "method": "POST",
        "path": PageUrls.REFRESH_PATH,
        "query_string": b"",
        "headers": headers,
        "client": ("127.0.0.1", 1234),
    }

    return Request(scope)


async def test_password_session_is_renewed_with_a_new_cookie() -> None:
    refresh = _refresh()
    session = await refresh._auth.by_password("alice", "pw")

    response = await refresh.refresh(_request(session.token, own_header=True))

    assert response.status_code == 204
    set_cookie = response.headers.get("set-cookie", "")
    assert COOKIE in set_cookie
    renewed = set_cookie.split(f"{COOKIE}=", 1)[1].split(";", 1)[0]
    claims = StandTokens.tokens().read(renewed)
    assert claims.identifier == "local-stand:alice"


async def test_refresh_without_its_own_mark_is_refused() -> None:
    refresh = _refresh()
    session = await refresh._auth.by_password("alice", "pw")

    response = await refresh.refresh(_request(session.token, own_header=False))

    assert response.status_code == 403


async def test_page_script_without_sso_has_no_button_url() -> None:
    script = _refresh().script()

    assert 'const SSO_URL = ""' in script
    assert PageUrls.REFRESH_PATH in script
    assert OwnRequest.HEADER in script
