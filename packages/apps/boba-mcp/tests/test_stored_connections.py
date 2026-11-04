"""Каталог MCP и вызов по имени: настоящий PostgreSQL, OAuth, HTTP и subprocess.

Проверяется новый путь без AID и без запечатанных клиентом профилей.
"""

from __future__ import annotations

import socket
from collections.abc import AsyncIterator
from pathlib import Path
from uuid import NAMESPACE_URL, uuid4, uuid5

import mcp_types as mt
import pytest
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from psycopg import sql
from pydantic import SecretStr
from service_stand import ServiceStand

from boba.config import bind
from boba.connection_broker.store import ConnectionStore
from boba.connections.manifest import ConnectionTypes
from boba.connections.stored import GrantTarget
from boba.db.postgres import AsyncPostgresPool
from boba.db.postgres.connection import (
    PasswordAuth,
    PostgresConfig,
    PostgresOptionsConfig,
    PostgresPoolConfig,
)
from boba.db.postgres.migrate import (
    MigrationScripts,
    MigrationTarget,
    PostgresMigrations,
)
from boba.kerberos import DelegatedAuth, KeytabAuth
from boba.mcp_server.app import McpAppConfig, mcp_connections
from boba.mcp_server.server import RunLimits
from boba.stand.overlay import ConfigOverlay
from boba.stand.roles import RoleSeed
from boba.stand.site import Stand
from boba.toolrun.stream_calls import WorkflowTool
from boba.transport.http.connection import HttpConnection

pytestmark = [pytest.mark.integration, pytest.mark.anyio]


@pytest.fixture
async def store(
    pool: AsyncPostgresPool,
    test_postgres: PostgresConfig,
    config_overlay: ConfigOverlay,
) -> AsyncIterator[ConnectionStore]:
    schema = f"mcp_catalog_{uuid4().hex[:12]}"
    raw = config_overlay.tree(
        {
            "app.schema": schema,
            "app.postgres": test_postgres.model_dump(mode="json"),
        }
    )
    config = bind(raw, "app", McpAppConfig)
    plan = MigrationScripts(schema).packages(
        (
            "boba.identity_postgres",
            "boba.access_postgres",
            "boba.connection_broker",
        )
    )
    await PostgresMigrations().apply((MigrationTarget(test_postgres, plan),))
    try:
        store = await mcp_connections(config, pool, ConnectionTypes.discover())
        await RoleSeed(store).add(("dev",))
        yield store
    finally:
        async with pool.connection() as conn:
            await conn.execute(
                sql.SQL("drop schema {} cascade").format(sql.Identifier(schema))
            )


@pytest.fixture
async def service(
    tmp_path: Path, store: ConnectionStore
) -> AsyncIterator[ServiceStand]:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = int(probe.getsockname()[1])
    built = ServiceStand(
        tmp_path, RunLimits(max_runs=4, max_waiting=4), port, "", connections=store
    )
    async with built.serving():
        yield built


class CatalogCalls:
    """Стандартный MCP-клиент: строки имён без расширения connection-seal."""

    TOOL = "fake_connection_host"
    SECRET = "catalog-password-do-not-publish"

    def __init__(self, service: ServiceStand) -> None:
        self._service = service

    async def client(self, *, analyst: bool = False) -> Client:
        token_of = self._service.dev_token
        if analyst:
            token_of = self._service.analyst_token
        token = await token_of(ServiceStand.STORED)
        transport = StreamableHttpTransport(
            self._service.url(ServiceStand.STORED), auth=token
        )
        return Client(transport)

    def connection(self, host: str = "warehouse.internal") -> PostgresConfig:
        auth = PasswordAuth(
            method="password", user="warehouse-user", password=SecretStr(self.SECRET)
        )
        return PostgresConfig(
            host=host,
            dbname="warehouse",
            auth=auth,
            options=PostgresOptionsConfig(),
            pool=PostgresPoolConfig(),
        )

    def text(self, result: mt.CallToolResult) -> str:
        parts: list[str] = []
        for block in result.content:
            if isinstance(block, mt.TextContent):
                parts.append(block.text)
        text = "\n".join(parts)
        assert self.SECRET not in text
        return text

    async def grant(
        self, store: ConnectionStore, name: str = "warehouse"
    ) -> GrantTarget:
        owner = uuid5(NAMESPACE_URL, "boba-user:corporate:alice")
        target = GrantTarget.user(owner)
        connection = await store.add(name, self.connection())
        await store.grant(connection, target)
        return target


async def test_catalog_search_and_named_call_keep_secrets_on_server(
    service: ServiceStand, store: ConnectionStore
) -> None:
    calls = CatalogCalls(service)
    await calls.grant(store)
    hidden = await store.add("hidden", calls.connection("hidden.internal"))
    await store.grant(hidden, GrantTarget.user(uuid4()))
    async with await calls.client() as client:
        tools = await client.list_tools()
        capabilities = client.server_capabilities
        listed = await client.call_tool_mcp("connection_list", {})
        found = await client.call_tool_mcp("connection_search", {"name": "ware"})
        called = await client.call_tool_mcp(calls.TOOL, {"connection": "warehouse"})
    assert not listed.is_error
    assert "warehouse" in calls.text(listed)
    assert "hidden" not in calls.text(listed)
    assert "warehouse" in calls.text(found)
    assert not called.is_error, calls.text(called)
    assert calls.text(called) == "warehouse.internal|warehouse"
    tool = next(tool for tool in tools if tool.name == calls.TOOL)
    assert "application/jose" not in str(tool.input_schema)
    assert calls.SECRET not in str(tool.input_schema)
    field = tool.input_schema["properties"]["connection"]
    assert field["type"] == "string"
    assert field["x-boba-connection"] == "postgres"
    assert "enum" not in field
    assert capabilities is not None
    assert "com.boba/connection-seal" not in (capabilities.extensions or {})


async def test_role_grant_is_enforced_for_both_catalog_and_execution(
    service: ServiceStand, store: ConnectionStore
) -> None:
    calls = CatalogCalls(service)
    role = next(role for role in await store.roles() if role.name == "dev")
    connection = await store.add("shared", calls.connection())
    await store.grant(connection, GrantTarget.role(role.id))
    async with await calls.client() as client:
        allowed = await client.call_tool_mcp(calls.TOOL, {"connection": "shared"})
    async with await calls.client(analyst=True) as client:
        listed = await client.call_tool_mcp("connection_list", {})
        refused = await client.call_tool_mcp(calls.TOOL, {"connection": "shared"})
    assert not allowed.is_error, calls.text(allowed)
    assert "shared" not in calls.text(listed)
    assert refused.is_error
    assert "available to you" in calls.text(refused)


async def test_revoke_after_list_refuses_direct_call_and_dag(
    service: ServiceStand, store: ConnectionStore
) -> None:
    calls = CatalogCalls(service)
    target = await calls.grant(store)
    connection = next(row for row in await store.list_all() if row.name == "warehouse")
    async with await calls.client() as client:
        listed = await client.call_tool_mcp("connection_list", {})
        assert "warehouse" in calls.text(listed)
        assert await store.revoke(connection.id, target)
        direct = await client.call_tool_mcp(calls.TOOL, {"connection": "warehouse"})
        dag = await client.call_tool_mcp(
            WorkflowTool.NAME,
            {
                "nodes": [
                    {
                        "key": "query",
                        "tool": calls.TOOL,
                        "args": {"connection": "warehouse"},
                    }
                ]
            },
        )
    assert direct.is_error
    assert dag.is_error
    assert "available to you" in calls.text(direct)
    assert "available to you" in calls.text(dag)


async def test_same_name_in_wrong_kind_is_not_a_postgres_connection(
    service: ServiceStand, store: ConnectionStore
) -> None:
    calls = CatalogCalls(service)
    connection = await store.add("web-only", HttpConnection(host="public.internal"))
    target = GrantTarget.user(uuid5(NAMESPACE_URL, "boba-user:corporate:alice"))
    await store.grant(connection, target)
    async with await calls.client() as client:
        result = await client.call_tool_mcp(calls.TOOL, {"connection": "web-only"})
    assert result.is_error
    assert "postgres" in calls.text(result)


async def test_ambiguous_name_is_hidden_and_cannot_execute(
    service: ServiceStand, store: ConnectionStore
) -> None:
    calls = CatalogCalls(service)
    await calls.grant(store, "duplicate")
    await calls.grant(store, "duplicate")
    async with await calls.client() as client:
        listed = await client.call_tool_mcp("connection_list", {})
        result = await client.call_tool_mcp(calls.TOOL, {"connection": "duplicate"})
    assert "duplicate" not in calls.text(listed)
    assert result.is_error
    assert "more than one" in calls.text(result)


async def test_role_cannot_call_a_tool_from_another_users_catalog(
    service: ServiceStand, store: ConnectionStore
) -> None:
    calls = CatalogCalls(service)
    await calls.grant(store)
    async with await calls.client() as client:
        names = {tool.name for tool in await client.list_tools()}
    assert calls.TOOL in names
    token = await service.weak_token(ServiceStand.STORED)
    transport = StreamableHttpTransport(service.url(ServiceStand.STORED), auth=token)
    async with Client(transport) as client:
        listed = {tool.name for tool in await client.list_tools()}
        direct = await client.call_tool_mcp(calls.TOOL, {"connection": "warehouse"})
        nodes = [
            {"key": "query", "tool": calls.TOOL, "args": {"connection": "warehouse"}}
        ]
        dag = await client.call_tool_mcp(WorkflowTool.NAME, {"nodes": nodes})
    assert calls.TOOL not in listed
    assert direct.is_error
    assert dag.is_error


async def test_stored_connection_outside_the_user_catalog_is_refused(
    service: ServiceStand,
) -> None:
    calls = CatalogCalls(service)
    async with await calls.client(analyst=True) as client:
        result = await client.call_tool_mcp(calls.TOOL, {"connection": "not-granted"})
    assert result.is_error
    message = calls.text(result)
    assert "not among the connections" in message
    assert "not-granted" not in message


async def test_keytab_credentials_are_prepared_by_mcp(
    service: ServiceStand,
    store: ConnectionStore,
    test_postgres: PostgresConfig,
    stand: Stand,
) -> None:
    calls = CatalogCalls(service)
    auth = KeytabAuth(
        method="kerberos_keytab",
        principal=stand.service_principal,
        keytab=stand.pg_krb_keytab,
    )
    profile = test_postgres.model_copy(update={"auth": auth})
    connection = await store.add("kerberos", profile)
    owner = uuid5(NAMESPACE_URL, "boba-user:corporate:alice")
    await store.grant(connection, GrantTarget.user(owner))
    async with await calls.client() as client:
        result = await client.call_tool_mcp(calls.TOOL, {"connection": "kerberos"})
    assert not result.is_error, calls.text(result)
    assert calls.text(result) == f"{profile.host}|{profile.dbname}"


async def test_proxy_identity_does_not_invent_delegated_credentials(
    service: ServiceStand,
    store: ConnectionStore,
    test_postgres: PostgresConfig,
) -> None:
    calls = CatalogCalls(service)
    auth = DelegatedAuth(method="kerberos_delegated")
    profile = test_postgres.model_copy(update={"auth": auth})
    connection = await store.add("delegated", profile)
    owner = uuid5(NAMESPACE_URL, "boba-user:corporate:alice")
    await store.grant(connection, GrantTarget.user(owner))
    async with await calls.client() as client:
        result = await client.call_tool_mcp(calls.TOOL, {"connection": "delegated"})
    assert result.is_error
    assert "kerberos" in calls.text(result).lower()


@pytest.mark.parametrize(
    "value",
    [
        {
            "kind": "postgres",
            "host": "injected.internal",
            "auth": {"method": "trust", "user": "intruder"},
        },
        '{"kind":"postgres","host":"injected.internal"}',
        "header.encrypted-key.iv.ciphertext.tag",
    ],
    ids=["profile-object", "profile-json", "ciphertext"],
)
async def test_client_profile_cannot_bypass_server_grants(
    service: ServiceStand,
    value: object,
) -> None:
    calls = CatalogCalls(service)
    args = {"connection": value}
    nodes = [{"key": "query", "tool": calls.TOOL, "args": args}]
    async with await calls.client() as client:
        direct = await client.call_tool_mcp(calls.TOOL, args)
        dag = await client.call_tool_mcp(WorkflowTool.NAME, {"nodes": nodes})

    assert direct.is_error
    assert dag.is_error
    calls.text(direct)
    calls.text(dag)
