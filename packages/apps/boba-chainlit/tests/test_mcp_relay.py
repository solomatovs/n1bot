"""Ретранслятор mcp/message без агента: запросы MCP по serverId регистрации
исполняют настоящие серверы — обычный сервер на SDK mcp по stdio и стенд
boba-mcp со входом proxy. Проверяются строки таблицы ошибок MCP-over-ACP,
встроенные методы SDK, видимость инструментов для модели, ход вызова и
запечатывание соединений пользователя."""

from __future__ import annotations

import asyncio
import base64
import secrets
import sys
from collections.abc import AsyncIterator, Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any, ClassVar
from uuid import uuid4

import mcp_types as mt
import pytest
from acp.exceptions import RequestError
from chainlit_stand import NoConnectionStore, ServiceProcess, TabSession
from pydantic import SecretStr

from boba.auth.credentials import KerberosCredentialSource, NoRefresh
from boba.chainlit.agent.config import ChatMcpConfig
from boba.chainlit.agent.relay import McpRelay
from boba.chainlit.agent.server import ChatCallers, HookSealing, ProcessKeys
from boba.connection_broker.store import ConnectionsConfig, ConnectionStore
from boba.connection_broker.user_connections import ArmedConnections
from boba.connections.manifest import ConnectionTypes
from boba.db.postgres import AsyncPostgresPool
from boba.db.postgres.connection import PostgresConfig
from boba.mcp_client.client import (
    McpServerConfig,
    McpServers,
    McpServersConfig,
    StdioCommand,
)
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

STANDARD = (
    Path(__file__).resolve().parents[3]
    / "services"
    / "boba-mcp-client"
    / "tests"
    / "standard_server.py"
)


@pytest.fixture
def tab(auth_token: str) -> TabSession:
    """Вкладка пользователя ivanov, вошедшего паролем с ролью dev."""
    session = TabSession()
    session.password_sign_in(frozenset({"dev"}))

    return session


@pytest.fixture
def standard() -> McpServersConfig:
    """Обычный сервер стенда клиента MCP по stdio под именем standard."""
    endpoint = StdioCommand(command=sys.executable, args=(str(STANDARD), "stdio"))
    server = McpServerConfig(
        endpoint=endpoint, connect_timeout_sec=20.0, call_timeout_sec=5.0
    )

    return McpServersConfig(servers={"standard": server})


@pytest.fixture(scope="module")
def boba_service(tmp_path_factory: pytest.TempPathFactory) -> Iterator[ServiceProcess]:
    """Стенд boba-mcp отдельным процессом: вход proxy, инструменты-заглушки."""
    process = ServiceProcess(tmp_path_factory.mktemp("boba-mcp"))
    try:
        process.await_listening()
        yield process
    finally:
        process.stop()


class TestMcpRelay:
    """Ретранслятор над обычным сервером: таблица ошибок, встроенные
    методы SDK, видимость инструментов и ход вызова."""

    CHAT: ClassVar[ChatMcpConfig] = ChatMcpConfig(
        enable=False,
        name="chat",
        path="/chat/mcp",
        connect_timeout_sec=5.0,
        call_timeout_sec=5.0,
    )
    ENVELOPE: ClassVar[dict[str, Any]] = {
        mt.PROTOCOL_VERSION_META_KEY: McpVersion.V2026_07_28.value,
        mt.CLIENT_CAPABILITIES_META_KEY: {},
    }

    @pytest.fixture
    def keys(self) -> ProcessKeys:
        return ProcessKeys()

    @pytest.fixture
    async def relay(
        self, keys: ProcessKeys, standard: McpServersConfig, call_stand: CallStand
    ) -> AsyncIterator[McpRelay]:
        servers = McpServers(standard)
        callers = ChatCallers(keys)
        credentials = KerberosCredentialSource(None, NoRefresh())
        connections = ArmedConnections(
            NoConnectionStore(), lambda: credentials, call_stand.contexts
        )
        sealing = HookSealing(
            callers, connections, call_stand.contexts, timedelta(minutes=2)
        )
        try:
            yield McpRelay(
                keys, callers, servers, sealing, self.CHAT, "http://127.0.0.1"
            )
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

    def _server_id(self, keys: ProcessKeys, tab: TabSession) -> str:
        holder = tab.holder(uuid4(), "general")

        return keys.register(holder, ["standard"])[0].server_id

    async def test_unknown_server_id_is_an_external_error(
        self, relay: McpRelay
    ) -> None:
        message = self._message("ghost", "tools/list", {})

        with pytest.raises(RequestError) as refused:
            await relay.answer(message, asyncio.Queue[McpMessage]().put)

        assert refused.value.code == AcpErrorCode.UNKNOWN_SERVER
        assert "'ghost'" in str(refused.value)

    async def test_request_id_in_work_is_an_external_error(
        self, relay: McpRelay, keys: ProcessKeys, tab: TabSession
    ) -> None:
        server_id = self._server_id(keys, tab)
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
        self, relay: McpRelay, keys: ProcessKeys, tab: TabSession
    ) -> None:
        server_id = self._server_id(keys, tab)
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
        self, relay: McpRelay, keys: ProcessKeys, tab: TabSession
    ) -> None:
        server_id = self._server_id(keys, tab)

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
        self, relay: McpRelay, keys: ProcessKeys, tab: TabSession
    ) -> None:
        server_id = self._server_id(keys, tab)
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
        self, relay: McpRelay, keys: ProcessKeys, tab: TabSession
    ) -> None:
        server_id = self._server_id(keys, tab)
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
        self, relay: McpRelay, keys: ProcessKeys, tab: TabSession
    ) -> None:
        server_id = self._server_id(keys, tab)
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
        self, relay: McpRelay, keys: ProcessKeys, tab: TabSession
    ) -> None:
        server_id = self._server_id(keys, tab)
        message = self._message(server_id, "tools/call", {"name": "die"})

        with pytest.raises(RequestError) as refused:
            await relay.answer(message, asyncio.Queue[McpMessage]().put)

        assert refused.value.code == AcpErrorCode.BACKEND_FAILED
        assert "'die'" in str(refused.value)


@pytest.mark.integration
class TestRelaySealing:
    """Ретранслятор запечатывает соединения пользователя через HookSealing:
    невыданное имя — отказ isError, выданное доходит до тела сервера."""

    SCHEMA: ClassVar[str] = "relay_connections"
    ENVELOPE: ClassVar[dict[str, Any]] = TestMcpRelay.ENVELOPE

    @pytest.fixture
    async def store(self, pool: AsyncPostgresPool) -> ConnectionStore:
        key = SecretStr(base64.b64encode(secrets.token_bytes(32)).decode())
        config = ConnectionsConfig(
            enable=True, db_schema=self.SCHEMA, encryption_key=key
        )
        built = ConnectionStore(config, ConnectionTypes.discover(), pool)
        await built.setup()

        return built

    async def test_sealing_is_applied_to_the_call(
        self,
        boba_service: ServiceProcess,
        store: ConnectionStore,
        call_stand: CallStand,
        tab: TabSession,
    ) -> None:
        servers = McpServers(boba_service.servers())
        keys = ProcessKeys()
        callers = ChatCallers(keys)
        credentials = KerberosCredentialSource(None, NoRefresh())
        connections = ArmedConnections(
            lambda: store, lambda: credentials, call_stand.contexts
        )
        sealing = HookSealing(
            callers, connections, call_stand.contexts, timedelta(minutes=2)
        )
        relay = McpRelay(
            keys, callers, servers, sealing, TestMcpRelay.CHAT, "http://127.0.0.1"
        )
        holder = tab.holder(uuid4(), "general")
        server_id = keys.register(holder, ["boba"])[0].server_id
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
            for _ in range(150):
                try:
                    await relay.answer(listing, heard.put)
                except RequestError:
                    await asyncio.sleep(0.2)
                    continue

                break

            refused = await relay.answer(
                McpMessage(
                    server_id=server_id,
                    request_id="mcp:refused",
                    method="tools/call",
                    params={
                        "name": "fake_connection_host",
                        "arguments": {"connection": "missing"},
                        "_meta": self.ENVELOPE,
                    },
                ),
                heard.put,
            )
            sealed = await relay.answer(
                McpMessage(
                    server_id=server_id,
                    request_id="mcp:sealed",
                    method="tools/call",
                    params={
                        "name": "fake_connection_host",
                        "arguments": {"connection": "orders"},
                        "_meta": self.ENVELOPE,
                    },
                ),
                heard.put,
            )
        finally:
            await servers.stop()

        assert isinstance(refused, McpResultOutcome), refused
        denial = mt.CallToolResult.model_validate(refused.result)
        assert denial.is_error, denial
        assert "is not among the connections" in str(denial.content), denial
        assert isinstance(sealed, McpResultOutcome), sealed
        answer = mt.CallToolResult.model_validate(sealed.result)
        assert not answer.is_error, answer
        assert "db.local|orders" in str(answer.content), answer
