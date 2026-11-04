"""Сервер подставляет профили из PostgreSQL по имени и текущим grants."""

from __future__ import annotations

import secrets
from collections.abc import AsyncIterator, Mapping
from uuid import uuid4

import pytest
from psycopg import sql
from pydantic import SecretStr, create_model

from boba.connections.manifest import ConnectionTypes
from boba.connections.stored import GrantTarget
from boba.crypto import SecretCipher
from boba.db.postgres import AsyncPostgresPool
from boba.db.postgres.connection import (
    PasswordAuth,
    PostgresConfig,
    PostgresOptionsConfig,
    PostgresPoolConfig,
)
from boba.identity.context import CallContext
from boba.identity.errors import RefusalError
from boba.krb.connections import KerberosCredentialSource
from boba.mcp_server.connections import NoRefresh
from boba.mcp_server.connections.params import ConnectionParams
from boba.mcp_server.connections.store import ConnectionStore
from boba.mcp_server.connections.user_connections import ArmedConnections
from boba.stand.migrate import StandMigrations
from boba.stand.toolstand import ProbeTools
from boba.stand_core.context import CallStand
from boba.toolkit.calls import ToolCallBase
from boba.toolrun.hosted import ToolInputs
from boba.toolrun.injected import StaticConfig, ToolConfigError

pytestmark = pytest.mark.anyio


@pytest.fixture
async def store(
    pool: AsyncPostgresPool,
    test_postgres: PostgresConfig,
    call_stand: CallStand,
) -> AsyncIterator[ConnectionStore]:
    schema = f"binding_{uuid4().hex[:12]}"
    await StandMigrations().application(test_postgres, schema, "boba.mcp_server")
    identity = call_stand.context("binding", login="alice", roles=("reader",))
    async with pool.connection() as conn:
        await conn.execute(
            sql.SQL(
                "insert into {}.users (id, realm, login) values (%s, %s, %s)"
            ).format(sql.Identifier(schema)),
            (
                identity.subject.user_id,
                identity.subject.identity.realm,
                identity.subject.identity.login,
            ),
        )

    try:
        cipher = SecretCipher(secrets.token_bytes(32))
        yield ConnectionStore(pool, schema, cipher, ConnectionTypes.discover())
    finally:
        async with pool.connection() as conn:
            await conn.execute(
                sql.SQL("drop schema {} cascade").format(sql.Identifier(schema))
            )


class BindingStand:
    """Настоящий resolver и тело-регистратор: тест видит подставленные объекты."""

    SECRET = "binding-private-password"

    def __init__(self, store: ConnectionStore, calls: CallStand) -> None:
        self.store = store
        self.calls = calls
        self.context = calls.context("binding", login="alice", roles=("reader",))
        self.credentials = KerberosCredentialSource(None, NoRefresh())
        self.probes = ProbeTools()
        self.params = ConnectionParams(
            ConnectionTypes.discover(),
            calls.contexts,
            StaticConfig(),
            ArmedConnections(store, self.credentials, calls.contexts),
        )
        schema = create_model(
            "CopyArgs", __base__=ToolCallBase, source=(str, ...), target=(str, ...)
        )
        self.tool = self.probes.recorder("copy", schema)
        self.tool.inputs = ToolInputs(
            connections={"source": "postgres", "target": "postgres"}
        )
        self.params.bind_all((self.tool,))

    async def grant(self, name: str, host: str) -> None:
        profile = PostgresConfig(
            host=host,
            dbname="warehouse",
            options=PostgresOptionsConfig(),
            pool=PostgresPoolConfig(),
            auth=PasswordAuth(
                method="password", user="reader", password=SecretStr(self.SECRET)
            ),
        )
        connection = await self.store.add(name, profile)
        await self.store.grant(
            connection, GrantTarget.user(self.context.subject.user_id)
        )

    async def call(
        self, source: str, target: str, context: CallContext | None = None
    ) -> Mapping[str, object]:
        if context is None:
            context = self.context

        with self.calls.applied(context):
            result = await self.probes.call(
                self.tool, "copy-1", {"source": source, "target": target}
            )

        assert result.ok, result
        return self.probes.last(self.tool.name)


async def test_named_profiles_reach_two_parameters_with_secrets_and_caller(
    store: ConnectionStore, call_stand: CallStand
) -> None:
    stand = BindingStand(store, call_stand)
    await stand.grant("source", "source.internal")
    await stand.grant("target", "target.internal")
    taken = await stand.call("source", "target")
    source = taken["source"]
    target = taken["target"]
    assert isinstance(source, PostgresConfig)
    assert isinstance(target, PostgresConfig)
    assert source.host == "source.internal"
    assert target.host == "target.internal"
    assert isinstance(source.auth, PasswordAuth)
    assert source.auth.password.get_secret_value() == stand.SECRET
    assert source.application_name is not None
    assert "alice" in source.application_name


async def test_names_with_dots_and_wildcards_are_exact_sql_parameters(
    store: ConnectionStore, call_stand: CallStand
) -> None:
    stand = BindingStand(store, call_stand)
    name = "a.b.c.d.e%_'"
    await stand.grant(name, "exact.internal")
    await stand.grant("a.b.c.d.eXX_'", "different.internal")
    rows = await store.for_subject(stand.context.subject, "postgres", name=name)
    assert len(rows) == 1
    assert rows[0].row.name == name
    taken = await stand.call(name, name)
    connection = taken["source"]
    assert isinstance(connection, PostgresConfig)
    assert connection.host == "exact.internal"


async def test_schema_declares_names_and_never_client_profiles(
    store: ConnectionStore, call_stand: CallStand
) -> None:
    stand = BindingStand(store, call_stand)
    schema = stand.tool.args_schema.model_json_schema()
    for name in ("source", "target"):
        field = schema["properties"][name]
        assert field["type"] == "string"
        assert field["x-boba-connection"] == "postgres"
        assert "contentMediaType" not in field
        assert "contentSchema" not in field


async def test_dag_preflight_uses_current_grants_without_calling_body(
    store: ConnectionStore, call_stand: CallStand
) -> None:
    stand = BindingStand(store, call_stand)
    await stand.grant("main", "db.internal")
    args = {"source": "main", "target": "main"}
    with call_stand.applied(stand.context):
        await stand.params.check(stand.tool.name, args)

    assert not stand.probes.received(stand.tool.name)
    stranger = call_stand.context("other", user_id=uuid4(), login="stranger", roles=())
    with (
        call_stand.applied(stranger),
        pytest.raises(RefusalError, match="available to you"),
    ):
        await stand.params.check(stand.tool.name, args)


async def test_unknown_kind_fails_during_tool_binding(
    store: ConnectionStore, call_stand: CallStand
) -> None:
    stand = BindingStand(store, call_stand)
    stand.tool.inputs = ToolInputs(connections={"source": "unregistered"})
    with pytest.raises(ToolConfigError, match="does not know"):
        stand.params.bind_all((stand.tool,))
