"""SSO studio на своём URL: /v1/auth/sso и /v1/auth/sso/refresh над общим SpnegoGate.

Стенд KDC площадки: токен браузера собирает boba.stand.kerberos.
"""

from __future__ import annotations

import base64
import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from httpx import ASGITransport, AsyncClient

from boba.auth import AuthService, JwtTokens
from boba.auth.config import KerberosAuthConfig
from boba.auth.sso import SpnegoGate, SsoSignIn
from boba.chat.profiles import ChatProfiles
from boba.config import bind
from boba.identity.api import AuthenticatedUser, PersistedUsers, UsersUpsert
from boba.identity.session import UserMetadataField
from boba.identity.signin import SignedIn
from boba.identity.sso import OwnRequest
from boba.identity.token import CookieSpec, SessionRenewal
from boba.krb import KerberosEnv
from boba.runtime.config import StudioRuntimeConfig
from boba.stand.auth import NoUsers, StubAuthenticator
from boba.stand.kerberos import SsoBrowser
from boba.stand.refs import StandRefs
from boba.stand.site import Stand as Site
from boba.studio.api.app import ApiAccess, ApiApp
from boba.studio.api.signin import PageUrls, SignInWiring
from boba.studio.api.urls import ApiVersion, SignInUrl

SITE = Site.required()
KRB5_CONF = Path(SITE.krb_config)
USER_PRINCIPAL = SITE.reader_principal

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

PREFIX = "/boba"
PAGE = f"{PREFIX}/workflow"
COOKIE = "access_token"


@pytest.fixture
def krb5_env() -> Iterator[None]:
    saved = os.environ.get(KerberosEnv.CONFIG)
    os.environ[KerberosEnv.CONFIG] = str(KRB5_CONF)
    yield
    if saved is None:
        os.environ.pop(KerberosEnv.CONFIG, None)
        return

    os.environ[KerberosEnv.CONFIG] = saved


def _no_store() -> Any:
    msg = "connection store is not part of this stand"
    raise RuntimeError(msg)


class Users(PersistedUsers, UsersUpsert):
    def __init__(self) -> None:
        self.rows: dict[str, AuthenticatedUser] = {}
        self.stored_metadata: list[dict[str, object]] = []

    async def ensure_user(self, signed: SignedIn) -> AuthenticatedUser:
        self.stored_metadata.append(signed.sign_in.persistable().render())
        row = AuthenticatedUser(
            id=UUID(int=len(self.rows) + 1),
            identifier=signed.identifier,
            sign_in=signed.sign_in,
        )
        self.rows[signed.identifier] = row
        return row

    async def get_user(self, identifier: str) -> AuthenticatedUser | None:
        return self.rows.get(identifier)


class Stand:
    """Studio с SSO стенда: секрет — из [session] конфига."""

    def __init__(self, raw_config: Any, studio_config: StudioRuntimeConfig) -> None:
        config = bind(raw_config, path="auth.kerberos", model=KerberosAuthConfig)
        secret = studio_config.session.auth_secret
        self.users = Users()
        self.sign_in = SsoSignIn(config, secret)
        self.auth = AuthService(
            tokens=JwtTokens(secret, 3600),
            cookie=CookieSpec(name=COOKIE, samesite="lax", ttl_sec=3600),
            password=None,
            sso=SpnegoGate(self.sign_in),
            users=self.users,
            renewal=SessionRenewal.of(3600, 3600 * 24),
        )
        wiring = SignInWiring(
            auth=self.auth,
            sso_url=f"{PREFIX}/api{ApiVersion.V1}{SignInUrl.SSO}",
            page=PageUrls(root=PAGE, login=f"{PAGE}/login", home=f"{PAGE}/observe"),
        )
        access = ApiAccess(StubAuthenticator(None), COOKIE, NoUsers.source)
        self.app = ApiApp.build(
            StandRefs.of(_no_store, lambda: None),
            access,
            ChatProfiles(studio_config.profiles),
            wiring,
        )


@pytest.fixture
def stand(raw_config: Any, studio_config: StudioRuntimeConfig) -> Stand:
    return Stand(raw_config, studio_config)


@pytest.fixture
async def client(stand: Stand) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(
        transport=ASGITransport(app=stand.app), base_url="http://studio"
    ) as built:
        yield built


def _negotiate(token: bytes) -> dict[str, str]:
    return {"authorization": "Negotiate " + base64.b64encode(token).decode()}


def _token_of(reply: Any) -> str:
    chunks: dict[int, str] = {}
    whole = ""
    for name, value in reply.cookies.items():
        if name == COOKIE and value:
            whole = value
            continue

        if name.startswith(f"{COOKIE}_") and value:
            chunks[int(name[len(COOKIE) + 1 :])] = value

    if whole:
        return whole

    return "".join(chunks[index] for index in sorted(chunks))


async def test_without_a_token_the_browser_is_challenged(client: AsyncClient) -> None:
    reply = await client.get(f"{ApiVersion.V1}{SignInUrl.SSO}")

    assert reply.status_code == 401
    assert reply.headers["www-authenticate"] == "Negotiate"
    assert f"{PAGE}/login?error=sso_ticket" in reply.text


async def test_sign_in_sets_a_cookie_with_the_ticket_and_returns_to_next(
    client: AsyncClient, stand: Stand, tmp_path: Path, krb5_env: None
) -> None:
    reply = await client.get(
        f"{ApiVersion.V1}{SignInUrl.SSO}",
        params={"next": f"{PAGE}/build/7"},
        headers=_negotiate(SsoBrowser.token(SITE, tmp_path)),
    )

    assert reply.status_code == 303, reply.text
    assert reply.headers["location"] == f"{PAGE}/build/7"

    ticket = stand.auth.ticket_of_token(_token_of(reply))
    assert ticket is not None
    assert ticket.principal == USER_PRINCIPAL

    stored = stand.users.stored_metadata[-1]
    assert stored[UserMetadataField.PRINCIPAL] == USER_PRINCIPAL
    assert stored[UserMetadataField.ROLES]


async def test_next_outside_the_page_falls_back_to_home(
    client: AsyncClient, tmp_path: Path, krb5_env: None
) -> None:
    reply = await client.get(
        f"{ApiVersion.V1}{SignInUrl.SSO}",
        params={"next": "https://evil.test/"},
        headers=_negotiate(SsoBrowser.token(SITE, tmp_path)),
    )

    assert reply.status_code == 303
    assert reply.headers["location"] == f"{PAGE}/observe"


async def test_refresh_issues_a_fresh_ticket_for_the_session(
    client: AsyncClient, stand: Stand, tmp_path: Path, krb5_env: None
) -> None:
    signed_in = await client.get(
        f"{ApiVersion.V1}{SignInUrl.SSO}",
        headers=_negotiate(SsoBrowser.token(SITE, tmp_path)),
    )
    token = _token_of(signed_in)
    before = stand.auth.ticket_of_token(token)
    assert before is not None

    reply = await client.post(
        f"{ApiVersion.V1}{SignInUrl.REFRESH}",
        headers={
            **_negotiate(SsoBrowser.token(SITE, tmp_path)),
            OwnRequest.HEADER.value: OwnRequest.VALUE.value,
        },
        cookies={COOKIE: token},
    )

    assert reply.status_code == 204, reply.text
    after = stand.auth.ticket_of_token(_token_of(reply))
    assert after is not None
    assert after.sealed != before.sealed
    assert after.principal == USER_PRINCIPAL


async def test_refresh_without_its_own_header_or_session_is_refused(
    client: AsyncClient, tmp_path: Path, krb5_env: None
) -> None:
    foreign = await client.post(
        f"{ApiVersion.V1}{SignInUrl.REFRESH}",
        headers=_negotiate(SsoBrowser.token(SITE, tmp_path)),
    )
    no_session = await client.post(
        f"{ApiVersion.V1}{SignInUrl.REFRESH}",
        headers={
            **_negotiate(SsoBrowser.token(SITE, tmp_path)),
            OwnRequest.HEADER.value: OwnRequest.VALUE.value,
        },
    )

    assert foreign.status_code == 403
    assert no_session.status_code == 403
