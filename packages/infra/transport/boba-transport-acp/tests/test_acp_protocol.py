"""Провод ACP v2 на соединении SDK; кадры пары сверяются со схемой ACP."""

from __future__ import annotations

import asyncio
import importlib.metadata
import json
import socket
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, ClassVar

import pytest
from acp._transport import memory_transport_pair
from acp.connection import Connection, JsonValue
from acp.exceptions import RequestError
from acp.experimental.v2 import Agent, AgentSideConnection, Client, schema
from acp.experimental.v2._router import MethodRouter
from acp.experimental.v2.meta import AGENT_METHODS, CLIENT_METHODS
from mcp_types.jsonrpc import ErrorData
from mcp_types.version import MODERN_PROTOCOL_VERSIONS

from boba.stand.acp_frames import Frames
from boba.transport.acp.protocol import (
    AcpErrorCode,
    AcpVersion,
    AgentSide,
    BatchTransport,
    ClientConnection,
    McpErrorOutcome,
    McpMessage,
    McpOutcome,
    McpResultOutcome,
    McpVersion,
    ToolStatus,
)

pytestmark = pytest.mark.anyio


class StandAgent(Agent):
    """Агент стенда с одним initialize; создаёт его фабрика AgentSide."""

    def __init__(self, side: AgentSide, connection: AgentSideConnection) -> None:
        return

    async def initialize(
        self,
        protocol_version: int,
        info: schema.Implementation,
        capabilities: schema.ClientCapabilities | None = None,
        **kwargs: Any,
    ) -> schema.InitializeResponse:
        return schema.InitializeResponse(
            protocol_version=AcpVersion.V2,
            info=schema.Implementation(name="stand-agent", version="0"),
        )


class StandClient(Client):
    """Клиент стенда: копит обновления, отвечает на mcp/message из replies и
    отмечает отмену ответа в cancelled."""

    def __init__(self) -> None:
        self.updates: asyncio.Queue[object] = asyncio.Queue()
        self.questions: asyncio.Queue[McpMessage] = asyncio.Queue()
        self.replies: asyncio.Queue[McpOutcome | RequestError] = asyncio.Queue()
        self.cancelled = asyncio.Event()

    async def session_update(
        self, session_id: str, update: object, **kwargs: Any
    ) -> None:
        await self.updates.put(update)

    # SDK считает метод протокола неподдержанным
    request_permission = Client.request_permission
    connect_mcp = Client.connect_mcp
    mcp_message = Client.mcp_message
    notify_mcp = Client.notify_mcp
    disconnect_mcp = Client.disconnect_mcp
    create_elicitation = Client.create_elicitation
    complete_elicitation = Client.complete_elicitation

    async def answer(self, message: McpMessage) -> McpOutcome:
        await self.questions.put(message)

        try:
            reply = await self.replies.get()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise

        if isinstance(reply, RequestError):
            raise reply

        return reply


@dataclass(frozen=True)
class AcpPair:
    """Агент и клиент, связанные одним соединением и прошедшие initialize."""

    agent: AgentSide
    client: ClientConnection
    stand_client: StandClient


@pytest.fixture
async def acp_pair() -> AsyncIterator[AcpPair]:
    agent_end, client_end = memory_transport_pair()
    info = schema.Implementation(name="boba-transport-acp-tests", version="0")
    stand_client = StandClient()
    frames = Frames()

    agent = AgentSide(StandAgent, info, agent_end)
    serving = asyncio.create_task(agent.serve())

    client = ClientConnection(
        stand_client, stand_client.answer, client_end, None, observers=[frames]
    )
    await client.initialize(protocol_version=AcpVersion.V2, info=info)

    yield AcpPair(agent, client, stand_client)

    await client.close()
    await serving

    frames.check_schema()


@pytest.fixture
async def batch_wire() -> AsyncIterator[
    tuple[asyncio.StreamReader, asyncio.StreamWriter]
]:
    """Сторона клиента сокета, на другом конце которого AgentSide на BatchTransport."""
    agent_socket, client_socket = socket.socketpair()
    agent_reader, agent_writer = await asyncio.open_connection(sock=agent_socket)
    client_reader, client_writer = await asyncio.open_connection(sock=client_socket)
    info = schema.Implementation(name="stand-agent", version="0")
    agent = AgentSide(StandAgent, info, BatchTransport(agent_reader, agent_writer))
    serving = asyncio.create_task(agent.serve())

    yield client_reader, client_writer

    client_writer.close()
    await client_writer.wait_closed()
    async with asyncio.timeout(5):
        await serving

    agent_writer.close()
    await agent_writer.wait_closed()


class TestBatches:
    INITIALIZE: ClassVar[dict[str, Any]] = {
        "jsonrpc": "2.0",
        "method": AGENT_METHODS["initialize"],
        "params": {
            "protocolVersion": AcpVersion.V2,
            "info": {"name": "batch-client", "version": "0"},
        },
    }

    async def test_two_requests_get_one_array(
        self, batch_wire: tuple[asyncio.StreamReader, asyncio.StreamWriter]
    ) -> None:
        reader, writer = batch_wire
        batch = [
            {**self.INITIALIZE, "id": 1},
            {"jsonrpc": "2.0", "id": 2, "method": AGENT_METHODS["session_list"]},
        ]

        writer.write(json.dumps(batch).encode() + b"\n")
        async with asyncio.timeout(5):
            answer = json.loads(await reader.readline())

        assert isinstance(answer, list)
        ids = sorted(reply["id"] for reply in answer)
        assert ids == [1, 2]
        (initialized,) = [reply for reply in answer if reply["id"] == 1]
        assert initialized["result"]["protocolVersion"] == AcpVersion.V2

    async def test_empty_batch_is_one_invalid_request(
        self, batch_wire: tuple[asyncio.StreamReader, asyncio.StreamWriter]
    ) -> None:
        reader, writer = batch_wire

        writer.write(b"[]\n")
        async with asyncio.timeout(5):
            answer = json.loads(await reader.readline())

        assert answer["id"] is None
        assert answer["error"]["code"] == -32600

    async def test_notifications_only_get_nothing(
        self, batch_wire: tuple[asyncio.StreamReader, asyncio.StreamWriter]
    ) -> None:
        reader, writer = batch_wire
        batch = [
            {
                "jsonrpc": "2.0",
                "method": "$/cancel_request",
                "params": {"requestId": 7},
            },
            {"jsonrpc": "2.0", "method": "_boba/unknown", "params": {}},
        ]

        writer.write(json.dumps(batch).encode() + b"\n")
        writer.write(json.dumps({**self.INITIALIZE, "id": 3}).encode() + b"\n")
        async with asyncio.timeout(5):
            answer = json.loads(await reader.readline())

        assert answer["id"] == 3
        assert answer["result"]["protocolVersion"] == AcpVersion.V2

    async def test_scalar_entries_get_invalid_request_each(
        self, batch_wire: tuple[asyncio.StreamReader, asyncio.StreamWriter]
    ) -> None:
        reader, writer = batch_wire

        writer.write(b"[17,true,null]\n")
        async with asyncio.timeout(5):
            answer = json.loads(await reader.readline())

        assert isinstance(answer, list)
        assert len(answer) == 3
        for reply in answer:
            assert reply["id"] is None
            assert reply["error"]["code"] == -32600

    async def test_broken_json_is_parse_error(
        self, batch_wire: tuple[asyncio.StreamReader, asyncio.StreamWriter]
    ) -> None:
        reader, writer = batch_wire

        writer.write(b'{"jsonrpc": "2.0", "method"\n')
        async with asyncio.timeout(5):
            answer = json.loads(await reader.readline())

        assert answer["id"] is None
        assert answer["error"]["code"] == -32700


class TestVersionNegotiation:
    async def _reply_version_3(
        self, method: str, params: JsonValue | None, is_notification: bool
    ) -> schema.InitializeResponse:
        return schema.InitializeResponse(
            protocol_version=3, info=schema.Implementation(name="future", version="3")
        )

    @pytest.mark.parametrize(
        "params",
        [
            {"protocolVersion": 3, "info": {"name": "future", "version": "3"}},
            {
                "protocolVersion": 1,
                "clientInfo": {"name": "legacy", "version": "1"},
                "clientCapabilities": {},
            },
        ],
        ids=["foreign-version", "v1-handshake"],
    )
    async def test_agent_answers_its_own_version(self, params: dict[str, Any]) -> None:
        agent_end, client_end = memory_transport_pair()
        info = schema.Implementation(name="stand-agent", version="0")
        agent = AgentSide(StandAgent, info, agent_end)
        serving = asyncio.create_task(agent.serve())

        async with Connection(self._reply_version_3, client_end) as raw:
            answer = await raw.send_request(AGENT_METHODS["initialize"], params)

        await serving

        assert answer["protocolVersion"] == AcpVersion.V2
        assert answer["info"] == {"name": "stand-agent", "version": "0"}

    async def test_client_closes_on_foreign_version(self) -> None:
        agent_end, client_end = memory_transport_pair()
        stand_client = StandClient()
        raw = Connection(self._reply_version_3, agent_end, listening=False)
        reading = asyncio.create_task(raw.main_loop())

        client = ClientConnection(stand_client, stand_client.answer, client_end, None)
        info = schema.Implementation(name="boba-transport-acp-tests", version="0")
        with pytest.raises(RequestError):
            await client.initialize(protocol_version=AcpVersion.V2, info=info)

        async with asyncio.timeout(5):
            await reading

        await raw.close()


class TestMcpMessage:
    CALL: ClassVar[McpMessage] = McpMessage(
        server_id="aid_all",
        request_id="mcp-1",
        method="tools/call",
        params={"name": "pg_query", "arguments": {"sql": "select 1"}},
    )

    @pytest.mark.parametrize(
        "outcome",
        [
            McpResultOutcome(result={"content": [{"type": "text", "text": "1"}]}),
            McpResultOutcome(result=None),
            McpErrorOutcome(error=ErrorData(code=-32602, message="Unknown tool")),
        ],
        ids=["result", "null-result", "mcp-error"],
    )
    async def test_request_reaches_client_and_outcome_returns(
        self, acp_pair: AcpPair, outcome: McpOutcome
    ) -> None:
        await acp_pair.stand_client.replies.put(outcome)

        received = await acp_pair.agent.request(self.CALL, None)

        assert received == outcome
        assert await acp_pair.stand_client.questions.get() == self.CALL

    async def test_outer_error_keeps_its_code(self, acp_pair: AcpPair) -> None:
        refusal = RequestError(
            AcpErrorCode.UNKNOWN_SERVER, "No MCP server 'aid_all' is declared"
        )
        await acp_pair.stand_client.replies.put(refusal)

        with pytest.raises(RequestError) as caught:
            await acp_pair.agent.request(self.CALL, None)

        assert caught.value.code == AcpErrorCode.UNKNOWN_SERVER

    async def test_notification_reaches_agent(self, acp_pair: AcpPair) -> None:
        received: asyncio.Queue[McpMessage] = asyncio.Queue()
        progress = McpMessage(
            server_id="aid_all",
            request_id=self.CALL.request_id,
            method="notifications/progress",
            params={"progressToken": "mcp-1", "progress": 1, "total": 2},
        )
        late = progress.model_copy(update={"request_id": "mcp-finished"})
        pending = asyncio.create_task(acp_pair.agent.request(self.CALL, received.put))

        async with asyncio.timeout(5):
            await acp_pair.stand_client.questions.get()

        await acp_pair.client.notify(late)
        await acp_pair.client.notify(progress)

        async with asyncio.timeout(5):
            assert await received.get() == progress

        await acp_pair.stand_client.replies.put(McpResultOutcome(result=None))

        async with asyncio.timeout(5):
            assert await pending == McpResultOutcome(result=None)

        assert received.empty()

    async def test_cancelled_request_cancels_client_answer(
        self, acp_pair: AcpPair
    ) -> None:
        pending = asyncio.create_task(acp_pair.agent.request(self.CALL, None))

        async with asyncio.timeout(5):
            await acp_pair.stand_client.questions.get()

        pending.cancel()

        async with asyncio.timeout(5):
            await acp_pair.stand_client.cancelled.wait()

        assert pending.cancelled()


class TestSdkGuard:
    async def test_sdk_still_has_the_old_mcp_message_shape(self) -> None:
        version = importlib.metadata.version("agent-client-protocol")

        assert version == "1.0.0rc3"
        assert "connection_id" in schema.MessageMcpRequest.model_fields
        assert McpVersion.V2026_07_28 in MODERN_PROTOCOL_VERSIONS
        assert [s for s in ToolStatus if s.terminal] == [
            ToolStatus.COMPLETED,
            ToolStatus.FAILED,
            ToolStatus.CANCELLED,
        ]

        router = MethodRouter(StandClient(), Client)
        with pytest.raises(RequestError) as caught:
            await router(CLIENT_METHODS["session_request_permission"], {}, False)

        assert caught.value.code == AcpErrorCode.METHOD_NOT_FOUND
