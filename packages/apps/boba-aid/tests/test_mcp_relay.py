"""Ретранслятор mcp/message без агента: запросы MCP по serverId регистрации
исполняют настоящие серверы — обычный сервер на SDK mcp по stdio и стенд
boba-mcp со входом proxy. Проверяются строки таблицы ошибок MCP-over-ACP,
встроенные методы SDK, видимость инструментов для модели, ход вызова и
запечатывание соединений пользователя."""

from __future__ import annotations

import asyncio
import secrets
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
from importlib.resources import files
from typing import Any, ClassVar
from uuid import uuid4

import mcp_types as mt
import pytest
from acp.exceptions import RequestError
from aid_stand import NoConnectionStore, RelayStand, ServiceProcess, TabSession

from boba.aid.agent.relay import McpRelay
from boba.auth.credentials import KerberosCredentialSource, NoRefresh
from boba.connection_broker.sealing import McpServerInfo
from boba.connection_broker.store import ConnectionStore
from boba.connections.manifest import ConnectionTypes
from boba.connections.sealed import ConnectionOffer, SealedParamSchema, SealFeature
from boba.connections.secrets import SecretCipher
from boba.db.postgres import AsyncPostgresPool, PostgresSchema
from boba.db.postgres.connection import PostgresConfig
from boba.identity.errors import RefusalError
from boba.mcp_client.client import McpServerConfig, McpServers, McpServersConfig
from boba.stand_core.context import CallStand
from boba.toolrun.call_id import CallFields
from boba.transport.acp.protocol import (
    AcpErrorCode,
    McpErrorOutcome,
    McpMessage,
    McpOutcome,
    McpResultOutcome,
    McpVersion,
)

pytestmark = pytest.mark.anyio


@pytest.fixture
def tab(auth_token: str) -> TabSession:
    """Вкладка пользователя ivanov, вошедшего паролем с ролью dev."""
    session = TabSession()
    session.password_sign_in(frozenset({"dev"}))

    return session


@pytest.fixture
def relay_stand(call_stand: CallStand) -> RelayStand:
    """Ретранслятор стенда без соединений пользователя."""
    credentials = KerberosCredentialSource(None, NoRefresh())

    return RelayStand(NoConnectionStore(), lambda: credentials, call_stand.contexts)


class TestMcpRelay:
    """Ретранслятор над обычным сервером: таблица ошибок, встроенные
    методы SDK, видимость инструментов, ход вызова и отказ запечатывания
    серверу, который не принимает соединений."""

    ENVELOPE: ClassVar[dict[str, Any]] = {
        mt.PROTOCOL_VERSION_META_KEY: McpVersion.V2026_07_28.value,
        mt.CLIENT_CAPABILITIES_META_KEY: {},
    }

    @pytest.fixture
    async def relay(
        self, relay_stand: RelayStand, standard: McpServerConfig
    ) -> AsyncIterator[McpRelay]:
        servers = McpServers(McpServersConfig(servers={"standard": standard}))
        try:
            yield relay_stand.relay(servers)
        finally:
            await servers.stop()

    def _message(
        self, server_id: str, method: str, params: dict[str, Any]
    ) -> McpMessage:
        sent = dict(params)
        sent["_meta"] = {**self.ENVELOPE, **params.get("_meta", {})}

        return McpMessage(
            server_id=server_id,
            request_id=f"mcp:{uuid4().hex}",
            method=method,
            params=sent,
        )

    async def _answer(
        self, relay: McpRelay, message: McpMessage
    ) -> tuple[McpOutcome, list[McpMessage]]:
        heard: asyncio.Queue[McpMessage] = asyncio.Queue()
        outcome = await relay.answer(message, heard.put)
        notes: list[McpMessage] = []
        while not heard.empty():
            notes.append(heard.get_nowait())

        return outcome, notes

    def _server_id(self, stand: RelayStand, tab: TabSession) -> str:
        holder = tab.holder(uuid4(), "general")

        return stand.keys.register(holder, ["standard"])[0].server_id

    async def test_unknown_server_id_is_an_external_error(
        self, relay: McpRelay
    ) -> None:
        message = self._message("ghost", "tools/list", {})

        with pytest.raises(RequestError) as refused:
            await relay.answer(message, asyncio.Queue[McpMessage]().put)

        assert refused.value.code == AcpErrorCode.UNKNOWN_SERVER
        assert "'ghost'" in str(refused.value)

    async def test_request_id_in_work_is_an_external_error(
        self, relay: McpRelay, relay_stand: RelayStand, tab: TabSession
    ) -> None:
        server_id = self._server_id(relay_stand, tab)
        slow = self._message(
            server_id,
            "tools/call",
            {"name": "sleep_for", "arguments": {"seconds": 0.5}},
        )
        running = asyncio.ensure_future(self._answer(relay, slow))
        await asyncio.sleep(0.2)

        with pytest.raises(RequestError) as refused:
            await relay.answer(slow, asyncio.Queue[McpMessage]().put)

        outcome, _notes = await running
        assert refused.value.code == AcpErrorCode.INVALID_PARAMS
        assert "already in work" in str(refused.value)
        assert isinstance(outcome, McpResultOutcome), outcome

    async def test_envelope_and_version_are_inner_errors(
        self, relay: McpRelay, relay_stand: RelayStand, tab: TabSession
    ) -> None:
        server_id = self._server_id(relay_stand, tab)
        bare = McpMessage(
            server_id=server_id, request_id="mcp:bare", method="tools/list", params={}
        )
        old = self._message(
            server_id,
            "tools/list",
            {"_meta": {mt.PROTOCOL_VERSION_META_KEY: "2025-11-25"}},
        )

        unwrapped, _ = await self._answer(relay, bare)
        outdated, _ = await self._answer(relay, old)

        assert isinstance(unwrapped, McpErrorOutcome), unwrapped
        assert unwrapped.error.code == mt.INVALID_PARAMS
        assert isinstance(outdated, McpErrorOutcome), outdated
        assert outdated.error.code == mt.UNSUPPORTED_PROTOCOL_VERSION

    async def test_sdk_answers_discover_ping_and_unknown_methods(
        self, relay: McpRelay, relay_stand: RelayStand, tab: TabSession
    ) -> None:
        server_id = self._server_id(relay_stand, tab)

        discovered, _ = await self._answer(
            relay, self._message(server_id, "server/discover", {})
        )
        pinged, _ = await self._answer(relay, self._message(server_id, "ping", {}))
        unknown, _ = await self._answer(
            relay, self._message(server_id, "resources/list", {})
        )

        assert isinstance(discovered, McpResultOutcome), discovered
        result = mt.DiscoverResult.model_validate(discovered.result)
        assert McpVersion.V2026_07_28.value in result.supported_versions
        # ping в редакции 2026-07-28 не определён: SDK отвечает -32601
        assert isinstance(pinged, McpErrorOutcome), pinged
        assert pinged.error.code == mt.METHOD_NOT_FOUND
        assert isinstance(unknown, McpErrorOutcome), unknown
        assert unknown.error.code == mt.METHOD_NOT_FOUND

    async def test_app_only_tool_is_neither_listed_nor_called(
        self, relay: McpRelay, relay_stand: RelayStand, tab: TabSession
    ) -> None:
        server_id = self._server_id(relay_stand, tab)
        call = {"name": "panel_refresh", "arguments": {}}

        listed, _ = await self._answer(
            relay, self._message(server_id, "tools/list", {})
        )
        called, _ = await self._answer(
            relay, self._message(server_id, "tools/call", call)
        )

        assert isinstance(listed, McpResultOutcome), listed
        names = [
            tool.name for tool in mt.ListToolsResult.model_validate(listed.result).tools
        ]
        assert "add" in names, names
        assert "panel_refresh" not in names, names
        assert isinstance(called, McpErrorOutcome), called
        assert called.error.code == mt.INVALID_PARAMS
        assert "'panel_refresh'" in called.error.message

    async def test_progress_reaches_the_agent_under_its_token(
        self, relay: McpRelay, relay_stand: RelayStand, tab: TabSession
    ) -> None:
        server_id = self._server_id(relay_stand, tab)
        call = {
            "name": "progress",
            "arguments": {"steps": 2},
            "_meta": {"progressToken": "call_001"},
        }
        message = self._message(server_id, "tools/call", call)

        outcome, notes = await self._answer(relay, message)

        assert isinstance(outcome, McpResultOutcome), outcome
        reply = mt.CallToolResult.model_validate(outcome.result)
        assert not reply.is_error, reply
        assert [note.method for note in notes] == ["notifications/progress"] * 2
        for note in notes:
            assert note.server_id == server_id
            assert note.request_id == message.request_id
            progress = mt.ProgressNotificationParams.model_validate(note.params)
            assert progress.progress_token == "call_001"

    async def test_input_required_and_server_errors_come_back_as_they_are(
        self, relay: McpRelay, relay_stand: RelayStand, tab: TabSession
    ) -> None:
        server_id = self._server_id(relay_stand, tab)
        asking = {"name": "ask", "arguments": {"refuse": False}}
        refusing = {"name": "ask", "arguments": {"refuse": True}}

        asked, _ = await self._answer(
            relay, self._message(server_id, "tools/call", asking)
        )
        refused, _ = await self._answer(
            relay, self._message(server_id, "tools/call", refusing)
        )

        assert isinstance(asked, McpResultOutcome), asked
        assert mt.InputRequiredResult.model_validate(asked.result).request_state
        assert isinstance(refused, McpErrorOutcome), refused
        assert refused.error.code == mt.INVALID_PARAMS
        assert refused.error.data == {"reason": "the stand refuses"}

    async def test_dead_server_is_an_external_error(
        self, relay: McpRelay, relay_stand: RelayStand, tab: TabSession
    ) -> None:
        server_id = self._server_id(relay_stand, tab)
        message = self._message(server_id, "tools/call", {"name": "die"})

        with pytest.raises(RequestError) as refused:
            await relay.answer(message, asyncio.Queue[McpMessage]().put)

        assert refused.value.code == AcpErrorCode.BACKEND_FAILED
        assert "'die'" in str(refused.value)

    async def test_sealing_is_refused_to_a_server_without_the_seal_feature(
        self, relay_stand: RelayStand
    ) -> None:
        offer = ConnectionOffer(kind="postgres", sealed=True, names={})
        declared = SealedParamSchema().declared(offer)
        schema = {
            "type": "object",
            "properties": {"connection": {"type": "string", **declared}},
        }
        server = McpServerInfo(name="standard", features={})

        with pytest.raises(RefusalError) as refused:
            await relay_stand.sealing.seal(
                "mcp__standard__query", {"connection": "orders"}, schema, server
            )

        assert SealFeature.ID in str(refused.value)
        assert "'standard'" in str(refused.value)

    async def test_server_name_goes_as_is_when_only_the_server_is_offered(
        self, relay_stand: RelayStand
    ) -> None:
        """Схема без ветки запечатанного значения: клиент не запечатывает и
        ключа сервера не требует, имя соединения сервера уходит как есть."""
        offer = ConnectionOffer(kind="postgres", sealed=False, names={"dwh": "dwh"})
        declared = SealedParamSchema().declared(offer)
        schema = {
            "type": "object",
            "properties": {"connection": {"type": "string", **declared}},
        }
        server = McpServerInfo(name="standard", features={})

        sent = await relay_stand.sealing.seal(
            "mcp__standard__query", {"connection": "dwh"}, schema, server
        )

        assert sent == {"connection": "dwh"}, sent


@dataclass(frozen=True)
class RelayCall:
    """Случай вызова через ретранслятор: инструмент, аргументы модели,
    ожидаемый ответ сервера — isError и куски его текста. endpoint — путь
    endpoint'а сервиса, roles — роли пользователя вкладки, owned — имена его
    соединений, logged — куски предупреждений ретранслятора."""

    name: str
    arguments: Mapping[str, Any]
    errored: bool
    expected: Sequence[str]
    endpoint: str
    roles: frozenset[str]
    owned: Sequence[str]
    logged: Sequence[str]


@pytest.mark.integration
class TestRelaySealing:
    """Ретранслятор запечатывает соединения пользователя через CallSealing:
    невыданное имя — отказ isError, выданное доходит до тела сервера. У
    инструмента со вложенными вызовами, объявленного сервером, соединения
    запечатываются в каждом узле; отказ узла — isError всего вызова с
    ключом узла, вызов не по объявленной форме уходит серверу как есть.
    Когда сервер предлагает и свои соединения (endpoint offers, роль
    analyst), имя соединения сервера уходит как есть, а совпавшее с именем
    соединения пользователя — запечатывается с предупреждением."""

    SCHEMA: ClassVar[str] = "relay_connections"
    ENVELOPE: ClassVar[dict[str, Any]] = TestMcpRelay.ENVELOPE

    @pytest.fixture
    async def store(self, pool: AsyncPostgresPool) -> ConnectionStore:
        cipher = SecretCipher(secrets.token_bytes(32))
        schema_dir = files("boba.connection_broker") / "schema"
        async with pool.connection() as conn:
            await PostgresSchema(self.SCHEMA).apply_files(conn, schema_dir, {})

        built = ConnectionStore(pool, self.SCHEMA, cipher, ConnectionTypes.discover())

        return built

    @pytest.mark.parametrize(
        "case",
        [
            RelayCall(
                name="fake_connection_host",
                arguments={"connection": "missing"},
                errored=True,
                expected=["is not among the connections"],
                endpoint="service",
                roles=frozenset({"dev"}),
                owned=("orders",),
                logged=(),
            ),
            RelayCall(
                name="fake_connection_host",
                arguments={"connection": "orders"},
                errored=False,
                expected=["db.local|orders"],
                endpoint="service",
                roles=frozenset({"dev"}),
                owned=("orders",),
                logged=(),
            ),
            RelayCall(
                name="workflow",
                arguments={
                    "nodes": [
                        {
                            "key": "n0",
                            "tool": "fake_connection_host",
                            "args": {"connection": "orders"},
                        }
                    ]
                },
                errored=False,
                expected=["db.local|orders"],
                endpoint="service",
                roles=frozenset({"dev"}),
                owned=("orders",),
                logged=(),
            ),
            RelayCall(
                name="workflow",
                arguments={
                    "nodes": [
                        {
                            "key": "n0",
                            "tool": "fake_connection_host",
                            "args": {"connection": "missing"},
                        }
                    ]
                },
                errored=True,
                expected=[
                    "node 'n0'",
                    "fake_connection_host",
                    "is not among the connections",
                ],
                endpoint="service",
                roles=frozenset({"dev"}),
                owned=("orders",),
                logged=(),
            ),
            RelayCall(
                name="workflow",
                arguments={
                    "nodes": [{"key": "n0", "tool": "no_such_tool", "args": {}}]
                },
                errored=True,
                expected=["node 'n0'", "offers the model no tool 'no_such_tool'"],
                endpoint="service",
                roles=frozenset({"dev"}),
                owned=("orders",),
                logged=(),
            ),
            RelayCall(
                name="workflow",
                arguments={"nodes": "n0"},
                errored=True,
                expected=["does not match its schema", "nodes"],
                endpoint="service",
                roles=frozenset({"dev"}),
                owned=("orders",),
                logged=(),
            ),
            RelayCall(
                name="fake_connection_host",
                arguments={"connection": "dwh"},
                errored=False,
                expected=["dwh.local|dwh"],
                endpoint="offers",
                roles=frozenset({"analyst"}),
                owned=(),
                logged=(),
            ),
            RelayCall(
                name="fake_connection_host",
                arguments={"connection": "dwh"},
                errored=False,
                expected=["db.local|orders"],
                endpoint="offers",
                roles=frozenset({"analyst"}),
                owned=("dwh",),
                logged=("connection dwh of kind postgres is both yours",),
            ),
        ],
        ids=[
            "refused",
            "sealed",
            "node-sealed",
            "node-refused",
            "node-unknown-tool",
            "off-shape-as-is",
            "server-name-as-is",
            "collision-user-wins",
        ],
    )
    async def test_sealing_is_applied_to_the_call(  # noqa: PLR0913 — случай собирается фикстурами стендов
        self,
        service_process: ServiceProcess,
        store: ConnectionStore,
        call_stand: CallStand,
        tab: TabSession,
        case: RelayCall,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        credentials = KerberosCredentialSource(None, NoRefresh())
        stand = RelayStand(lambda: store, lambda: credentials, call_stand.contexts)
        config = service_process.servers()
        boba = config.servers["boba"]
        endpoint = boba.endpoint.model_copy(update={"path": f"/mcp/{case.endpoint}"})
        served = boba.model_copy(update={"endpoint": endpoint})
        servers = McpServers(config.model_copy(update={"servers": {"boba": served}}))
        relay = stand.relay(servers)
        tab.password_sign_in(case.roles)
        holder = tab.holder(uuid4(), "general")
        server_id = stand.keys.register(holder, ["boba"])[0].server_id
        profile = PostgresConfig.model_validate(
            {
                "host": "db.local",
                "dbname": "orders",
                "auth": {"method": "trust", "user": "u"},
            }
        )
        for name in case.owned:
            await store.add_owned(name, profile, holder.user_id)
        heard: asyncio.Queue[McpMessage] = asyncio.Queue()
        listing = McpMessage(
            server_id=server_id,
            request_id="mcp:listing",
            method="tools/list",
            params={"_meta": self.ENVELOPE},
        )
        try:
            listed: McpOutcome | None = None
            for _ in range(150):
                try:
                    listed = await relay.answer(listing, heard.put)
                except RequestError:
                    await asyncio.sleep(0.2)
                    continue

                break

            assert isinstance(listed, McpResultOutcome), (
                f"the service stand did not list its tools within 30s: {listed}"
            )
            called = await relay.answer(
                McpMessage(
                    server_id=server_id,
                    request_id="mcp:call",
                    method="tools/call",
                    params={
                        "name": case.name,
                        "arguments": case.arguments,
                        "_meta": self.ENVELOPE,
                    },
                ),
                heard.put,
            )
        finally:
            await servers.stop()

        assert isinstance(called, McpResultOutcome), called
        answer = mt.CallToolResult.model_validate(called.result)
        assert answer.is_error is case.errored, answer
        for part in case.expected:
            assert part in str(answer.content), answer

        for part in case.logged:
            assert part in caplog.text, caplog.text

    async def test_thread_is_hidden_from_the_model_and_filled_by_the_relay(
        self,
        service_process: ServiceProcess,
        call_stand: CallStand,
        tab: TabSession,
    ) -> None:
        """Параметр треда агент в списке не видит, а вызов идёт в треде
        вкладки: ретранслятор ставит его в параметр с пометкой x-boba-thread."""
        credentials = KerberosCredentialSource(None, NoRefresh())
        stand = RelayStand(
            NoConnectionStore(), lambda: credentials, call_stand.contexts
        )
        servers = McpServers(service_process.servers())
        relay = stand.relay(servers)
        holder = tab.holder(uuid4(), "general")
        server_id = stand.keys.register(holder, ["boba"])[0].server_id
        heard: asyncio.Queue[McpMessage] = asyncio.Queue()
        listing = McpMessage(
            server_id=server_id,
            request_id="mcp:listing",
            method="tools/list",
            params={"_meta": self.ENVELOPE},
        )
        try:
            listed: McpOutcome | None = None
            for _ in range(150):
                try:
                    listed = await relay.answer(listing, heard.put)
                except RequestError:
                    await asyncio.sleep(0.2)
                    continue

                break

            called = await relay.answer(
                McpMessage(
                    server_id=server_id,
                    request_id="mcp:call",
                    method="tools/call",
                    params={
                        "name": "fake_scope",
                        "arguments": {},
                        "_meta": self.ENVELOPE,
                    },
                ),
                heard.put,
            )
        finally:
            await servers.stop()

        assert isinstance(listed, McpResultOutcome), listed
        tools = mt.ListToolsResult.model_validate(listed.result).tools
        scoped = next(tool for tool in tools if tool.name == "fake_scope")
        assert CallFields.THREAD not in scoped.input_schema["properties"], scoped
        assert isinstance(called, McpResultOutcome), called
        answer = mt.CallToolResult.model_validate(called.result)
        assert not answer.is_error, answer
        assert holder.thread_id in str(answer.content), answer
