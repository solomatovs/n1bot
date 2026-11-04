"""Пользовательские MCP-серверы: настоящий Postgres, выдача и шифрование."""

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Literal
from uuid import UUID

import pytest
from acp_stand import AppsHost
from aid_stand import StandTokens
from psycopg import sql
from pydantic import SecretStr

from boba.aid.data.data_layer import PostgresDataLayer
from boba.aid.data.users import UsersTable
from boba.aid.infra.config import AppConfig
from boba.aid.mcp_servers import McpAppError, McpAuthChoice, McpOrigin
from boba.aid.mcp_servers.config import McpDefaultsConfig
from boba.aid.mcp_servers.directory import McpDirectory, McpState
from boba.aid.mcp_servers.form import HeaderField, McpForms, McpServerForm
from boba.aid.mcp_servers.sources import McpViewer, StoredServers
from boba.aid.mcp_servers.store import (
    McpServerNameTaken,
    McpServerStore,
    McpServerStoreError,
    StoredServer,
)
from boba.crypto import SecretCipher
from boba.db.postgres import AsyncPostgresPool
from boba.identity.principal import UserIdentity
from boba.mcp_client.client import (
    BearerAuth,
    HttpEndpoint,
    McpClientError,
    McpServerConfig,
    McpServers,
    ProxyAuth,
    SseEndpoint,
)

pytestmark = pytest.mark.anyio


@dataclass(frozen=True)
class StoreStand:
    """Хранилище, его источник и два пользователя настоящей базы теста."""

    store: McpServerStore
    source: StoredServers
    owner: McpViewer
    other: McpViewer
    pool: AsyncPostgresPool
    schema: str


@pytest.fixture
async def stored(
    layer: PostgresDataLayer,
    pool: AsyncPostgresPool,
    app_config: AppConfig,
) -> StoreStand:
    schema = app_config.db_schema
    users = UsersTable(pool, schema)
    await users.upsert(UserIdentity.model_validate("corporate:mcp-owner"))
    await users.upsert(UserIdentity.model_validate("corporate:mcp-other"))

    owner = await layer.create_user(StandTokens.user(identifier="corporate:mcp-owner"))
    other = await layer.create_user(StandTokens.user(identifier="corporate:mcp-other"))
    assert owner is not None
    assert other is not None
    cipher = SecretCipher(app_config.mcp_servers.cipher.get_secret_value())
    store = McpServerStore(pool, schema, cipher)
    return StoreStand(
        store,
        StoredServers(store, users),
        McpViewer(
            user_id=UUID(owner.id), login=owner.identifier, roles=frozenset({"reader"})
        ),
        McpViewer(user_id=UUID(other.id), login=other.identifier, roles=frozenset()),
        pool,
        schema,
    )


class TestStoredServers:
    def _form(self, name: str = "personal") -> McpServerForm:
        return McpServerForm(
            name=name,
            transport="streamable-http",
            url="https://mcp.invalid/mcp",
            auth=McpAuthChoice.BEARER,
            token=SecretStr("owner-bearer-secret"),
            headers=(
                HeaderField(name="X-Api-Key", value=SecretStr("owner-header-secret")),
            ),
        )

    async def test_add_encrypts_grants_and_round_trips(
        self, stored: StoreStand
    ) -> None:
        server = McpForms(McpDefaultsConfig()).server(self._form())
        server_id = await stored.store.add("personal", server, stored.owner.user_id)
        rows = await stored.store.visible_to(stored.owner.user_id, stored.owner.roles)
        assert len(rows) == 1
        assert rows[0].id == server_id
        assert rows[0].editable
        assert rows[0].server == server
        assert await stored.store.visible_to(stored.other.user_id, ()) == []
        query = sql.SQL("select data::text from {}.mcp_servers where id = %s").format(
            sql.Identifier(stored.schema)
        )
        async with stored.pool.connection() as conn:
            row = await (await conn.execute(query, (server_id,))).fetchone()

        assert row is not None
        assert "owner-bearer-secret" not in row[0]
        assert "owner-header-secret" not in row[0]
        assert row[0].count("enc:v1:") == 2
        config = await stored.source.config_of("personal", stored.owner.caller())
        assert config is not None
        assert config.endpoint == server.endpoint
        assert await stored.source.config_of("personal", stored.other.caller()) is None

    async def test_replace_keeps_grants_and_enabled_and_unique_name(
        self, stored: StoreStand
    ) -> None:
        forms = McpForms(McpDefaultsConfig())
        server = forms.server(self._form())
        server_id = await stored.store.add("personal", server, stored.owner.user_id)
        tokens = sql.SQL(
            "insert into {}.mcp_tokens (server_id, user_id, token_data) "
            "values (%s, %s, %s)"
        ).format(sql.Identifier(stored.schema))
        async with stored.pool.connection() as conn:
            await conn.execute(tokens, (server_id, stored.owner.user_id, "old-token"))
        await stored.store.set_enabled(server_id, False)
        with pytest.raises(McpServerNameTaken):
            await stored.store.add("personal", server, stored.other.user_id)

        changed = forms.server(
            self._form().model_copy(update={"url": "https://changed.invalid/new"}),
            server,
        )
        await stored.store.replace(server_id, "renamed", changed)
        remaining = sql.SQL(
            "select count(*) from {}.mcp_tokens where server_id = %s"
        ).format(sql.Identifier(stored.schema))
        async with stored.pool.connection() as conn:
            token_count = await (await conn.execute(remaining, (server_id,))).fetchone()
        assert token_count is not None
        assert token_count[0] == 0
        record = await stored.source.record(stored.owner, "renamed")
        assert record.server_id == server_id
        assert record.editable
        assert not record.enabled
        assert record.config.endpoint.label() == "https://changed.invalid/new"
        assert await stored.store.by_name("personal") is None
        assert await stored.source.config_of("renamed", stored.owner.caller()) is None

    @pytest.mark.parametrize("auth", ["bearer", "proxy"])
    async def test_role_grant_is_visible_but_read_only(
        self, stored: StoreStand, auth: Literal["bearer", "proxy"]
    ) -> None:
        server = McpForms(McpDefaultsConfig()).server(self._form())
        if auth == "proxy":
            proxy = ProxyAuth(
                issuer=server.endpoint.service,
                client_id="role-client",
                client_secret=SecretStr("role-client-secret"),
                secret=SecretStr("role-signing-secret"),
            )
            endpoint = server.endpoint.model_copy(update={"auth": proxy})
            server = StoredServer(endpoint=endpoint, limits=server.limits)

        server_id = await stored.store.add("shared", server, stored.other.user_id)
        schema = sql.Identifier(stored.schema)
        async with stored.pool.connection() as conn, conn.transaction():
            await conn.execute(
                sql.SQL("insert into {}.roles (role) values ('reader')").format(schema)
            )
            query = sql.SQL("""
                insert into {}.grants (src_kind, src_kind_id, tgt_kind, tgt_kind_id)
                select 'mcp_servers', %s, 'roles', id
                from {}.roles where role = 'reader'
            """).format(schema, schema)
            await conn.execute(query, (server_id,))

        record = await stored.source.record(stored.owner, "shared")
        assert record.origin is McpOrigin.ROLE
        assert not record.editable
        with pytest.raises(McpAppError, match="personally"):
            await stored.source.remove(stored.owner, "shared")

        with pytest.raises(McpAppError, match="personally"):
            await stored.source.replace(stored.owner, "shared", "hijacked", server)

        with pytest.raises(McpAppError, match="personally"):
            await stored.source.set_enabled(stored.owner, "shared", False)

        servers = McpServers(stored.source)
        directory = McpDirectory(stored.source, servers, McpDefaultsConfig())
        try:
            view = await directory.form(stored.owner, "shared")
            assert not view.editable
            assert view.auth == auth
            assert "owner-bearer-secret" not in view.model_dump_json()
            assert "owner-header-secret" not in view.model_dump_json()
            assert "role-client-secret" not in view.model_dump_json()
            assert "role-signing-secret" not in view.model_dump_json()
            with pytest.raises(McpAppError, match="personally"):
                await directory.save(stored.owner, self._form(), current="shared")

            with pytest.raises(McpAppError, match="personally"):
                await directory.set_enabled(stored.owner, "shared", False)

            with pytest.raises(McpAppError, match="personally"):
                await directory.remove(stored.owner, "shared")

            outsider = stored.owner.model_copy(update={"roles": frozenset()})
            with pytest.raises(McpAppError):
                await directory.form(outsider, "shared")

            after = await stored.store.by_name("shared")
            assert after is not None
            assert after.enabled
            assert after.server == server
        finally:
            await servers.stop()

    async def test_wrong_key_and_malformed_data_do_not_expose_secrets(
        self, stored: StoreStand
    ) -> None:
        server = McpForms(McpDefaultsConfig()).server(self._form())
        await stored.store.add("personal", server, stored.owner.user_id)
        wrong = McpServerStore(stored.pool, stored.schema, SecretCipher(bytes(32)))
        with pytest.raises(McpServerStoreError, match="personal") as refused:
            await wrong.by_name("personal")

        assert "owner-bearer-secret" not in str(refused.value)
        assert "owner-header-secret" not in str(refused.value)
        query = sql.SQL(
            "update {}.mcp_servers set data = jsonb_set("
            "data, '{{endpoint,transport}}', '\"secret-invalid-transport\"')"
        ).format(sql.Identifier(stored.schema))
        async with stored.pool.connection() as conn:
            await conn.execute(query)

        with pytest.raises(McpServerStoreError) as malformed:
            await stored.store.by_name("personal")

        assert "secret-invalid-transport" not in str(malformed.value)
        assert "owner-bearer-secret" not in str(malformed.value)

    async def test_directory_crud_and_disabled_client_access(
        self, stored: StoreStand
    ) -> None:
        servers = McpServers(stored.source)
        directory = McpDirectory(stored.source, servers, McpDefaultsConfig())
        try:
            added = await directory.save(stored.owner, self._form())
            assert added.state is McpState.PENDING
            view = await directory.form(stored.owner, "personal")
            assert view.editable
            assert view.has_token
            assert view.headers == ("X-Api-Key",)
            assert "owner-bearer-secret" not in view.model_dump_json()
            assert "owner-header-secret" not in view.model_dump_json()
            before = await servers.port("personal", stored.owner.caller())
            blank = self._form().model_copy(
                update={
                    "token": SecretStr(""),
                    "headers": (HeaderField(name="X-Api-Key", value=SecretStr("")),),
                }
            )
            await directory.save(stored.owner, blank, "personal")
            after = await servers.port("personal", stored.owner.caller())
            assert after is not before
            record = await stored.source.record(stored.owner, "personal")
            assert isinstance(record.config.endpoint, (HttpEndpoint, SseEndpoint))
            assert isinstance(record.config.endpoint.auth, BearerAuth)
            assert (
                record.config.endpoint.auth.token.get_secret_value()
                == "owner-bearer-secret"
            )
            disabled = await directory.set_enabled(stored.owner, "personal", False)
            assert disabled.state is McpState.DISABLED
            assert (
                await directory.status(stored.owner, "personal")
            ).state is McpState.DISABLED
            with pytest.raises(McpClientError, match="not granted"):
                await servers.port("personal", stored.owner.caller())

            with pytest.raises(McpAppError):
                await directory.remove(stored.other, "personal")

            await directory.set_enabled(stored.owner, "personal", True)
            removed = await directory.remove(stored.owner, "personal")
            assert removed.servers == []
            assert await stored.store.by_name("personal") is None
            query = sql.SQL(
                "select count(*) from {}.grants where src_kind = 'mcp_servers'"
            ).format(sql.Identifier(stored.schema))
            async with stored.pool.connection() as conn:
                row = await (await conn.execute(query)).fetchone()

            assert row == (0,)
        finally:
            await servers.stop()


@pytest.fixture(scope="module", params=["streamable-http", "sse"])
def keyed_host(request: pytest.FixtureRequest) -> Iterator[AppsHost]:
    host = AppsHost(["--require-header", "--require-bearer"], transport=request.param)
    try:
        host.await_listening()
        yield host
    finally:
        host.stop()


async def test_saved_headers_and_bearer_reach_the_real_server(
    stored: StoreStand,
    keyed_host: AppsHost,
) -> None:
    config = McpServerConfig.model_validate(keyed_host.plain())
    assert isinstance(config.endpoint, (HttpEndpoint, SseEndpoint))
    denied_state = McpState.NEEDS_AUTH
    if config.endpoint.transport == "sse":
        denied_state = McpState.FAILED

    form = McpServerForm(
        name="keyed",
        transport=config.endpoint.transport,
        url=config.endpoint.label(),
        auth=McpAuthChoice.BEARER,
        token=SecretStr("stand-bearer-secret"),
        headers=(
            HeaderField(name="X-Api-Key", value=SecretStr("stand-header-secret")),
        ),
    )
    servers = McpServers(stored.source)
    directory = McpDirectory(stored.source, servers, McpDefaultsConfig())
    try:
        await directory.save(stored.owner, form)
        assert (
            await directory.status(stored.owner, "keyed")
        ).state is McpState.CONNECTED
        missing_header = form.model_copy(update={"headers": ()})
        await directory.save(stored.owner, missing_header, "keyed")
        assert (await directory.status(stored.owner, "keyed")).state is denied_state
        wrong_bearer = form.model_copy(update={"token": SecretStr("wrong-bearer")})
        await directory.save(stored.owner, wrong_bearer, "keyed")
        assert (await directory.status(stored.owner, "keyed")).state is denied_state
        await directory.save(stored.owner, form, "keyed")
        assert (
            await directory.status(stored.owner, "keyed")
        ).state is McpState.CONNECTED
    finally:
        await servers.stop()
