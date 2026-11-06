"""Connection на MCP: настоящий OAuth/SPNEGO, JWT, песочница и PostgreSQL.

Билет входа остаётся в JWT сервиса; клиент передаёт только имя connection.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import html
import json
import re
import secrets
import tomllib
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import httpx
import mcp_types as mt
import pytest
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from pydantic import BaseModel, RootModel

from boba.config import bind
from boba.connections.manifest import ConnectionTypes
from boba.connections.stored import GrantTarget
from boba.crypto import SecretCipher
from boba.db.postgres import AsyncPostgresPool
from boba.db.postgres.connection import PostgresConfig
from boba.identity.context import DelegatedTicket
from boba.kerberos import DelegatedAuth, KerberosPasswordAuth
from boba.krb import PasswordCredentials
from boba.krb.spnego import SpnegoNegotiate
from boba.mcp_server.auth import ServiceMark
from boba.mcp_server.config.models import McpAppConfig
from boba.mcp_server.connections.store import ConnectionStore
from boba.mcp_server.migrations import configured_migrations
from boba.oauth.claims import TokenClaims
from boba.oauth.server.tokens import ServiceTokens
from boba.stand.migrate import StandSchema
from boba.stand.service_signin import ServiceSignIn
from boba.stand.site import Stand
from boba.stand.ui.config import ServiceStand, StandApp, StandPorts
from boba.stand.ui.stand import StandProcess
from boba.stand.ui.toml_text import TomlText

pytestmark = [pytest.mark.integration, pytest.mark.anyio]


class StoredSsoConfig(ServiceStand):
    """Рабочий сервер: каталог MCP, штатные входы и только плагин PostgreSQL."""

    def write(self) -> None:
        super().write()
        document = tomllib.loads(self.files.overlay.read_text())
        document["app"]["auth"].append("${auth.kerberos}")
        kerberos = self.app.table("auth.kerberos")
        principal = Stand.required().reader_principal
        kerberos["roles"] = {"principal": {"principal": {principal: ["ADM"]}}}
        document["stand_replaced"]["auth__kerberos"] = kerberos
        document["auth"]["kerberos"] = "${stand_replaced.auth__kerberos}"
        self.files.overlay.write_text(TomlText.dumps(document))


@dataclass(frozen=True)
class StoredSsoService:
    process: StandProcess[StoredSsoConfig]
    store: ConnectionStore
    tokens: ServiceTokens
    postgres: PostgresConfig

    @property
    def public(self) -> str:
        return f"http://127.0.0.1:{self.process.config.app_port}"

    @property
    def resource(self) -> str:
        return f"{self.public}/all"

    def claims(self, token: str) -> TokenClaims:
        access = self.tokens.read(token)
        assert access is not None, "MCP must accept its own access token"
        return TokenClaims.model_validate(access.claims)

    async def grant(self) -> None:
        auth = DelegatedAuth(method="kerberos_delegated")
        profile = self.postgres.model_copy(update={"auth": auth})
        connection = await self.store.add("pg-me", profile)
        role = next(role for role in await self.store.roles() if role.name == "ADM")
        await self.store.grant(connection, GrantTarget.role(role.id))

    async def query(self, token: str) -> mt.CallToolResult:
        transport = StreamableHttpTransport(self.resource, auth=token)
        async with Client(transport) as client:
            return await client.call_tool_mcp(
                "pg_query",
                {
                    "connection": "pg-me",
                    "sql": "select current_user as who",
                    "offset": 0,
                    "limit": 1,
                },
            )


@pytest.fixture
async def service(
    tmp_path: Path, pool: AsyncPostgresPool, test_postgres: PostgresConfig
) -> AsyncIterator[StoredSsoService]:
    config = StoredSsoConfig(
        tmp_path,
        StandPorts().free(),
        test_postgres.dbname,
        True,
        db_schema=f"mcp_sso_{uuid4().hex[:12]}",
        generation="stored-sso",
        plugins=("pg",),
    )
    config.write()
    paths = config.files.paths(config.app)
    schema = StandSchema(
        paths,
        "boba.mcp_server",
        configured_migrations(),
    )
    process = StandProcess(config, tmp_path / "mcp.log")
    await schema.create()
    try:
        await asyncio.to_thread(process.start, 180.0)
        app = bind(
            StandApp.MCP.config_files().loader(paths).load(), "app", McpAppConfig
        )
        cipher = SecretCipher(app.connection_store.cipher.get_secret_value())
        store = ConnectionStore(pool, app.db_schema, cipher, ConnectionTypes.discover())
        session = app.session
        tokens = ServiceTokens(
            app.server.public_url,
            session.auth_secret,
            session.lifetime(),
            session.session_generation(),
            ServiceMark.TOKEN_SALT,
        )
        yield StoredSsoService(process, store, tokens, test_postgres)
    finally:
        await asyncio.to_thread(process.stop)
        await schema.drop()


class OAuthBrowser:
    """HTTP-вход с cookie страницы и PKCE; SPNEGO получает настоящий билет KDC."""

    REDIRECT = "http://127.0.0.1:53999/callback"

    def __init__(self, http: httpx.AsyncClient, service: StoredSsoService) -> None:
        self._http = http
        self._service = service
        self._verifier = secrets.token_urlsafe(48)
        self._stand = Stand.required()
        auth = KerberosPasswordAuth(
            method="kerberos_password",
            principal=self._stand.reader_principal,
            password=self._stand.reader_password,
        )
        self._credentials = PasswordCredentials.of(auth)

    async def login(self, *, kerberos: bool) -> tuple[str, OAuthToken]:
        client_id = await self._registered()
        page = await self._page(client_id)
        if kerberos:
            reply = await self._sso(page)
        else:
            reply = await self._password(page)
        assert reply.status_code == httpx.codes.FOUND, reply.text
        back = httpx.URL(reply.headers["location"])
        assert back.params["state"] == "stored-sso"
        token = await self._http.post(
            f"{self._service.public}/token",
            data={
                "grant_type": "authorization_code",
                "code": back.params["code"],
                "client_id": client_id,
                "redirect_uri": self.REDIRECT,
                "code_verifier": self._verifier,
                "resource": self._service.resource,
            },
        )
        assert token.status_code == httpx.codes.OK
        return client_id, OAuthToken.model_validate(token.json())

    async def refresh(self, client_id: str, token: OAuthToken) -> OAuthToken:
        assert token.refresh_token is not None
        reply = await self._http.post(
            f"{self._service.public}/token",
            data={
                "grant_type": "refresh_token",
                "client_id": client_id,
                "refresh_token": token.refresh_token,
                "resource": self._service.resource,
            },
        )
        assert reply.status_code == httpx.codes.OK
        return OAuthToken.model_validate(reply.json())

    async def _registered(self) -> str:
        reply = await self._http.post(
            f"{self._service.public}/register",
            json={
                "redirect_uris": [self.REDIRECT],
                "token_endpoint_auth_method": "none",
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
            },
        )
        assert reply.status_code == httpx.codes.CREATED
        registered = OAuthClientInformationFull.model_validate(reply.json())
        assert registered.client_id is not None
        return registered.client_id

    async def _page(self, client_id: str) -> httpx.Response:
        digest = hashlib.sha256(self._verifier.encode()).digest()
        challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
        reply = await self._http.get(
            f"{self._service.public}/authorize",
            params={
                "response_type": "code",
                "client_id": client_id,
                "redirect_uri": self.REDIRECT,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "state": "stored-sso",
                "resource": self._service.resource,
            },
        )
        assert reply.status_code == httpx.codes.FOUND
        page = await self._http.get(reply.headers["location"])
        assert page.status_code == httpx.codes.OK
        return page

    async def _sso(self, page: httpx.Response) -> httpx.Response:
        found = re.search(r'href="([^"]*/login/sso[^"]*)"', page.text)
        assert found is not None, "MCP must offer its configured Kerberos login"
        address = html.unescape(found.group(1))
        header = await asyncio.to_thread(self._negotiate)
        return await self._http.get(address, headers={"Authorization": header})

    def _negotiate(self) -> str:
        self._credentials.ensure()
        with self._credentials.applied():
            return SpnegoNegotiate.header(f"HTTP@{self._stand.krb_domain}")

    async def _password(self, page: httpx.Response) -> httpx.Response:
        found = re.search(r'name="proof" value="([^"]+)"', page.text)
        assert found is not None, "MCP login page must include its CSRF proof"
        config = self._service.process.config
        return await self._http.post(
            f"{self._service.public}/login",
            data={
                "txn": page.url.params["txn"],
                "proof": found.group(1),
                "login": config.LOCAL_LOGIN,
                "password": config.LOCAL_PASSWORD,
            },
        )


class DatabaseUser(BaseModel):
    who: str


class QueryReply:
    """Проверка результата SQL по фактической роли соединения в PostgreSQL."""

    def __init__(self) -> None:
        self._decoder = json.JSONDecoder()

    def text(self, result: mt.CallToolResult) -> str:
        parts: list[str] = []
        for block in result.content:
            if isinstance(block, mt.TextContent):
                parts.append(block.text)
        return "\n".join(parts)

    def user(self, result: mt.CallToolResult, expected: str) -> None:
        text = self.text(result)
        assert not result.is_error, text
        raw, _ = self._decoder.raw_decode(text)
        rows = RootModel[list[DatabaseUser]].model_validate(raw).root
        assert rows == [DatabaseUser(who=expected)]


async def test_sso_ticket_survives_oauth_refresh_and_mcp_restart(
    service: StoredSsoService,
) -> None:
    await service.grant()
    reply = QueryReply()
    principal = Stand.required().reader_principal
    expected = principal.split("@")[0]
    async with httpx.AsyncClient(timeout=60) as http:
        browser = OAuthBrowser(http, service)
        client_id, issued = await browser.login(kerberos=True)
        claims = service.claims(issued.access_token)
        assert isinstance(claims.credential, DelegatedTicket)
        ticket = claims.credential
        assert ticket.principal == principal
        assert ticket.sealed
        reply.user(await service.query(issued.access_token), expected)

        await asyncio.to_thread(service.process.stop)
        await asyncio.to_thread(service.process.start, 180.0)
        reply.user(await service.query(issued.access_token), expected)

        renewed = await browser.refresh(client_id, issued)
        assert service.claims(renewed.access_token).credential == ticket
        reply.user(await service.query(renewed.access_token), expected)


@pytest.mark.parametrize("provider", ["local", "proxy"])
async def test_other_sign_in_cannot_use_a_delegated_connection(
    service: StoredSsoService, provider: str
) -> None:
    await service.grant()
    async with httpx.AsyncClient(timeout=60) as http:
        browser = OAuthBrowser(http, service)
        if provider == "local":
            client_id, issued = await browser.login(kerberos=False)
            renewed = await browser.refresh(client_id, issued)
            token = renewed.access_token
        else:
            config = service.process.config
            proxy = ServiceSignIn(
                service.public,
                config.CLIENT_ID,
                config.CLIENT_SECRET,
                config.PROXY_SECRET,
            )
            token = await asyncio.to_thread(
                proxy.token, "reader", "ADM", service.resource
            )
    assert not isinstance(service.claims(token).credential, DelegatedTicket)
    result = await service.query(token)
    assert result.is_error
    assert "no delegated credentials" in QueryReply().text(result).lower()
