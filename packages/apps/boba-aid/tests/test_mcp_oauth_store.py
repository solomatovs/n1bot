"""OAuth credentials survive store recreation, stay encrypted and isolated."""

from __future__ import annotations

import secrets
from importlib.resources import files
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from uuid import UUID, uuid4

import pytest
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from psycopg import sql
from pydantic import SecretStr

from boba.aid.mcp_servers import McpAppError
from boba.aid.mcp_servers.oauth import McpBrowserOAuth
from boba.aid.mcp_servers.oauth_store import McpOAuthStore
from boba.connection_broker.crypto import SecretCipher
from boba.db.postgres import AsyncPostgresPool, PostgresSchema
from boba.mcp_client.client import (
    BrowserOAuthAuth,
    HttpEndpoint,
    HttpLocation,
    McpServerConfig,
    McpServerLimits,
    McpServers,
    McpToolServer,
    UnattendedLogin,
)
from boba.stand.service_signin import FakeServiceProcess

pytestmark = pytest.mark.anyio


async def test_oauth_credentials_are_encrypted_and_private(
    pool: AsyncPostgresPool, test_database: str
) -> None:
    schema = PostgresSchema(test_database)
    schema_dir = files("boba.aid.mcp_servers") / "schema"
    async with pool.connection() as conn:
        await schema.apply_packages(conn, ("boba.identity_postgres", "boba.runtime"))
        await schema.apply_files(conn, schema_dir, {})
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
                "insert into {}.users (id, identifier) values (%s, %s), (%s, %s)"
            ).format(schema.ident),
            (
                first_user,
                f"oauth-{first_user.hex}",
                second_user,
                f"oauth-{second_user.hex}",
            ),
        )
    first = McpOAuthStore(pool, test_database, cipher)
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

    reopened = McpOAuthStore(pool, test_database, cipher)
    assert await reopened.get_tokens(server_id, first_user) == token
    assert await reopened.get_client_info(server_id, first_user) == client
    assert await reopened.get_tokens(server_id, second_user) is None

    await reopened.delete(server_id, first_user)
    assert await reopened.get_tokens(server_id, first_user) is None
    assert await reopened.get_client_info(server_id, first_user) is None


async def test_browser_authorization_round_trips_to_mcp(
    pool: AsyncPostgresPool, test_database: str, tmp_path: Path
) -> None:
    stand = FakeServiceProcess(tmp_path, 300)
    stand.wait()
    try:
        schema = PostgresSchema(test_database)
        async with pool.connection() as conn:
            await schema.apply_packages(
                conn, ("boba.identity_postgres", "boba.runtime")
            )
            await schema.apply_files(conn, files("boba.aid.mcp_servers") / "schema", {})
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

        user_id = uuid4()
        async with pool.connection() as conn:
            await conn.execute(
                sql.SQL("insert into {}.users (id, identifier) values (%s, %s)").format(
                    schema.ident
                ),
                (user_id, f"oauth-{user_id.hex}"),
            )
        store = McpOAuthStore(
            pool, test_database, SecretCipher(secrets.token_bytes(32))
        )
        broker = McpBrowserOAuth(store, "http://127.0.0.1/aid/api/mcp/oauth/callback")
        url = await broker.begin(server_id, user_id, stand.LOGIN, stand.url())
        state = parse_qs(urlsplit(url).query)["state"][0]
        with pytest.raises(McpAppError, match="matching AID sign-in"):
            broker.reject("someone-else", state)
        broker.reject(stand.LOGIN, state)
        url = await broker.begin(server_id, user_id, stand.LOGIN, stand.url())
        browser = UnattendedLogin(stand.LOGIN, SecretStr(stand.PASSWORD), stand.url())
        await browser.redirect(url)
        granted = await browser.callback()

        with pytest.raises(McpAppError, match="another AID user"):
            await broker.finish(
                "someone-else", granted.code, granted.state or "", granted.iss
            )
        await broker.finish(stand.LOGIN, granted.code, granted.state or "", granted.iss)
        assert await store.get_tokens(server_id, user_id) is not None
        assert await store.get_client_info(server_id, user_id) is not None

        auth = BrowserOAuthAuth().bound(broker, server_id, user_id)
        endpoint = HttpEndpoint(
            service=HttpLocation(
                scheme="http", host=stand.HOST, port=stand.port, path=""
            ),
            path=stand.ENDPOINT,
            auth=auth,
        )
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

        await broker.disconnect(server_id, user_id)
        assert await store.get_tokens(server_id, user_id) is None
    finally:
        stand.stop()
