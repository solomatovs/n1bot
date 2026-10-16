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

from boba.aid.agent.relay import ChatTools, McpRegistrations, McpRelay
from boba.auth.credentials import KerberosCredentialSource, NoRefresh
from boba.connection_broker.sealing import McpServerInfo
from boba.connection_broker.store import ConnectionStore
from boba.connections.manifest import ConnectionTypes
from boba.connections.sealed import SealedParamSchema, SealFeature
from boba.connections.secrets import SecretCipher
from boba.db.postgres import AsyncPostgresPool, PostgresSchema
from boba.db.postgres.connection import PostgresConfig
from boba.identity.context import DelegatedTicket, NoUserCredential
from boba.identity.errors import AuthenticationError, RefusalError
from boba.mcp_client.client import McpServerConfig, McpServers, McpServersConfig
from boba.stand_core.context import CallStand
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

        return stand.registrations.register(holder, ["standard"])[0].server_id

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
        self, relay_stand: RelayStand, tab: TabSession
    ) -> None:
        server_id = self._server_id(relay_stand, tab)
        context = relay_stand.registrations.of(server_id).context()
        declared = SealedParamSchema().declared("postgres")
        schema = {
            "type": "object",
            "properties": {"connection": {"type": "string", **declared}},
        }
        server = McpServerInfo(name="standard", features={})

        with pytest.raises(RefusalError) as refused:
            await relay_stand.sealing.seal(
                "mcp__standard__query",
                {"connection": "orders"},
                schema,
                server,
                context,
            )

        assert SealFeature.ID in str(refused.value)
        assert "'standard'" in str(refused.value)


@pytest.fixture
async def store(pool: AsyncPostgresPool) -> ConnectionStore:
    """Хранилище соединений пользователей стенда ретранслятора."""
    schema = "relay_connections"
    cipher = SecretCipher(secrets.token_bytes(32))
    schema_dir = files("boba.connection_broker") / "schema"
    async with pool.connection() as conn:
        await PostgresSchema(schema).apply_files(conn, schema_dir, {})

    return ConnectionStore(pool, schema, cipher, ConnectionTypes.discover())


@dataclass(frozen=True)
class RelayCall:
    """Случай вызова через ретранслятор: инструмент, аргументы модели и
    ожидаемый ответ сервера — isError и куски его текста."""

    name: str
    arguments: Mapping[str, Any]
    errored: bool
    expected: Sequence[str]


@pytest.mark.integration
class TestRelaySealing:
    """Ретранслятор запечатывает соединения пользователя через CallSealing:
    невыданное имя — отказ isError, выданное доходит до тела сервера. У
    инструмента со вложенными вызовами, объявленного сервером, соединения
    запечатываются в каждом узле; отказ узла — isError всего вызова с
    ключом узла, вызов не по объявленной форме уходит серверу как есть."""

    ENVELOPE: ClassVar[dict[str, Any]] = TestMcpRelay.ENVELOPE

    @pytest.mark.parametrize(
        "case",
        [
            RelayCall(
                name="fake_connection_host",
                arguments={"connection": "missing"},
                errored=True,
                expected=["is not among the connections"],
            ),
            RelayCall(
                name="fake_connection_host",
                arguments={"connection": "orders"},
                errored=False,
                expected=["db.local|orders"],
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
            ),
            RelayCall(
                name="workflow",
                arguments={
                    "nodes": [{"key": "n0", "tool": "no_such_tool", "args": {}}]
                },
                errored=True,
                expected=["node 'n0'", "offers the model no tool 'no_such_tool'"],
            ),
            RelayCall(
                name="workflow",
                arguments={"nodes": "n0"},
                errored=True,
                expected=["does not match its schema", "nodes"],
            ),
        ],
        ids=[
            "refused",
            "sealed",
            "node-sealed",
            "node-refused",
            "node-unknown-tool",
            "off-shape-as-is",
        ],
    )
    async def test_sealing_is_applied_to_the_call(
        self,
        service_process: ServiceProcess,
        store: ConnectionStore,
        call_stand: CallStand,
        tab: TabSession,
        case: RelayCall,
    ) -> None:
        credentials = KerberosCredentialSource(None, NoRefresh())
        stand = RelayStand(lambda: store, lambda: credentials, call_stand.contexts)
        servers = McpServers(service_process.servers())
        relay = stand.relay(servers)
        holder = tab.holder(uuid4(), "general")
        server_id = stand.registrations.register(holder, ["boba"])[0].server_id
        profile = PostgresConfig.model_validate(
            {
                "host": "db.local",
                "dbname": "orders",
                "auth": {"method": "trust", "user": "u"},
            }
        )
        await store.add_owned("orders", profile, holder.user_id)
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


@pytest.mark.integration
class TestChatServer:
    """Сервер chat в процессе чата через ретранслятор: собственные
    инструменты чата по ролям вкладки, connection_list отдаёт соединения,
    выданные пользователю регистрации."""

    ENVELOPE: ClassVar[dict[str, Any]] = TestMcpRelay.ENVELOPE

    async def test_own_tools_are_listed_and_called_in_process(
        self, store: ConnectionStore, call_stand: CallStand, tab: TabSession
    ) -> None:
        credentials = KerberosCredentialSource(None, NoRefresh())
        stand = RelayStand(lambda: store, lambda: credentials, call_stand.contexts)
        relay = stand.relay(McpServers(McpServersConfig(servers={})))
        holder = tab.holder(uuid4(), RelayStand.PROFILE)
        server_id = stand.registrations.register(holder, [ChatTools.NAME])[0].server_id
        profile = PostgresConfig.model_validate(
            {
                "host": "db.local",
                "dbname": "orders",
                "auth": {"method": "trust", "user": "u"},
            }
        )
        await store.add_owned("orders", profile, holder.user_id)
        heard: asyncio.Queue[McpMessage] = asyncio.Queue()

        listed = await relay.answer(
            McpMessage(
                server_id=server_id,
                request_id="mcp:listing",
                method="tools/list",
                params={"_meta": self.ENVELOPE},
            ),
            heard.put,
        )
        called = await relay.answer(
            McpMessage(
                server_id=server_id,
                request_id="mcp:call",
                method="tools/call",
                params={
                    "name": "connection_list",
                    "arguments": {},
                    "_meta": self.ENVELOPE,
                },
            ),
            heard.put,
        )

        assert isinstance(listed, McpResultOutcome), listed
        names: list[str] = []
        for tool in mt.ListToolsResult.model_validate(listed.result).tools:
            names.append(tool.name)

        assert sorted(names) == ["connection_list", "connection_search"], names
        assert isinstance(called, McpResultOutcome), called
        answer = mt.CallToolResult.model_validate(called.result)
        assert answer.is_error is False, answer
        assert "orders" in str(answer.content), answer


class TestRegistrations:
    """Регистрации серверов процесса агента: свой serverId на каждый сервер
    до снятия, профиль треда в субъекте и актуальность входа пользователя —
    роли и делегированный билет kerberos читаются из живой сессии в момент
    вызова."""

    async def test_each_server_gets_its_own_server_id_until_revoked(self) -> None:
        registrations = McpRegistrations()
        registration = TabSession().holder(uuid4(), "general")

        servers = registrations.register(registration, ["boba_general", "chat"])
        ids: list[str] = []
        for server in servers:
            ids.append(server.server_id)

        names: list[str] = []
        kinds: list[str] = []
        for server in servers:
            names.append(server.name)
            kinds.append(server.type)

        assert names == ["boba_general", "chat"]
        assert kinds == ["acp", "acp"]
        assert len(set(ids)) == 2
        for server in servers:
            registered = registrations.of(server.server_id)
            assert registered.server == server.name
            assert registered.thread_id == registration.thread_id

        registrations.revoke(ids[0])
        with pytest.raises(RequestError) as revoked:
            registrations.of(ids[0])

        assert revoked.value.code == AcpErrorCode.UNKNOWN_SERVER
        assert registrations.of(ids[1]).server == "chat"

    async def test_subject_takes_the_profile_of_the_thread(
        self, auth_token: str
    ) -> None:
        registrations = McpRegistrations()
        tab = TabSession()
        tab.password_sign_in(frozenset({"DEV"}))
        user_id = uuid4()
        server = registrations.register(tab.holder(user_id, "search"), ["chat"])[0]

        context = registrations.of(server.server_id).context()

        assert context.subject.user_id == user_id
        assert context.subject.login == TabSession.LOGIN
        assert context.subject.roles == frozenset({"DEV"})
        assert context.subject.profile == "search"
        assert isinstance(context.credential, NoUserCredential)

    async def test_credential_follows_the_renewed_session_token(
        self, auth_token: str
    ) -> None:
        registrations = McpRegistrations()
        tab = TabSession()
        tab.sign_in(frozenset({"DEV"}), "sealed-ticket-at-launch")
        server = registrations.register(tab.holder(uuid4(), "general"), ["chat"])[0]
        registration = registrations.of(server.server_id)
        launched = registration.context()

        tab.sign_in(frozenset({"DEV", "ADM"}), "sealed-ticket-renewed")
        renewed = registration.context()

        assert isinstance(launched.credential, DelegatedTicket)
        assert launched.credential.sealed == "sealed-ticket-at-launch"
        assert isinstance(renewed.credential, DelegatedTicket)
        assert renewed.credential.sealed == "sealed-ticket-renewed"
        assert renewed.credential.principal == TabSession.PRINCIPAL
        assert renewed.subject.roles == frozenset({"DEV", "ADM"})

    async def test_registration_without_a_sign_in_token_is_refused(self) -> None:
        registrations = McpRegistrations()
        registration = TabSession().holder(uuid4(), "general")
        server = registrations.register(registration, ["chat"])[0]

        with pytest.raises(AuthenticationError, match="no sign-in token"):
            registrations.of(server.server_id).context()
