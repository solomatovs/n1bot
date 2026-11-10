"""Браузерный OAuth AID получает токен, но MCP отказывает по ролям endpoint'а."""

import secrets
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from uuid import UUID, uuid4

import pytest
from psycopg import sql
from pydantic import AnyHttpUrl, SecretStr
from service_stand import LOCAL_LOGIN, LOCAL_PASSWORD, PROFILE, ServiceStand

from boba.aid.mcp_servers import McpAppError
from boba.aid.mcp_servers.oauth import McpBrowserOAuth
from boba.aid.mcp_servers.oauth_store import McpOAuthStore
from boba.crypto import SecretCipher
from boba.db.postgres import AsyncPostgresPool
from boba.db.postgres.connection import PostgresConfig
from boba.mcp_client.client import (
    BrowserOAuthAuth,
    HttpEndpoint,
    HttpLocation,
    UnattendedLogin,
)
from boba.mcp_server.catalog import EndpointCatalog
from boba.mcp_server.config.models import RunLimits
from boba.stand.migrate import StandMigrations
from boba.stand.ui.config import StandPorts

pytestmark = [pytest.mark.anyio, pytest.mark.integration]


@dataclass(frozen=True)
class OAuthAccount:
    """Учётная запись AID с настоящим хранилищем токенов для проверки отказа MCP."""

    broker: McpBrowserOAuth
    store: McpOAuthStore
    server_id: UUID
    user_id: UUID


@pytest.fixture
async def account(
    pool: AsyncPostgresPool, test_postgres: PostgresConfig
) -> AsyncIterator[OAuthAccount]:
    name = f"oauth_refusal_{uuid4().hex[:12]}"
    await StandMigrations().application(test_postgres, name, "boba.aid")
    schema = sql.Identifier(name)
    server_id = uuid4()
    user_id = uuid4()
    try:
        async with pool.connection() as conn:
            await conn.execute(
                sql.SQL("insert into {}.users (id, login) values (%s, %s)").format(
                    schema
                ),
                (user_id, LOCAL_LOGIN),
            )
            await conn.execute(
                sql.SQL(
                    "insert into {}.mcp_servers (id, name, data) "
                    "values (%s, 'denied', '{{}}'::jsonb)"
                ).format(schema),
                (server_id,),
            )

        store = McpOAuthStore(pool, name, SecretCipher(secrets.token_bytes(32)))
        broker = McpBrowserOAuth(
            store,
            "http://127.0.0.1/debug/aid/",
            client_name="OAuth refusal test",
        )
        yield OAuthAccount(broker, store, server_id, user_id)
    finally:
        async with pool.connection() as conn:
            await conn.execute(sql.SQL("drop schema {} cascade").format(schema))


@pytest.mark.parametrize("allowed_roles", [frozenset(), frozenset({"analyst"})])
async def test_role_refusal_does_not_restart_completed_browser_flow(
    account: OAuthAccount, tmp_path: Path, allowed_roles: frozenset[str]
) -> None:
    port = StandPorts().free()
    stand = ServiceStand(tmp_path, RunLimits(max_runs=4, max_waiting=4), port, "")
    endpoints = list(stand.catalog.endpoints())
    endpoints[0] = endpoints[0].model_copy(update={"roles": allowed_roles})
    stand.catalog.replace(EndpointCatalog(endpoints))
    stand.endpoints = stand.build(stand.catalog)
    endpoint = HttpEndpoint(
        service=HttpLocation(scheme="http", host="127.0.0.1", port=port, path=""),
        path=f"/{PROFILE}",
        auth=BrowserOAuthAuth(),
    )
    browser = UnattendedLogin(
        LOCAL_LOGIN, SecretStr(LOCAL_PASSWORD), stand.url(PROFILE)
    )
    broker = account.broker
    async with stand.serving():
        url = await broker.begin(
            account.server_id,
            account.user_id,
            LOCAL_LOGIN,
            endpoint,
            return_url=AnyHttpUrl("http://127.0.0.1/debug/aid/thread/test"),
        )
        await browser.redirect(url)
        granted = await browser.callback()
        assert granted.state is not None

        with pytest.raises(McpAppError, match="allowed roles") as refused:
            await broker.finish(LOCAL_LOGIN, granted.code, granted.state, granted.iss)

        assert "403" in str(refused.value)
        assert "InvalidStateError" not in str(refused.value)
        token = await account.store.get_tokens(account.server_id, account.user_id)
        assert token is not None
        assert token.scope is not None
        assert f"/{PROFILE}" not in token.scope.split()
        with pytest.raises(McpAppError, match="state is missing or expired"):
            await broker.finish(LOCAL_LOGIN, granted.code, granted.state, granted.iss)

        retry = await broker.begin(
            account.server_id,
            account.user_id,
            LOCAL_LOGIN,
            endpoint,
            return_url=AnyHttpUrl("http://127.0.0.1/debug/aid/thread/test"),
        )
        retry_state = parse_qs(urlsplit(retry).query)["state"][0]
        assert retry_state != granted.state
        broker.reject(LOCAL_LOGIN, retry_state)
