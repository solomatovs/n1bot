"""Вход kerberos в сервис: страница входа сервера авторизации меняет билет
браузера на код авторизации, код — на токен сервиса.

Обмен SPNEGO настоящий: билет пользователя стенда выдаёт AD, секция
[auth.kerberos] берётся из конфига чата — способ входа у приложений общий.

pytest -m integration.
"""

from __future__ import annotations

import base64
import html
import os
import re
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import httpx
import krb5
import pytest
from gssapi import Credentials, Name, NameType, SecurityContext
from mcp.server.auth.provider import AuthorizationParams
from mcp.shared.auth import OAuthClientInformationFull
from omegaconf import DictConfig
from pydantic import AnyUrl
from starlette.applications import Starlette

from boba.access import ProfileGrant
from boba.auth import SignIns
from boba.auth.config import KerberosAuthConfig
from boba.auth.sso import SpnegoGate
from boba.config import bind
from boba.krb import KerberosEnv
from boba.ldap import Ldap3Directory
from boba.mcp_server.auth import (
    AuthServer,
    LoginPages,
    RegisteredClients,
    SealedValues,
    ServiceTokens,
)
from boba.mcp_server.server import EndpointCatalog
from boba.runtime.signin import SignInAssembly
from boba.stand.site import Stand, StandLayers
from boba.stand.ui.stand import StandApp

STAND = Stand.required()
SERVICE_SPN = f"HTTP/{STAND.krb_domain}@{STAND.krb_realm}"
PUBLIC = "http://127.0.0.1:8650"
ENDPOINT = "general"
CLIENT_ID = "stand-browser-client"
REDIRECT = "http://127.0.0.1:53999/callback"
SECRET = "stand-session-secret"

pytestmark = [
    pytest.mark.integration,
    pytest.mark.anyio,
    pytest.mark.failif(not STAND.live(), reason="нет keytab/krb5.conf локального AD"),
]


@pytest.fixture
def krb5_env() -> Iterator[None]:
    saved = os.environ.get(KerberosEnv.CONFIG)
    os.environ[KerberosEnv.CONFIG] = STAND.krb_config
    yield
    if saved is None:
        os.environ.pop(KerberosEnv.CONFIG, None)
        return

    os.environ[KerberosEnv.CONFIG] = saved


@pytest.fixture
def chat_config() -> DictConfig:
    """Конфиг чата со стендовым слоем: в нём живёт секция [auth.kerberos]."""
    raw = StandLayers.compose(StandApp.CHAINLIT.files())
    if not isinstance(raw, DictConfig):
        got = type(raw).__name__
        msg = f"chat config: expected to compose into a table, got {got}"
        raise TypeError(msg)

    return raw


@pytest.fixture
def server(chat_config: DictConfig, krb5_env: None) -> AuthServer:
    """Сервер авторизации сервиса с одним способом входа — kerberos."""
    config = bind(chat_config, path="auth.kerberos", model=KerberosAuthConfig)
    catalog = EndpointCatalog({ENDPOINT: ProfileGrant(tools=["*"], roles=["*"])})
    assembly = SignInAssembly(Ldap3Directory(), catalog)
    sign_ins = SignIns(None, SpnegoGate(assembly.sso(config, SECRET)), None)

    return AuthServer(
        PUBLIC,
        sign_ins,
        ServiceTokens(PUBLIC, SECRET, 300, 3600, "stand"),
        None,
        RegisteredClients(SealedValues(SECRET), {}),
        SealedValues(SECRET),
        [f"{PUBLIC}/mcp/{ENDPOINT}"],
    )


@pytest.fixture
async def browser(server: AuthServer) -> AsyncIterator[httpx.AsyncClient]:
    """Браузер у страниц входа сервиса: запросы идут в приложение напрямую."""
    app = Starlette(routes=LoginPages(server, PUBLIC).routes())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url=PUBLIC) as http:
        yield http


async def _started(server: AuthServer) -> tuple[str, OAuthClientInformationFull]:
    """Идентификатор начатого входа и клиент, который его начал."""
    client = OAuthClientInformationFull(
        client_id=CLIENT_ID,
        redirect_uris=[AnyUrl(REDIRECT)],
        token_endpoint_auth_method="none",
    )
    await server.register_client(client)
    params = AuthorizationParams(
        state="stand-state",
        scopes=None,
        code_challenge="stand-challenge",
        redirect_uri=AnyUrl(REDIRECT),
        redirect_uri_provided_explicitly=True,
        resource=f"{PUBLIC}/mcp/{ENDPOINT}",
    )
    address = httpx.URL(await server.authorize(client, params))

    return address.params[AuthServer.TXN], client


async def _sso_link(browser: httpx.AsyncClient, txn: str) -> httpx.URL:
    """Ссылка входа kerberos со страницы входа: страница открыта этим
    браузером, её cookie остаётся у него."""
    page = await browser.get("/login", params={"txn": txn})
    found = re.search(r'href="([^"]*/login/sso[^"]*)"', page.text)
    if found is None:
        raise AssertionError(f"the sign-in page links the kerberos sign-in: {page}")

    return httpx.URL(html.unescape(found.group(1)))


def _ticket(tmp_path: Path) -> str:
    """Билет браузера: TGT пользователя стенда по паролю и AP-REQ к SPN сервиса."""
    password = STAND.reader_password.get_secret_value()
    context = krb5.init_context()
    user = krb5.parse_name_flags(context, STAND.reader_principal.encode())
    options = krb5.get_init_creds_opt_alloc(context)
    tgt = krb5.get_init_creds_password(context, user, options, password.encode())

    ccache = f"FILE:{tmp_path / 'browser'}"
    cache = krb5.cc_resolve(context, ccache.encode())
    krb5.cc_initialize(context, cache, user)
    krb5.cc_store_cred(context, cache, tgt)

    creds = Credentials(usage="initiate", store={b"ccache": ccache.encode()})
    target = Name(SERVICE_SPN, NameType.kerberos_principal)
    initiator = SecurityContext(name=target, creds=creds, usage="initiate", flags=0)
    token = initiator.step()
    if token is None:
        raise AssertionError("the initiator produces a SPNEGO token")

    return base64.b64encode(token).decode()


async def test_page_offers_kerberos(
    server: AuthServer, browser: httpx.AsyncClient
) -> None:
    txn, _ = await _started(server)

    page = await browser.get("/login", params={"txn": txn})

    if page.status_code != httpx.codes.OK or "/login/sso" not in page.text:
        raise AssertionError(
            f"the sign-in page links the kerberos sign-in: {page.text}"
        )

    if "password" in page.text:
        raise AssertionError(
            f"no password form without a password provider: {page.text}"
        )


async def test_request_without_a_ticket_is_challenged(
    server: AuthServer, browser: httpx.AsyncClient
) -> None:
    txn, _ = await _started(server)

    reply = await browser.get(await _sso_link(browser, txn))

    if reply.status_code != httpx.codes.UNAUTHORIZED:
        raise AssertionError(f"a request without a ticket is 401: {reply}")

    if reply.headers.get("www-authenticate") != "Negotiate":
        raise AssertionError(f"the refusal asks for Negotiate: {reply.headers}")

    if "needs a ticket" not in reply.text or "/login/sso" not in reply.text:
        raise AssertionError(f"the refusal keeps the sign-in page: {reply.text}")


async def test_ticket_signs_in_and_issues_a_token(
    server: AuthServer, browser: httpx.AsyncClient, tmp_path: Path
) -> None:
    txn, client = await _started(server)

    reply = await browser.get(
        await _sso_link(browser, txn),
        headers={"Authorization": f"Negotiate {_ticket(tmp_path)}"},
    )
    if reply.status_code != httpx.codes.FOUND:
        raise AssertionError(f"the ticket returns to the client: {reply.text}")

    back = httpx.URL(reply.headers["location"])

    code = await server.load_authorization_code(client, back.params["code"])
    if code is None:
        raise AssertionError(f"the return carries a live authorization code: {back}")

    issued = await server.exchange_authorization_code(client, code)
    access = await server.load_access_token(issued.access_token)
    if access is None:
        raise AssertionError("the issued token is accepted by the service")

    login, _, _ = STAND.reader_principal.partition("@")
    claims = access.claims
    if claims is None or str(claims.get("login", "")).lower() != login.lower():
        raise AssertionError(f"the token names the kerberos user {login}: {claims}")

    if access.scopes != [ENDPOINT]:
        raise AssertionError(f"roles of the user open the endpoint: {access.scopes}")


async def test_ticket_on_a_bare_link_does_not_sign_in(
    server: AuthServer, browser: httpx.AsyncClient, tmp_path: Path
) -> None:
    """Ссылка /login/sso чужого начатого входа, открытая браузером домена
    мимо страницы входа: билет есть, кода нет."""
    txn, _ = await _started(server)

    reply = await browser.get(
        "/login/sso",
        params={"txn": txn},
        headers={"Authorization": f"Negotiate {_ticket(tmp_path)}"},
    )

    if reply.status_code != httpx.codes.FORBIDDEN:
        raise AssertionError(f"a step past the sign-in page is 403: {reply}")

    if "location" in reply.headers:
        raise AssertionError(f"no code leaves the service: {reply.headers}")

    if REDIRECT not in html.unescape(reply.text):
        raise AssertionError(f"the refusal shows where the code would go: {reply.text}")
