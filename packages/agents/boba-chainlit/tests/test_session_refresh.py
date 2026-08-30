"""POST /auth/refresh для входа по паролю: перевыпуск JWT без нового входа, cookie в
ответ и отказ без метки своего запроса."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any
from uuid import UUID

import jwt
import pytest
from chainlit_stand import SESSIONS, StandTokens
from fastapi import Request

from boba.auth import AuthService, JwtTokens
from boba.auth.config import LocalAuthConfig
from boba.auth.signin import PasswordSignIns
from boba.chainlit.auth.refresh import PageUrls, SessionRefresh
from boba.identity.admission import RoleMappingConfig
from boba.identity.api import AuthenticatedUser, PersistedUsers, UsersUpsert
from boba.identity.signin import SignedIn, SignInMetadata
from boba.identity.sso import OwnRequest
from boba.identity.token import CookieSpec, SessionRenewal

pytestmark = pytest.mark.anyio

APP_ROOT = Path(os.environ["BOBA_BASE"]) / "app_root"
COOKIE = "access_token"


class Users(PersistedUsers, UsersUpsert):
    """Строки users стенда: один известный пользователь."""

    def __init__(self) -> None:
        self.row = AuthenticatedUser(
            id=UUID(int=7),
            identifier="alice",
            sign_in=SignInMetadata(roles=frozenset({"DEV"})),
        )

    async def get_user(self, identifier: str) -> AuthenticatedUser | None:
        if identifier != self.row.identifier:
            return None

        return self.row

    async def ensure_user(self, signed: SignedIn) -> AuthenticatedUser:
        return self.row


@pytest.fixture(autouse=True)
def chainlit_context() -> None:
    """Маршрут зовётся напрямую: сессия chainlit не нужна."""


def _refresh() -> SessionRefresh:
    secret = StandTokens.secret()
    config = LocalAuthConfig(
        users={"alice": "pw"}, roles=RoleMappingConfig(root={"alice": ["DEV"]})
    )
    auth = AuthService(
        tokens=JwtTokens(secret, 60),
        cookie=CookieSpec(name=COOKIE, samesite="lax", ttl_sec=60),
        password=PasswordSignIns.of([config]),
        sso=None,
        users=Users(),
        renewal=SessionRenewal.of(60, 3600),
    )

    return SessionRefresh(PageUrls.of("/boba", ""), auth, SESSIONS, APP_ROOT)


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
    claims = jwt.decode(renewed, StandTokens.secret(), algorithms=["HS256"])
    assert claims["identifier"] == "alice"


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
