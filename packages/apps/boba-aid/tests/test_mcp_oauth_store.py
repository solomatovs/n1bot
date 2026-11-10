"""OAuth credentials survive store recreation, stay encrypted and isolated."""

from __future__ import annotations

import asyncio
import secrets
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from uuid import UUID, uuid4

import httpx2
import pytest
from aid_stand import StandTokens
from fastapi import FastAPI
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from psycopg import sql
from pydantic import AnyHttpUrl, SecretStr

from boba.aid.mcp_servers import McpAppError
from boba.aid.mcp_servers.oauth import (
    McpBrowserOAuth,
    McpOAuthCallback,
    McpOAuthError,
)
from boba.aid.mcp_servers.oauth_store import McpOAuthStore, McpOAuthStoreError
from boba.crypto import SecretCipher, SecretCryptoError
from boba.db.postgres import AsyncPostgresPool, PostgresSchema
from boba.db.postgres.connection import PostgresConfig
from boba.mcp_client.client import (
    BrowserOAuthAuth,
    HttpEndpoint,
    HttpLocation,
    McpAuthRequired,
    McpServerConfig,
    McpServerLimits,
    McpServers,
    McpToolServer,
    SseEndpoint,
    UnattendedLogin,
)
from boba.stand.apps_slot import AppServer, AppsSlot
from boba.stand.migrate import StandMigrations
from boba.stand.service_signin import FakeServiceProcess

pytestmark = pytest.mark.anyio


@pytest.fixture(autouse=True)
def chainlit_context() -> None:
    """OAuth store tests exercise the service protocol, not Chainlit context."""


@pytest.fixture
async def oauth_schema(
    pool: AsyncPostgresPool, test_postgres: PostgresConfig
) -> AsyncIterator[str]:
    name = f"mcp_oauth_{uuid4().hex[:12]}"
    await StandMigrations().application(test_postgres, name, "boba.aid")
    try:
        yield name
    finally:
        async with pool.connection() as conn:
            await conn.execute(
                sql.SQL("drop schema if exists {} cascade").format(sql.Identifier(name))
            )


async def test_oauth_credentials_are_encrypted_and_private(
    pool: AsyncPostgresPool,
    oauth_schema: str,
) -> None:
    schema = PostgresSchema(oauth_schema)
    async with pool.connection() as conn:
        row = await conn.execute(
            sql.SQL(
                "insert into {}.mcp_servers (name, data) "
                "values (%s, '{{}}'::jsonb) returning id"
            ).format(schema.ident),
            (f"oauth-{uuid4().hex}",),
        )
        fetched = await row.fetchone()
        assert fetched is not None
        server_id = UUID(str(fetched[0]))

    cipher = SecretCipher(secrets.token_bytes(32))
    first_user = uuid4()
    second_user = uuid4()
    async with pool.connection() as conn:
        await conn.execute(
            sql.SQL(
                "insert into {}.users (id, login) values (%s, %s), (%s, %s)"
            ).format(schema.ident),
            (
                first_user,
                f"oauth-{first_user.hex}",
                second_user,
                f"oauth-{second_user.hex}",
            ),
        )
    first = McpOAuthStore(pool, oauth_schema, cipher)
    token = OAuthToken(
        access_token="access-very-secret",
        refresh_token="refresh-very-secret",
        expires_in=3600,
    )
    client = OAuthClientInformationFull(
        client_id="client-id",
        client_secret="client-very-secret",
    )
    await first.set_client_info(server_id, first_user, client)
    await first.set_tokens(server_id, first_user, token)

    async with pool.connection() as conn:
        row = await conn.execute(
            sql.SQL(
                "select token_data, client_data from {}.mcp_tokens "
                "where server_id = %s and user_id = %s"
            ).format(schema.ident),
            (server_id, first_user),
        )
        saved = await row.fetchone()
        assert saved is not None
        assert all(value.startswith(SecretCipher.PREFIX) for value in saved)
        assert all("very-secret" not in value for value in saved)

    reopened = McpOAuthStore(pool, oauth_schema, cipher)
    assert await reopened.get_tokens(server_id, first_user) == token
    assert await reopened.get_client_info(server_id, first_user) == client
    assert await reopened.get_tokens(server_id, second_user) is None

    await reopened.delete(server_id, first_user)
    assert await reopened.get_tokens(server_id, first_user) is None
    assert await reopened.get_client_info(server_id, first_user) is None


@pytest.fixture
async def oauth_user(pool: AsyncPostgresPool, oauth_schema: str) -> UUID:
    user_id = uuid4()
    schema = PostgresSchema(oauth_schema)
    async with pool.connection() as conn:
        await conn.execute(
            sql.SQL("insert into {}.users (id, login) values (%s, %s)").format(
                schema.ident
            ),
            (user_id, f"oauth-{user_id.hex}"),
        )

    return user_id


@pytest.mark.parametrize("transport", [HttpEndpoint, SseEndpoint])
async def test_disconnected_oauth_requires_sign_in_before_network_requests(
    pool: AsyncPostgresPool,
    oauth_schema: str,
    oauth_user: UUID,
    transport: type[HttpEndpoint | SseEndpoint],
) -> None:
    server_id = uuid4()
    async with pool.connection() as conn:
        await conn.execute(
            sql.SQL(
                "insert into {}.mcp_servers (id, name, data) "
                "values (%s, 'disconnected', '{{}}'::jsonb)"
            ).format(sql.Identifier(oauth_schema)),
            (server_id,),
        )

    store = McpOAuthStore(pool, oauth_schema, SecretCipher(secrets.token_bytes(32)))
    await store.set_tokens(
        server_id, oauth_user, OAuthToken(access_token="discarded", token_type="Bearer")
    )
    broker = McpBrowserOAuth(
        store,
        "http://127.0.0.1/aid/",
        client_name="Disconnected OAuth test",
    )
    await broker.disconnect(server_id, oauth_user)
    endpoint = transport(
        service=HttpLocation(scheme="http", host="127.0.0.1", port=9, path=""),
        path="/mcp",
        auth=BrowserOAuthAuth().bound(broker, server_id, oauth_user),
    )
    server = McpToolServer(
        "disconnected",
        McpServerConfig(
            endpoint=endpoint,
            limits=McpServerLimits(connect_timeout_sec=10, call_timeout_sec=30),
        ),
    )
    try:
        with pytest.raises(McpAuthRequired) as refused:
            await server.tools(McpServers.OWN)

        assert "Sign in with OAuth" in refused.value.brief
        assert "handshake failed" not in refused.value.brief
        assert await store.get_tokens(server_id, oauth_user) is None
        assert await store.get_client_info(server_id, oauth_user) is None
    finally:
        await server.close()


@pytest.mark.parametrize("trailing_slash", ["", "/"])
async def test_browser_authorization_round_trips_to_mcp(
    pool: AsyncPostgresPool,
    oauth_schema: str,
    oauth_user: UUID,
    tmp_path: Path,
    trailing_slash: str,
) -> None:
    stand = FakeServiceProcess(tmp_path, 300)
    stand.wait()
    try:
        schema = PostgresSchema(oauth_schema)
        async with pool.connection() as conn:
            row = await conn.execute(
                sql.SQL(
                    "insert into {}.mcp_servers (name, data) "
                    "values (%s, '{{}}'::jsonb) returning id"
                ).format(schema.ident),
                (f"browser-{uuid4().hex}",),
            )
            fetched = await row.fetchone()
            assert fetched is not None
            server_id = UUID(str(fetched[0]))

        store = McpOAuthStore(pool, oauth_schema, SecretCipher(secrets.token_bytes(32)))
        client_name = f"OAuth test {server_id}"
        broker = McpBrowserOAuth(
            store,
            "http://127.0.0.1/aid/",
            client_name=client_name,
        )
        assert broker.client_document().client_name == client_name
        return_url = AnyHttpUrl(
            f"http://127.0.0.1/aid/thread/{server_id}?view=mcp#tools"
        )
        resource = stand.url() + trailing_slash

        endpoint = HttpEndpoint(
            service=HttpLocation(
                scheme="http", host=stand.HOST, port=stand.port, path=""
            ),
            path=urlsplit(resource).path,
            auth=BrowserOAuthAuth(),
        )
        url = await broker.begin(
            server_id, oauth_user, stand.LOGIN, endpoint, return_url=return_url
        )
        state = parse_qs(urlsplit(url).query)["state"][0]
        with pytest.raises(McpAppError, match="matching AID sign-in"):
            broker.reject("someone-else", state)
        broker.reject(stand.LOGIN, state)
        url = await broker.begin(
            server_id, oauth_user, stand.LOGIN, endpoint, return_url=return_url
        )
        browser = UnattendedLogin(stand.LOGIN, SecretStr(stand.PASSWORD), stand.url())
        await browser.redirect(url)
        granted = await browser.callback()

        with pytest.raises(McpAppError, match="another AID user"):
            await broker.finish(
                "someone-else", granted.code, granted.state or "", granted.iss
            )
        assert await broker.finish(
            stand.LOGIN, granted.code, granted.state or "", granted.iss
        ) == str(return_url)
        assert await store.get_tokens(server_id, oauth_user) is not None
        registered = await store.get_client_info(server_id, oauth_user)
        assert registered is not None
        assert registered.client_name == client_name

        assert (
            await broker.begin(
                server_id, oauth_user, stand.LOGIN, endpoint, return_url=return_url
            )
            == ""
        )
        auth = BrowserOAuthAuth().bound(broker, server_id, oauth_user)
        endpoint = endpoint.model_copy(update={"auth": auth})
        server = McpToolServer(
            "browser-oauth",
            McpServerConfig(
                endpoint=endpoint,
                limits=McpServerLimits(connect_timeout_sec=20, call_timeout_sec=60),
            ),
        )
        try:
            assert (await server.tools(McpServers.OWN)).tools
        finally:
            await server.close()

        await broker.disconnect(server_id, oauth_user)
        assert await store.get_tokens(server_id, oauth_user) is None
        missing = endpoint.model_copy(update={"path": "/missing"})
        with pytest.raises(McpAppError, match="404") as rejected:
            await broker.begin(
                server_id, oauth_user, stand.LOGIN, missing, return_url=return_url
            )

        assert rejected.value.__cause__ is not None
    finally:
        stand.stop()


@pytest.mark.integration
@pytest.mark.parametrize("remote", [AppServer.LEGACY, AppServer.OLDER])
async def test_oauth_probe_uses_negotiation_for_older_mcp_servers(
    pool: AsyncPostgresPool,
    oauth_schema: str,
    apps_slot: AppsSlot,
    remote: AppServer,
) -> None:
    store = McpOAuthStore(pool, oauth_schema, SecretCipher(secrets.token_bytes(32)))
    broker = McpBrowserOAuth(
        store,
        "http://127.0.0.1/aid/",
        client_name="Protocol negotiation test",
    )
    endpoint = HttpEndpoint(
        service=HttpLocation(
            scheme="http", host=apps_slot.host, port=remote.value, path=""
        ),
        path=apps_slot.PATH,
        auth=BrowserOAuthAuth(),
    )
    result = await broker.begin(
        uuid4(),
        uuid4(),
        "probe",
        endpoint,
        return_url=AnyHttpUrl("http://127.0.0.1/aid/"),
    )
    assert result == ""


@pytest.fixture
async def oauth_server(pool: AsyncPostgresPool, oauth_schema: str) -> UUID:
    server_id = uuid4()
    async with pool.connection() as conn:
        await conn.execute(
            sql.SQL(
                "insert into {}.mcp_servers (id, name, data) "
                "values (%s, 'error-boundary', '{{}}'::jsonb)"
            ).format(sql.Identifier(oauth_schema)),
            (server_id,),
        )

    return server_id


@pytest.mark.parametrize("operation", ["get_tokens", "get_client_info", "delete"])
async def test_missing_oauth_table_has_storage_and_application_boundaries(
    pool: AsyncPostgresPool, operation: str
) -> None:
    store = McpOAuthStore(
        pool, f"missing_oauth_{uuid4().hex}", SecretCipher(secrets.token_bytes(32))
    )
    server_id, user_id = uuid4(), uuid4()
    with pytest.raises(McpOAuthStoreError) as failed:
        await getattr(store, operation)(server_id, user_id)

    assert failed.value.__cause__ is not None
    assert str(server_id) in str(failed.value)
    assert str(user_id) in str(failed.value)
    assert store.schema in str(failed.value)
    broker = McpBrowserOAuth(store, "http://localhost/aid/", client_name="Store error")
    with pytest.raises(McpOAuthError) as disconnect:
        await broker.disconnect(server_id, user_id)

    assert isinstance(disconnect.value.__cause__, McpOAuthStoreError)
    assert store.schema in str(disconnect.value)


@pytest.mark.parametrize("client", [False, True])
async def test_oauth_serialization_failure_stays_inside_storage(
    pool: AsyncPostgresPool, oauth_schema: str, client: bool
) -> None:
    store = McpOAuthStore(pool, oauth_schema, SecretCipher(secrets.token_bytes(32)))
    if client:
        operation = store.set_client_info(
            uuid4(), uuid4(), OAuthClientInformationFull(client_id="\ud800")
        )
    else:
        operation = store.set_tokens(
            uuid4(), uuid4(), OAuthToken(access_token="\ud800")
        )

    with pytest.raises(McpOAuthStoreError) as failed:
        await operation

    assert failed.value.__cause__ is not None
    assert "serializ" in str(failed.value).lower()


async def test_wrong_key_is_a_storage_error_with_crypto_cause(
    pool: AsyncPostgresPool, oauth_schema: str, oauth_server: UUID, oauth_user: UUID
) -> None:
    store = McpOAuthStore(pool, oauth_schema, SecretCipher(secrets.token_bytes(32)))
    await store.set_tokens(oauth_server, oauth_user, OAuthToken(access_token="secret"))
    wrong_key = McpOAuthStore(pool, oauth_schema, SecretCipher(secrets.token_bytes(32)))
    with pytest.raises(McpOAuthStoreError) as failed:
        await wrong_key.get_tokens(oauth_server, oauth_user)

    assert isinstance(failed.value.__cause__, SecretCryptoError)
    assert "decrypting" in str(failed.value)
    assert str(oauth_server) in str(failed.value)


@dataclass(frozen=True)
class OAuthAccount:
    """Настоящее хранилище и его владелец для отказов во время обмена кодом."""

    store: McpOAuthStore
    server_id: UUID
    user_id: UUID


@pytest.fixture
async def oauth_account(
    pool: AsyncPostgresPool, oauth_schema: str, oauth_server: UUID, oauth_user: UUID
) -> OAuthAccount:
    store = McpOAuthStore(pool, oauth_schema, SecretCipher(secrets.token_bytes(32)))
    return OAuthAccount(store, oauth_server, oauth_user)


@pytest.mark.parametrize("http_callback", [False, True])
async def test_storage_failure_after_browser_sign_in_reaches_callback(
    pool: AsyncPostgresPool,
    oauth_account: OAuthAccount,
    tmp_path: Path,
    http_callback: bool,
) -> None:
    stand = FakeServiceProcess(tmp_path, 300)
    stand.wait()
    store = oauth_account.store
    oauth_schema = store.schema
    oauth_server = oauth_account.server_id
    oauth_user = oauth_account.user_id
    broker = McpBrowserOAuth(
        store, "http://localhost/aid/", client_name="Failing store"
    )
    endpoint = HttpEndpoint(
        service=HttpLocation(scheme="http", host=stand.HOST, port=stand.port, path=""),
        path=urlsplit(stand.url()).path,
        auth=BrowserOAuthAuth(),
    )
    try:
        url = await broker.begin(
            oauth_server,
            oauth_user,
            stand.LOGIN,
            endpoint,
            return_url=AnyHttpUrl("http://localhost/aid/thread/one"),
        )
        browser = UnattendedLogin(stand.LOGIN, SecretStr(stand.PASSWORD), stand.url())
        await browser.redirect(url)
        granted = await browser.callback()
        assert granted.state is not None
        async with pool.connection() as conn:
            await conn.execute(
                sql.SQL("drop table {}.mcp_tokens").format(sql.Identifier(oauth_schema))
            )

        if http_callback:
            app = FastAPI()
            callback = McpOAuthCallback(broker, StandTokens(), "session")
            app.add_api_route(callback.ROUTE, callback.serve)
            token = StandTokens.issue(StandTokens.session(stand.LOGIN, oauth_user))
            transport = httpx2.ASGITransport(app=app)
            async with httpx2.AsyncClient(
                transport=transport, base_url="http://test"
            ) as client:
                response = await client.get(
                    callback.ROUTE,
                    params={"code": granted.code, "state": granted.state},
                    headers={"Authorization": f"Bearer {token}"},
                )

            assert response.status_code == 500
            assert "internal AID error" in response.text
            assert oauth_schema not in response.text
        else:
            with pytest.raises(McpOAuthError) as failed:
                await broker.finish(
                    stand.LOGIN, granted.code, granted.state, granted.iss
                )

            assert failed.value.__cause__ is not None
            assert "saving tokens" in str(failed.value)
            assert oauth_schema in str(failed.value)

        with pytest.raises(McpAppError, match="missing or expired"):
            await broker.finish(stand.LOGIN, granted.code, granted.state, granted.iss)
    finally:
        stand.stop()


async def test_editing_propagates_body_errors_and_cancellation_without_stale_locks(
    pool: AsyncPostgresPool, oauth_schema: str, oauth_server: UUID, oauth_user: UUID
) -> None:
    store = McpOAuthStore(pool, oauth_schema, SecretCipher(secrets.token_bytes(32)))
    broker = McpBrowserOAuth(store, "http://localhost/aid/", client_name="Edit errors")
    broker.provider(oauth_server, oauth_user, "http://localhost/mcp")
    failure = RuntimeError("the caller's operation failed")
    with pytest.raises(RuntimeError) as raised:
        async with broker.editing(oauth_server):
            raise failure

    assert raised.value is failure
    with pytest.raises(asyncio.CancelledError):
        async with broker.editing(oauth_server):
            raise asyncio.CancelledError

    await broker.disconnect(oauth_server, oauth_user)
    assert await store.get_tokens(oauth_server, oauth_user) is None
