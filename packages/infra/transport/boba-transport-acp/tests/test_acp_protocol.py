"""Провод ACP v2 на соединении SDK; кадры пары сверяются со схемой ACP.

Данные: `acp_schema/` — схема ACP v2, тег schema-v2.0.0-alpha.8.
"""

from __future__ import annotations

import asyncio
import importlib.metadata
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

import pytest
from acp._transport import memory_transport_pair
from acp.connection import Connection, JsonValue, StreamDirection, StreamEvent
from acp.exceptions import RequestError
from acp.experimental.v2 import Agent, AgentSideConnection, Client, schema
from acp.experimental.v2._router import MethodRouter
from acp.experimental.v2.meta import AGENT_METHODS, CLIENT_METHODS
from jsonschema import Draft202012Validator
from mcp_types.jsonrpc import ErrorData
from mcp_types.version import MODERN_PROTOCOL_VERSIONS

from boba.transport.acp.protocol import (
    AcpErrorCode,
    AcpVersion,
    AgentSide,
    ClientConnection,
    IdleErrorNotification,
    IdleErrorUpdate,
    McpErrorOutcome,
    McpMessage,
    McpOutcome,
    McpResultOutcome,
    McpVersion,
    StopReason,
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
    notifications: asyncio.Queue[McpMessage]


@pytest.fixture(scope="module")
def acp_schema() -> Draft202012Validator:
    path = Path(__file__).parent / "acp_schema" / "schema.unstable.json"
    return Draft202012Validator(json.loads(path.read_text(encoding="utf-8")))


@pytest.fixture
async def acp_pair(acp_schema: Draft202012Validator) -> AsyncIterator[AcpPair]:
    agent_end, client_end = memory_transport_pair()
    info = schema.Implementation(name="boba-transport-acp-tests", version="0")
    notifications: asyncio.Queue[McpMessage] = asyncio.Queue()
    stand_client = StandClient()
    frames: list[StreamEvent] = []

    agent = AgentSide(StandAgent, notifications.put, info, agent_end, None)
    serving = asyncio.create_task(agent.serve())

    client = ClientConnection(
        stand_client, stand_client.answer, client_end, None, observers=[frames.append]
    )
    await client.initialize(protocol_version=AcpVersion.V2, info=info)

    yield AcpPair(agent, client, stand_client, notifications)

    await client.close()
    await serving

    root = acp_schema.schema
    assert isinstance(root, dict)
    defs = root["$defs"]
    answered = {
        StreamDirection.INCOMING: StreamDirection.OUTGOING,
        StreamDirection.OUTGOING: StreamDirection.INCOMING,
    }
    asked: dict[tuple[StreamDirection, object], str] = {}

    for frame in frames:
        message = frame.message
        acp_schema.validate(message)

        if "method" not in message:
            method = asked.pop((answered[frame.direction], message["id"]))
            part = "Response"
            payload = message.get("result")
        elif "id" in message:
            method = message["method"]
            asked[(frame.direction, message["id"])] = method
            part = "Request"
            payload = message["params"]
        else:
            method = message["method"]
            part = "Notification"
            payload = message["params"]

        if payload is None:
            continue

        # корневая схема пускает любые params через Ext*, поэтому сверяем ещё и
        # с определением метода по x-method
        names = [
            name
            for name, body in defs.items()
            if body.get("x-method") == method and name.endswith(part)
        ]
        (name,) = names
        frame_schema = {"$ref": f"#/$defs/{name}", "$defs": defs}
        acp_schema.evolve(schema=frame_schema).validate(payload)


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
        notifications: asyncio.Queue[McpMessage] = asyncio.Queue()
        info = schema.Implementation(name="stand-agent", version="0")
        agent = AgentSide(StandAgent, notifications.put, info, agent_end, None)
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
        server_id="boba_general",
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

        received = await acp_pair.agent.request(self.CALL)

        assert received == outcome
        assert await acp_pair.stand_client.questions.get() == self.CALL

    async def test_outer_error_keeps_its_code(self, acp_pair: AcpPair) -> None:
        refusal = RequestError(
            AcpErrorCode.SERVER_UNAVAILABLE, "No MCP server 'boba_general' is declared"
        )
        await acp_pair.stand_client.replies.put(refusal)

        with pytest.raises(RequestError) as caught:
            await acp_pair.agent.request(self.CALL)

        assert caught.value.code == AcpErrorCode.SERVER_UNAVAILABLE

    async def test_notification_reaches_agent(self, acp_pair: AcpPair) -> None:
        progress = McpMessage(
            server_id="boba_general",
            request_id="mcp-1",
            method="notifications/progress",
            params={"progressToken": "mcp-1", "progress": 1, "total": 2},
        )

        await acp_pair.client.notify(progress)

        async with asyncio.timeout(5):
            assert await acp_pair.notifications.get() == progress

    async def test_cancelled_request_cancels_client_answer(
        self, acp_pair: AcpPair
    ) -> None:
        pending = asyncio.create_task(acp_pair.agent.request(self.CALL))

        async with asyncio.timeout(5):
            await acp_pair.stand_client.questions.get()

        pending.cancel()

        async with asyncio.timeout(5):
            await acp_pair.stand_client.cancelled.wait()

        assert pending.cancelled()


class TestIdleError:
    async def test_error_reaches_client(self, acp_pair: AcpPair) -> None:
        error = schema.Error(
            code=AcpErrorCode.INTERNAL_ERROR,
            message="The model endpoint refused the request",
        )
        stopped = IdleErrorNotification(
            session_id="session-1",
            update=IdleErrorUpdate(stop_reason=StopReason.ERROR, error=error),
        )

        await acp_pair.agent.send_update(stopped)

        async with asyncio.timeout(5):
            update = await acp_pair.stand_client.updates.get()

        assert isinstance(update, IdleErrorUpdate)
        assert update.error == error


class TestSdkGuard:
    async def test_sdk_still_has_the_old_mcp_message_shape(self) -> None:
        version = importlib.metadata.version("agent-client-protocol")

        assert version == "1.0.0rc3"
        assert "connection_id" in schema.MessageMcpRequest.model_fields
        assert McpVersion.V2026_07_28 in MODERN_PROTOCOL_VERSIONS

        router = MethodRouter(StandClient(), Client)
        with pytest.raises(RequestError) as caught:
            await router(CLIENT_METHODS["session_request_permission"], {}, False)

        assert caught.value.code == AcpErrorCode.METHOD_NOT_FOUND
