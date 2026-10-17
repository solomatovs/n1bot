"""Ретранслятор mcp/message без агента: запросы MCP по serverId регистрации
исполняют настоящие серверы — обычный сервер на SDK mcp по stdio и стенд
boba-mcp со входом proxy. Проверяются строки таблицы ошибок MCP-over-ACP,
встроенные методы SDK, видимость инструментов для модели и приложения MCP
Apps, ход вызова и запечатывание соединений пользователя."""

from __future__ import annotations

import asyncio
import secrets
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, replace
from importlib.resources import files
from typing import Any, ClassVar
from uuid import uuid4

import mcp_types as mt
import pytest
from acp.exceptions import RequestError
from aid_stand import (
    HolderSurface,
    NoConnectionStore,
    RelayStand,
    ServiceProcess,
    TabSession,
)
from chainlit.context import context_var
from mcp.server.apps import APP_MIME_TYPE
from mcp.shared.exceptions import MCPError

from boba.aid.agent.relay import ChatTools, McpRegistration, McpRegistrations, McpRelay
from boba.aid.canvas.tools import CanvasOpener
from boba.auth.credentials import KerberosCredentialSource, NoRefresh
from boba.connection_broker.sealing import McpServerInfo
from boba.connection_broker.store import ConnectionStore
from boba.connections.manifest import ConnectionTypes
from boba.connections.sealed import ConnectionOffer, SealedParamSchema, SealFeature
from boba.connections.secrets import SecretCipher
from boba.connections.stored import GrantTarget
from boba.db.postgres import AsyncPostgresPool, PostgresSchema
from boba.db.postgres.connection import PostgresConfig
from boba.identity.context import DelegatedTicket, NoUserCredential, Scope
from boba.identity.errors import AuthenticationError, RefusalError
from boba.mcp_client.client import (
    McpServerApps,
    McpServerConfig,
    McpServers,
    McpServersConfig,
)
from boba.messaging import Envelope, JournalOpened
from boba.stand.refs import StandRefs
from boba.stand.signin import SignInStand
from boba.stand.toolstand import ProbeTools
from boba.stand_core.context import CallStand
from boba.toolkit.ports import StreamSpecs
from boba.toolkit.wire import ResultWire
from boba.toolrun.call_id import CallFields
from boba.toolrun.dev_null import DevNullTool
from boba.toolrun.hosted import ToolHosting
from boba.toolrun.stream_calls import WorkflowCall, WorkflowTool
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
    """Вкладка пользователя ivanov, вошедшего паролем с ролью, которой
    инструменты chat стенда ретранслятора видны (RelayStand.ROLE)."""
    session = TabSession()
    session.password_sign_in(frozenset({RelayStand.ROLE}))

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
        registration = tab.registration(uuid4(), SignInStand.PROFILE)

        return stand.registrations.register(registration, ["standard"])[0].server_id

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

    async def test_discover_is_the_server_behind_the_registration(
        self, relay: McpRelay, relay_stand: RelayStand, tab: TabSession
    ) -> None:
        """server/discover отдаёт возможности, инструкции и serverInfo
        сервера за регистрацией; ping и методы вне редакции SDK отвечает
        -32601."""
        server_id = self._server_id(relay_stand, tab)

        discovered, _ = await self._answer(
            relay, self._message(server_id, "server/discover", {})
        )
        pinged, _ = await self._answer(relay, self._message(server_id, "ping", {}))
        unknown, _ = await self._answer(
            relay, self._message(server_id, "logging/setLevel", {"level": "info"})
        )

        assert isinstance(discovered, McpResultOutcome), discovered
        result = mt.DiscoverResult.model_validate(discovered.result)
        assert McpVersion.V2026_07_28.value in result.supported_versions
        assert result.instructions, result
        assert result.meta is not None
        assert result.meta[mt.SERVER_INFO_META_KEY]["name"] == "standard"
        assert result.capabilities.prompts == mt.PromptsCapability(list_changed=True)
        assert result.capabilities.logging is None
        # ping в редакции 2026-07-28 не определён: SDK отвечает -32601
        assert isinstance(pinged, McpErrorOutcome), pinged
        assert pinged.error.code == mt.METHOD_NOT_FOUND
        assert isinstance(unknown, McpErrorOutcome), unknown
        assert unknown.error.code == mt.METHOD_NOT_FOUND

    async def test_app_only_tool_is_neither_listed_nor_called(
        self, relay: McpRelay, relay_stand: RelayStand, tab: TabSession
    ) -> None:
        server_id = self._server_id(relay_stand, tab)
        call = {"name": "app_only", "arguments": {}}

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
        assert "board" in names, names
        assert "model_only" in names, names
        assert "app_only" not in names, names
        assert isinstance(called, McpErrorOutcome), called
        assert called.error.code == mt.INVALID_PARAMS
        assert "'app_only'" in called.error.message

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

    async def test_shown_file_without_live_tabs_comes_back_as_is(
        self, relay: McpRelay, relay_stand: RelayStand, tab: TabSession
    ) -> None:
        """Файл, который сервер просит показать, открывать некому: у треда
        нет живых вкладок на этом инстансе — ответ агенту как есть."""
        registration = tab.registration(uuid4(), SignInStand.PROFILE)
        server_id = relay_stand.registrations.register(registration, ["standard"])[
            0
        ].server_id
        path = f"/workspace/{registration.thread_id}/upload/data.bin"
        shown = {"name": "show", "arguments": {"path": path, "mime": "x/unknown"}}

        outcome, _ = await self._answer(
            relay, self._message(server_id, "tools/call", shown)
        )

        assert isinstance(outcome, McpResultOutcome), outcome
        reply = mt.CallToolResult.model_validate(outcome.result)
        assert not reply.is_error, reply
        assert len(reply.content) == 2, reply

    async def test_refused_show_is_an_error_of_the_call(
        self,
        relay_stand: RelayStand,
        standard: McpServerConfig,
        tab: TabSession,
        call_stand: CallStand,
    ) -> None:
        """Панель живой вкладки не показала файл (вьювера для MIME нет):
        агент получает isError с причиной в том же вызове."""
        servers = McpServers(McpServersConfig(servers={"standard": standard}))
        canvas = CanvasOpener(HolderSurface(context_var.get()))
        relay = relay_stand.relay_with(servers, relay_stand.chat, canvas)
        registration = tab.registration(uuid4(), SignInStand.PROFILE)
        server_id = relay_stand.registrations.register(registration, ["standard"])[
            0
        ].server_id
        path = f"/workspace/{registration.thread_id}/upload/data.bin"
        shown = {"name": "show", "arguments": {"path": path, "mime": "x/unknown"}}
        try:
            outcome, _ = await self._answer(
                relay, self._message(server_id, "tools/call", shown)
            )
        finally:
            await servers.stop()

        assert isinstance(outcome, McpResultOutcome), outcome
        reply = mt.CallToolResult.model_validate(outcome.result)
        assert reply.is_error, reply
        refusal = reply.content[-1]
        assert isinstance(refusal, mt.TextContent), reply
        assert refusal.text.startswith(f"{path} is not shown in the canvas: "), refusal
        assert "x/unknown" in refusal.text, refusal

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


class TestAppRequests:
    """Запросы приложения MCP Apps: инструменты только своего сервера с "app"
    в visibility, ресурсы своего сервера; сервер без секции apps и сервер
    chat приложению отказывают, отказ уходит приложению ошибкой JSON-RPC."""

    SANDBOX: ClassVar[McpServerApps] = McpServerApps.model_validate(
        {"sandbox_url": "http://localhost:8123/mcp-apps/sandbox.html"}
    )

    @pytest.fixture
    async def relay(
        self, relay_stand: RelayStand, standard: McpServerConfig
    ) -> AsyncIterator[McpRelay]:
        drawn = standard.model_copy(update={"apps": self.SANDBOX})
        config = McpServersConfig(servers={"apps": drawn, "standard": standard})
        servers = McpServers(config)
        try:
            yield relay_stand.relay(servers)
        finally:
            await servers.stop()

    PROFILE_SERVERS: ClassVar[frozenset[str]] = frozenset({"apps", "standard"})
    """Серверы профиля треда регистрации приложения."""

    @staticmethod
    def _of(tab: TabSession, server: str, servers: frozenset[str]) -> McpRegistration:
        registration = tab.registration(uuid4(), SignInStand.PROFILE)

        return replace(registration, server=server, servers=servers)

    async def test_app_calls_the_tools_for_the_app(
        self, relay: McpRelay, tab: TabSession
    ) -> None:
        registration = self._of(tab, "apps", self.PROFILE_SERVERS)
        app_only = mt.CallToolRequestParams(name="app_only", arguments={})
        board = mt.CallToolRequestParams(name="board", arguments={"markup": "<i/>"})

        refreshed = await relay.app_call(registration, app_only)
        drawn = await relay.app_call(registration, board)

        assert not refreshed.is_error, refreshed
        assert refreshed.content == [mt.TextContent(text="app_only answered")]
        assert drawn.content == [mt.TextContent(text="the board is drawn by its app")]

    async def test_tool_for_the_model_is_refused_to_the_app(
        self, relay: McpRelay, tab: TabSession
    ) -> None:
        registration = self._of(tab, "apps", self.PROFILE_SERVERS)
        model_only = mt.CallToolRequestParams(name="model_only", arguments={})

        with pytest.raises(MCPError) as refused:
            await relay.app_call(registration, model_only)

        answered = await relay.app_answer(relay.app_call(registration, model_only))

        assert refused.value.error.code == mt.INVALID_PARAMS
        assert "offers the app no tool 'model_only'" in refused.value.error.message
        assert answered["error"]["code"] == mt.INVALID_PARAMS
        assert "'model_only'" in answered["error"]["message"]

    @pytest.mark.parametrize("server", ["standard", ChatTools.NAME])
    async def test_server_without_apps_refuses_the_app(
        self, relay: McpRelay, tab: TabSession, server: str
    ) -> None:
        registration = self._of(tab, server, self.PROFILE_SERVERS)
        board = mt.CallToolRequestParams(name="board", arguments={"markup": "<i/>"})
        read = mt.ReadResourceRequestParams(uri="ui://standard/board.html")

        with pytest.raises(MCPError) as called:
            await relay.app_call(registration, board)

        with pytest.raises(MCPError) as reading:
            await relay.app_read(registration, read)

        assert called.value.error.code == mt.INVALID_PARAMS
        assert f"{server!r}" in called.value.error.message
        assert reading.value.error.code == mt.INVALID_PARAMS

    async def test_server_outside_the_profile_of_the_thread_refuses_the_app(
        self, relay: McpRelay, tab: TabSession
    ) -> None:
        """Сервер с секцией apps, которого профиль треда не даёт: приложение
        и любой код вкладки его не зовут и не читают."""
        registration = self._of(tab, "apps", frozenset({"standard"}))
        board = mt.CallToolRequestParams(name="board", arguments={"markup": "<i/>"})
        read = mt.ReadResourceRequestParams(uri="ui://standard/board.html")

        with pytest.raises(MCPError) as called:
            await relay.app_call(registration, board)

        with pytest.raises(MCPError) as reading:
            await relay.app_read(registration, read)

        assert called.value.error.code == mt.INVALID_PARAMS
        assert "is not a server of profile" in called.value.error.message
        assert reading.value.error.code == mt.INVALID_PARAMS

    async def test_app_reads_the_resources_of_its_server(
        self, relay: McpRelay, tab: TabSession
    ) -> None:
        registration = self._of(tab, "apps", self.PROFILE_SERVERS)
        read = mt.ReadResourceRequestParams(uri="ui://standard/board.html")

        answered = await relay.app_answer(relay.app_read(registration, read))

        resource = mt.ReadResourceResult.model_validate(answered).contents[0]
        assert resource.mime_type == APP_MIME_TYPE
        assert answered["contents"][0]["_meta"]["ui"]["csp"] == {
            "resourceDomains": ["https://cdn.example.com"]
        }


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
    ключом узла, вызов не по объявленной форме уходит серверу как есть.
    Когда сервер предлагает и свои соединения (endpoint offers, роль
    analyst), имя соединения сервера уходит как есть, а совпавшее с именем
    соединения пользователя — запечатывается с предупреждением."""

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
                expected=[
                    "node 'n0'",
                    "offers the caller of the call no tool 'no_such_tool'",
                ],
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
        config = service_process.servers()
        answer = await self._called(config, store, call_stand, tab, ["orders"], case)

        assert answer.is_error is case.errored, answer
        for part in case.expected:
            assert part in str(answer.content), answer

    @pytest.mark.parametrize(
        ("owned", "logged", "case"),
        [
            (
                [],
                [],
                RelayCall(
                    name="fake_connection_host",
                    arguments={"connection": "dwh"},
                    errored=False,
                    expected=["dwh.local|dwh"],
                ),
            ),
            (
                ["dwh"],
                ["connection dwh of kind postgres is both yours"],
                RelayCall(
                    name="fake_connection_host",
                    arguments={"connection": "dwh"},
                    errored=False,
                    expected=["db.local|orders"],
                ),
            ),
        ],
        ids=["server-name-as-is", "collision-user-wins"],
    )
    async def test_server_connections_are_offered(  # noqa: PLR0913 — случай собирается фикстурами стендов
        self,
        service_process: ServiceProcess,
        store: ConnectionStore,
        call_stand: CallStand,
        tab: TabSession,
        owned: Sequence[str],
        logged: Sequence[str],
        case: RelayCall,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Endpoint offers предлагает роли analyst и соединение сервера dwh:
        имя сервера уходит как есть, совпавшее с именем соединения
        пользователя запечатывается с предупреждением."""
        config = service_process.servers()
        boba = config.servers["boba"]
        endpoint = boba.endpoint.model_copy(update={"path": "/mcp/offers"})
        served = boba.model_copy(update={"endpoint": endpoint})
        offers = config.model_copy(update={"servers": {"boba": served}})
        tab.password_sign_in(frozenset({"analyst"}))
        answer = await self._called(offers, store, call_stand, tab, owned, case)

        assert answer.is_error is case.errored, answer
        for part in case.expected:
            assert part in str(answer.content), answer

        for part in logged:
            assert part in caplog.text, caplog.text

    async def _called(  # noqa: PLR0913 — вызов собирается фикстурами стендов
        self,
        config: McpServersConfig,
        store: ConnectionStore,
        call_stand: CallStand,
        tab: TabSession,
        owned: Sequence[str],
        case: RelayCall,
    ) -> mt.CallToolResult:
        """Итог вызова case через ретранслятор к серверам config, когда у
        пользователя вкладки есть соединения owned; ждёт, пока стенд сервиса
        начнёт отдавать список инструментов."""
        credentials = KerberosCredentialSource(None, NoRefresh())
        stand = RelayStand(lambda: store, lambda: credentials, call_stand.contexts)
        servers = McpServers(config)
        relay = stand.relay(servers)
        registration = tab.registration(uuid4(), SignInStand.PROFILE)
        server_id = stand.registrations.register(registration, ["boba"])[0].server_id
        profile = PostgresConfig.model_validate(
            {
                "host": "db.local",
                "dbname": "orders",
                "auth": {"method": "trust", "user": "u"},
            }
        )
        owner = GrantTarget.user(registration.user_id)
        for name in owned:
            connection_id = await store.add(name, profile)
            await store.grant(connection_id, owner)

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

        return mt.CallToolResult.model_validate(called.result)

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
        registration = tab.registration(uuid4(), SignInStand.PROFILE)
        server_id = stand.registrations.register(registration, ["boba"])[0].server_id
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
        assert registration.thread_id in str(answer.content), answer


@pytest.mark.integration
class TestRelayJournals:
    """Журнал, который вызов агента открыл на сервере boba-mcp, ретранслятор
    узнаёт из resources/list по смене списка ресурсов (поток
    subscriptions/listen) и называет ленте треда сообщением JournalOpened
    под идентификатором вызова агента — его токеном прогресса; у связки —
    с ключом узла от автора."""

    ENVELOPE: ClassVar[dict[str, Any]] = TestMcpRelay.ENVELOPE

    @pytest.mark.parametrize(
        ("name", "arguments", "node"),
        [
            ("fake_progress", {"steps": 3, "pause": 0.5}, None),
            (
                "workflow",
                {
                    "nodes": [
                        {
                            "key": "slow",
                            "tool": "fake_progress",
                            "args": {"steps": 3, "pause": 0.5},
                        }
                    ]
                },
                "slow",
            ),
        ],
        ids=["call", "node"],
    )
    async def test_running_call_journal_is_named_to_the_thread(  # noqa: PLR0913 — случай собирается фикстурами стендов
        self,
        service_process: ServiceProcess,
        call_stand: CallStand,
        tab: TabSession,
        name: str,
        arguments: dict[str, Any],
        node: str | None,
    ) -> None:
        credentials = KerberosCredentialSource(None, NoRefresh())
        stand = RelayStand(
            NoConnectionStore(), lambda: credentials, call_stand.contexts
        )
        servers = McpServers(service_process.servers())
        relay = stand.relay(servers)
        registration = tab.registration(uuid4(), SignInStand.PROFILE)
        server_id = stand.registrations.register(registration, ["boba"])[0].server_id
        opened: list[JournalOpened] = []

        async def heard(envelope: Envelope) -> None:
            if isinstance(envelope.message, JournalOpened):
                opened.append(envelope.message)

        stand.bus.subscribe(Scope.chat(registration.thread_id), heard)
        notes: asyncio.Queue[McpMessage] = asyncio.Queue()
        meta = {**self.ENVELOPE, "progressToken": "agent-call-7"}
        try:
            for _ in range(150):
                try:
                    await relay.answer(
                        McpMessage(
                            server_id=server_id,
                            request_id=f"mcp:{uuid4().hex}",
                            method="tools/list",
                            params={"_meta": self.ENVELOPE},
                        ),
                        notes.put,
                    )
                except RequestError:
                    await asyncio.sleep(0.2)
                    continue

                break

            called = await relay.answer(
                McpMessage(
                    server_id=server_id,
                    request_id="mcp:call",
                    method="tools/call",
                    params={"name": name, "arguments": arguments, "_meta": meta},
                ),
                notes.put,
            )
        finally:
            await servers.stop()

        assert isinstance(called, McpResultOutcome), called
        answer = mt.CallToolResult.model_validate(called.result)
        assert not answer.is_error, answer
        assert len(opened) == 1, opened
        journal = opened[0]
        assert journal.call_id == "agent-call-7"
        assert journal.server == "boba"
        assert journal.node == node
        address = f"//{journal.run}/{journal.call}/"
        linked: list[str] = []
        for block in answer.content:
            if not isinstance(block, mt.ResourceLink):
                continue

            if address in str(block.uri):
                linked.append(str(block.uri))

        assert linked, (answer.content, journal)


@pytest.mark.integration
class TestChatServer:
    """Сервер chat в процессе чата через ретранслятор: собственные
    инструменты чата по ролям вкладки, connection_list отдаёт соединения,
    выданные пользователю регистрации; совпавшие имена инструментов —
    ошибка доставки без исхода MCP."""

    ENVELOPE: ClassVar[dict[str, Any]] = TestMcpRelay.ENVELOPE

    async def test_own_tools_are_listed_and_called_in_process(
        self, store: ConnectionStore, call_stand: CallStand, tab: TabSession
    ) -> None:
        credentials = KerberosCredentialSource(None, NoRefresh())
        stand = RelayStand(lambda: store, lambda: credentials, call_stand.contexts)
        relay = stand.relay(McpServers(McpServersConfig(servers={})))
        registration = tab.registration(uuid4(), SignInStand.PROFILE)
        server_id = stand.registrations.register(registration, [ChatTools.NAME])[
            0
        ].server_id
        profile = PostgresConfig.model_validate(
            {
                "host": "db.local",
                "dbname": "orders",
                "auth": {"method": "trust", "user": "u"},
            }
        )
        connection_id = await store.add("orders", profile)
        await store.grant(connection_id, GrantTarget.user(registration.user_id))

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
        assert answer.meta is not None, "the step name keeps the call duration"
        assert isinstance(answer.meta.get(ResultWire.ELAPSED), int), answer.meta

    async def test_colliding_tool_names_are_an_external_error(
        self, call_stand: CallStand, tab: TabSession
    ) -> None:
        refs = StandRefs(call_stand.contexts)
        drain = DevNullTool(refs.ambient)
        tools = refs.tool_stand(())
        tools.adopt(
            [
                ToolHosting().hosted(drain.build()),
                ProbeTools().recorder(WorkflowTool.NAME, WorkflowCall),
            ]
        )
        registry = tools.registry(
            tools.access(RelayStand.ROLE, SignInStand.PROFILE), ()
        )
        streaming = StreamSpecs({DevNullTool.NAME: drain.spec()})
        own = frozenset({WorkflowTool.NAME})
        chat = ChatTools(
            replace(registry, own=own, specs=streaming), refs.runs, RelayStand.VERSION
        )
        credentials = KerberosCredentialSource(None, NoRefresh())
        stand = RelayStand(
            NoConnectionStore(), lambda: credentials, call_stand.contexts
        )
        servers = McpServers(McpServersConfig(servers={}))
        relay = stand.relay_with(servers, chat, stand.canvas)
        registration = tab.registration(uuid4(), SignInStand.PROFILE)
        server_id = stand.registrations.register(registration, [ChatTools.NAME])[
            0
        ].server_id
        message = McpMessage(
            server_id=server_id,
            request_id="mcp:listing",
            method="tools/list",
            params={"_meta": self.ENVELOPE},
        )

        with pytest.raises(RequestError) as refused:
            await relay.answer(message, asyncio.Queue[McpMessage]().put)

        assert refused.value.code == AcpErrorCode.BACKEND_FAILED
        assert f"tool {WorkflowTool.NAME!r} is offered by two servers" in str(
            refused.value
        )


class TestRegistrations:
    """Регистрации серверов процесса агента: свой serverId на каждый сервер
    до снятия, профиль треда в субъекте и актуальность входа пользователя —
    роли и делегированный билет kerberos читаются из живой сессии в момент
    вызова."""

    async def test_each_server_gets_its_own_server_id_until_revoked(self) -> None:
        registrations = McpRegistrations()
        registration = TabSession().registration(uuid4(), SignInStand.PROFILE)

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
        server = registrations.register(tab.registration(user_id, "search"), ["chat"])[
            0
        ]

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
        server = registrations.register(
            tab.registration(uuid4(), SignInStand.PROFILE), ["chat"]
        )[0]
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
        registration = TabSession().registration(uuid4(), SignInStand.PROFILE)
        server = registrations.register(registration, ["chat"])[0]

        with pytest.raises(AuthenticationError, match="no sign-in token"):
            registrations.of(server.server_id).context()
