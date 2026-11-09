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
from acp.experimental.v2 import (
    Agent,
    AgentSideConnection,
    Client,
    ClientSideConnection,
    schema,
)
from acp.experimental.v2._router import MethodRouter
from acp.experimental.v2.meta import AGENT_METHODS, CLIENT_METHODS
from mcp_types.jsonrpc import ErrorData
from mcp_types.version import MODERN_PROTOCOL_VERSIONS

from boba.stand.acp_frames import Frames
from boba.transport.acp.protocol import (
    AcpErrorCode,
    AcpTransportError,
    AcpVersion,
    AgentSide,
    BobaExtension,
    ClientConnection,
    McpErrorOutcome,
    McpMessage,
    McpOutcome,
    McpResultOutcome,
    McpVersion,
    StdioTransport,
    ToolSourceContent,
    ToolSourceReply,
    ToolSourceRequest,
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
    отмечает отмену ответа в cancelled; на `_boba/tool_source` отвечает
    источником с именем инструмента и копит запросы в sourced."""

    def __init__(self) -> None:
        self.updates: asyncio.Queue[object] = asyncio.Queue()
        self.questions: asyncio.Queue[McpMessage] = asyncio.Queue()
        self.replies: asyncio.Queue[McpOutcome | RequestError] = asyncio.Queue()
        self.cancelled = asyncio.Event()
        self.sourced: list[ToolSourceRequest] = []

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

    async def source(self, request: ToolSourceRequest) -> ToolSourceReply:
        self.sourced.append(request)

        return ToolSourceReply(source={"tool": request.name})


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

    agent = AgentSide(StandAgent, agent_end)
    serving = asyncio.create_task(agent.serve())

    client = ClientConnection(
        stand_client,
        stand_client.answer,
        stand_client.source,
        client_end,
        None,
        observers=[frames],
    )
    await client.initialize(protocol_version=AcpVersion.V2, info=info)

    yield AcpPair(agent, client, stand_client)

    await client.close()
    await serving

    frames.check_schema()


@pytest.fixture
async def stdio_wire() -> AsyncIterator[
    tuple[asyncio.StreamReader, asyncio.StreamWriter]
]:
    """Сторона клиента сокета, на другом конце которого AgentSide на StdioTransport."""
    agent_socket, client_socket = socket.socketpair()
    agent_reader, agent_writer = await asyncio.open_connection(sock=agent_socket)
    client_reader, client_writer = await asyncio.open_connection(sock=client_socket)
    agent = AgentSide(StandAgent, StdioTransport(agent_reader, agent_writer))
    serving = asyncio.create_task(agent.serve())

    yield client_reader, client_writer

    client_writer.close()
    await client_writer.wait_closed()
    async with asyncio.timeout(5):
        await serving

    agent_writer.close()
    await agent_writer.wait_closed()


class TestStdio:
    INITIALIZE: ClassVar[dict[str, Any]] = {
        "jsonrpc": "2.0",
        "method": AGENT_METHODS["initialize"],
        "params": {
            "protocolVersion": AcpVersion.V2,
            "info": {"name": "batch-client", "version": "0"},
        },
    }

    @pytest.mark.parametrize(
        "value",
        [
            [],
            [17, True, None],
            17,
            True,
            None,
            "text",
            [
                {**INITIALIZE, "id": 1},
                {"jsonrpc": "2.0", "id": 2, "method": "session/list"},
            ],
            [{"jsonrpc": "2.0", "method": "_unknown"}],
        ],
    )
    async def test_non_object_is_rejected_without_executing_entries(
        self,
        stdio_wire: tuple[asyncio.StreamReader, asyncio.StreamWriter],
        value: Any,
    ) -> None:
        reader, writer = stdio_wire
        writer.write(json.dumps(value).encode() + b"\n")
        async with asyncio.timeout(5):
            answer = json.loads(await reader.readline())
        assert answer["id"] is None
        assert answer["error"]["code"] == -32600

        # initialize из массива не должен был изменить состояние соединения.
        writer.write(b'{"jsonrpc":"2.0","id":3,"method":"session/list"}\n')
        async with asyncio.timeout(5):
            refused = json.loads(await reader.readline())
        assert refused["id"] == 3
        assert refused["error"]["code"] == -32600

        writer.write(json.dumps({**self.INITIALIZE, "id": 4}).encode() + b"\n")
        async with asyncio.timeout(5):
            initialized = json.loads(await reader.readline())
        assert initialized["id"] == 4
        assert initialized["result"]["protocolVersion"] == 2

    @pytest.mark.parametrize("line", [b'{"jsonrpc": "2.0", "method"\n', b"\xff\n"])
    async def test_broken_json_is_parse_error_and_reading_continues(
        self,
        stdio_wire: tuple[asyncio.StreamReader, asyncio.StreamWriter],
        line: bytes,
    ) -> None:
        reader, writer = stdio_wire
        writer.write(line)
        writer.write(json.dumps({**self.INITIALIZE, "id": 1}).encode() + b"\n")
        async with asyncio.timeout(5):
            answer = json.loads(await reader.readline())
            initialized = json.loads(await reader.readline())
        assert answer["id"] is None
        assert answer["error"]["code"] == -32700
        assert initialized["result"]["protocolVersion"] == 2

    async def test_line_limit_is_preserved(
        self,
        stdio_wire: tuple[asyncio.StreamReader, asyncio.StreamWriter],
    ) -> None:
        _, writer = stdio_wire
        reader = asyncio.StreamReader(limit=16)
        reader.feed_data(b"x" * 17 + b"\n")
        with pytest.raises(AcpTransportError, match="line limit"):
            await StdioTransport(reader, writer).receive()


class TestVersionNegotiation:
    async def _reply_version_3(
        self, method: str, params: JsonValue | None, is_notification: bool
    ) -> schema.InitializeResponse:
        return schema.InitializeResponse(
            protocol_version=3, info=schema.Implementation(name="future", version="3")
        )

    @pytest.mark.parametrize("version", [2, 3, 65535])
    async def test_agent_selects_a_supported_version(self, version: int) -> None:
        agent_end, client_end = memory_transport_pair()
        agent = AgentSide(StandAgent, agent_end)
        serving = asyncio.create_task(agent.serve())

        async with Connection(self._reply_version_3, client_end) as raw:
            params = {
                "protocolVersion": version,
                "info": {"name": "future", "version": "3"},
            }
            answer = await raw.send_request(AGENT_METHODS["initialize"], params)
            with pytest.raises(RequestError) as unsupported:
                await raw.send_request(AGENT_METHODS["session_list"], {})

            assert unsupported.value.code == AcpErrorCode.METHOD_NOT_FOUND

        await serving

        assert answer["protocolVersion"] == AcpVersion.V2
        assert answer["info"] == {"name": "stand-agent", "version": "0"}

    @pytest.mark.parametrize("version", [0, 1])
    async def test_lower_version_does_not_initialize_the_connection(
        self,
        stdio_wire: tuple[asyncio.StreamReader, asyncio.StreamWriter],
        version: int,
    ) -> None:
        reader, writer = stdio_wire
        request = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": AGENT_METHODS["initialize"],
            "params": {
                "protocolVersion": version,
                "clientInfo": {"name": "legacy", "version": "1"},
            },
        }
        writer.write(json.dumps(request).encode() + b"\n")
        async with asyncio.timeout(5):
            answer = json.loads(await reader.readline())

        assert answer["error"] == {
            "code": AcpErrorCode.UNSUPPORTED_PROTOCOL_VERSION,
            "message": "Unsupported protocol version",
            "data": {"requested": version, "supported": [2]},
        }

        followup = {"jsonrpc": "2.0", "id": 2, "method": AGENT_METHODS["session_list"]}
        writer.write(json.dumps(followup).encode() + b"\n")
        async with asyncio.timeout(5):
            refused = json.loads(await reader.readline())

        assert refused["error"]["code"] == -32600

    @pytest.mark.parametrize(
        "params",
        [
            {},
            {"protocolVersion": None},
            {"protocolVersion": "2"},
            {"protocolVersion": True},
            {"protocolVersion": False},
            {"protocolVersion": 2.5},
            {"protocolVersion": -1},
            {"protocolVersion": 65536},
        ],
    )
    async def test_malformed_version_is_invalid_params(
        self,
        stdio_wire: tuple[asyncio.StreamReader, asyncio.StreamWriter],
        params: dict[str, Any],
    ) -> None:
        reader, writer = stdio_wire
        request = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": AGENT_METHODS["initialize"],
            "params": params,
        }
        writer.write(json.dumps(request).encode() + b"\n")
        async with asyncio.timeout(5):
            answer = json.loads(await reader.readline())

        assert answer["error"]["code"] == AcpErrorCode.INVALID_PARAMS

    @pytest.mark.parametrize(
        "cursor",
        ["start", {}, {"type": 5}, {"type": None}, {"type": True}, [], 5, False],
    )
    async def test_malformed_replay_is_not_normalized(
        self,
        stdio_wire: tuple[asyncio.StreamReader, asyncio.StreamWriter],
        cursor: Any,
    ) -> None:
        reader, writer = stdio_wire
        initialize = {**TestStdio.INITIALIZE, "id": 1}
        writer.write(json.dumps(initialize).encode() + b"\n")
        async with asyncio.timeout(5):
            initialized = json.loads(await reader.readline())

        assert "result" in initialized
        request = {
            "jsonrpc": "2.0",
            "id": 2,
            "method": AGENT_METHODS["session_resume"],
            "params": {"sessionId": "existing", "cwd": "/", "replayFrom": cursor},
        }
        writer.write(json.dumps(request).encode() + b"\n")
        async with asyncio.timeout(5):
            answer = json.loads(await reader.readline())

        assert answer["error"]["code"] == AcpErrorCode.INVALID_PARAMS

    async def test_client_closes_on_foreign_version(self) -> None:
        agent_end, client_end = memory_transport_pair()
        stand_client = StandClient()
        raw = Connection(self._reply_version_3, agent_end, listening=False)
        reading = asyncio.create_task(raw.main_loop())

        client = ClientConnection(
            stand_client, stand_client.answer, stand_client.source, client_end, None
        )
        info = schema.Implementation(name="boba-transport-acp-tests", version="0")
        with pytest.raises(RequestError):
            await client.initialize(protocol_version=AcpVersion.V2, info=info)

        async with asyncio.timeout(5):
            await reading

        await raw.close()


class NegotiatingAgent:
    """Сохраняет последнее согласование, позволяет задержать или отклонить его."""

    def __init__(self) -> None:
        self.capabilities: schema.ClientCapabilities | None = None
        self.calls = 0
        self.entered = asyncio.Event()
        self.proceed = asyncio.Event()
        self.notified = asyncio.Event()

    async def handle_extension_notification(self, method: str, params: Any) -> None:
        self.notified.set()

    async def initialize(
        self,
        protocol_version: int,
        info: schema.Implementation,
        capabilities: schema.ClientCapabilities | None = None,
        **kwargs: Any,
    ) -> schema.InitializeResponse:
        self.calls += 1
        if info.name == "wait":
            self.entered.set()
            await self.proceed.wait()
        if info.name == "refuse":
            raise RequestError.invalid_params({"details": "test refusal"})
        self.capabilities = capabilities
        return schema.InitializeResponse(protocol_version=2, info=info)


@pytest.fixture
async def negotiation() -> AsyncIterator[tuple[NegotiatingAgent, Connection]]:
    agent_end, client_end = memory_transport_pair()
    target = NegotiatingAgent()
    agent = AgentSide(lambda side, connection: target, agent_end)
    serving = asyncio.create_task(agent.serve())

    async def receive(
        method: str,
        params: JsonValue | None,
        is_notification: bool,
    ) -> None:
        return None

    async with Connection(receive, client_end) as raw:
        yield target, raw
    await serving


class TestRepeatedInitialization:
    @staticmethod
    async def initialize(
        raw: Connection,
        name: str = "client",
        **params: Any,
    ) -> Any:
        return await raw.send_request(
            "initialize",
            {"protocolVersion": 2, "info": {"name": name, "version": "0"}, **params},
        )

    async def test_latest_success_replaces_capabilities(
        self,
        negotiation: tuple[NegotiatingAgent, Connection],
    ) -> None:
        target, raw = negotiation
        first = await self.initialize(
            raw, protocolVersion=3, capabilities={"elicitation": {"form": {}}}
        )
        assert first["protocolVersion"] == 2
        assert target.capabilities is not None
        assert target.capabilities.elicitation is not None
        await raw.send_notification("_unknown", {})
        for _ in range(2):
            await self.initialize(raw, capabilities={"elicitation": {"url": {}}})
        assert target.calls == 3
        assert target.capabilities.elicitation.form is None
        assert target.capabilities.elicitation.url is not None

    @pytest.mark.parametrize("first", [True, False])
    async def test_notifications_are_ignored_only_before_first_initialization(
        self,
        negotiation: tuple[NegotiatingAgent, Connection],
        first: bool,
    ) -> None:
        target, raw = negotiation
        if not first:
            await self.initialize(raw)
        pending = asyncio.create_task(self.initialize(raw, name="wait"))
        await target.entered.wait()
        try:
            await raw.send_notification("_test/notification", {})
            with pytest.raises(RequestError):
                await raw.send_request("_test/barrier", {})
        finally:
            target.proceed.set()
            await pending
        if not first:
            async with asyncio.timeout(5):
                await target.notified.wait()
        assert target.notified.is_set() is not first

    @pytest.mark.parametrize("first", [True, False])
    async def test_failed_initialize_can_be_retried(
        self,
        negotiation: tuple[NegotiatingAgent, Connection],
        first: bool,
    ) -> None:
        target, raw = negotiation
        if not first:
            await self.initialize(raw, capabilities={"elicitation": {"form": {}}})
        previous = target.capabilities
        with pytest.raises(RequestError, match="Invalid params"):
            await self.initialize(raw, name="refuse")
        assert target.capabilities is previous
        await self.initialize(raw)

    @pytest.mark.parametrize(
        ("params", "code"),
        [
            ({"protocolVersion": 1}, -32022),
            ({"info": {}}, -32602),
            ({"info": {"name": "refuse", "version": "0"}}, -32602),
        ],
    )
    async def test_bad_repeat_leaves_previous_initialization_in_effect(
        self,
        negotiation: tuple[NegotiatingAgent, Connection],
        params: dict[str, Any],
        code: int,
    ) -> None:
        target, raw = negotiation
        await self.initialize(raw, capabilities={"elicitation": {"form": {}}})
        previous = target.capabilities
        with pytest.raises(RequestError) as refused:
            await self.initialize(raw, **params)
        assert refused.value.code == code
        assert target.capabilities is previous
        calls = target.calls
        with pytest.raises(RequestError) as unknown:
            await raw.send_request("_unknown", {})
        assert unknown.value.code == -32601  # готов, а не -32600
        with pytest.raises(RequestError) as sealed:
            await self.initialize(raw)
        assert sealed.value.code == -32600
        assert target.calls == calls

    async def test_pending_initialize_does_not_admit_requests_or_repeats(
        self,
        negotiation: tuple[NegotiatingAgent, Connection],
    ) -> None:
        target, raw = negotiation
        pending = asyncio.create_task(self.initialize(raw, name="wait"))
        await target.entered.wait()
        try:
            for method, params in [
                ("session/list", {}),
                (
                    "initialize",
                    {"protocolVersion": 2, "info": {"name": "second", "version": "0"}},
                ),
            ]:
                with pytest.raises(RequestError) as refused:
                    await raw.send_request(method, params)
                assert refused.value.code == -32600
            assert target.calls == 1
        finally:
            target.proceed.set()
            await pending
        await self.initialize(raw)

    async def test_client_retries_refusal_without_closing(self) -> None:
        agent_end, client_end = memory_transport_pair()
        target = NegotiatingAgent()
        agent = AgentSide(lambda side, connection: target, agent_end)
        serving = asyncio.create_task(agent.serve())
        peer = StandClient()
        client = ClientConnection(peer, peer.answer, peer.source, client_end, None)
        try:
            for name in ("refuse", "client", "refuse", "client"):
                info = schema.Implementation(name=name, version="0")
                if name == "refuse":
                    with pytest.raises(RequestError):
                        await client.initialize(protocol_version=2, info=info)
                else:
                    await client.initialize(protocol_version=2, info=info)
                assert client.connected()
            with pytest.raises(RequestError):
                await client.list_sessions()
            with pytest.raises(RequestError) as sealed:
                await client.initialize(protocol_version=2, info=info)
            assert sealed.value.code == -32600
            assert target.calls == 4
        finally:
            await client.close()
            await serving


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


class TestToolSource:
    """Расширение источника вызова: запрос агента `_boba/tool_source` доходит
    до обработчика клиента, ответ возвращается агенту как есть; содержимое
    вызова того же вида проходит схему уведомления SDK."""

    REQUEST: ClassVar[ToolSourceRequest] = ToolSourceRequest(
        session_id="s-1",
        tool_call_id="call-1",
        server_id="aid_all",
        name="pg_query",
        arguments={"sql": "select 1"},
    )

    async def test_request_reaches_client_and_source_returns(
        self, acp_pair: AcpPair
    ) -> None:
        reply = await acp_pair.agent.tool_source(self.REQUEST)

        assert reply == ToolSourceReply(source={"tool": "pg_query"})
        assert acp_pair.stand_client.sourced == [self.REQUEST]

    @pytest.mark.parametrize("params", [None, {}, {"sessionId": "s-1"}])
    async def test_invalid_source_request_is_refused(
        self, acp_pair: AcpPair, params: JsonValue | None
    ) -> None:
        with pytest.raises(RequestError) as caught:
            await acp_pair.agent.connection.send_request(
                BobaExtension.TOOL_SOURCE.value, params
            )

        assert caught.value.code == AcpErrorCode.INVALID_PARAMS
        assert acp_pair.stand_client.sourced == []

    async def test_unknown_extension_is_refused(self, acp_pair: AcpPair) -> None:
        with pytest.raises(RequestError) as caught:
            await acp_pair.agent.connection.send_request("_stand/unknown", {})

        assert caught.value.code == AcpErrorCode.METHOD_NOT_FOUND
        assert acp_pair.stand_client.sourced == []

    async def test_other_extensions_reach_the_original_client(
        self, acp_pair: AcpPair, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        requests: list[tuple[str, Any]] = []
        notifications: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()

        async def request(method: str, params: Any) -> Any:
            requests.append((method, params))
            return {"echo": params}

        async def notification(method: str, params: Any) -> None:
            await notifications.put((method, params))

        monkeypatch.setattr(
            acp_pair.stand_client, "handle_extension_request", request, raising=False
        )
        monkeypatch.setattr(
            acp_pair.stand_client,
            "handle_extension_notification",
            notification,
            raising=False,
        )
        reply = await acp_pair.agent.connection.send_request("_stand/echo", {"n": 1})
        assert reply == {"echo": {"n": 1}}
        assert requests == [("_stand/echo", {"n": 1})]

        await acp_pair.agent.connection.send_notification("_stand/changed", {"n": 2})
        async with asyncio.timeout(5):
            assert await notifications.get() == ("_stand/changed", {"n": 2})

        # Наше расширение по-прежнему получает source, а не чужой обработчик.
        assert await acp_pair.agent.tool_source(self.REQUEST) == ToolSourceReply(
            source={"tool": "pg_query"}
        )
        assert len(requests) == 1

    async def test_unimplemented_sdk_method_remains_unavailable(
        self, acp_pair: AcpPair
    ) -> None:
        with pytest.raises(RequestError) as caught:
            await acp_pair.agent.connection.send_request(
                CLIENT_METHODS["session_request_permission"],
                {
                    "sessionId": "s-1",
                    "title": "Allow test?",
                    "options": [
                        {"optionId": "allow", "name": "Allow", "kind": "allow_once"}
                    ],
                },
            )

        assert caught.value.code == AcpErrorCode.METHOD_NOT_FOUND

    @pytest.mark.parametrize("reply", [{"source": {"tool": "plain-sdk"}}, {}, None])
    async def test_source_from_a_plain_sdk_client(self, reply: Any) -> None:
        class Peer:
            async def handle_extension_request(self, method: str, params: Any) -> Any:
                assert method == BobaExtension.TOOL_SOURCE.value
                assert (
                    ToolSourceRequest.model_validate(params) == TestToolSource.REQUEST
                )
                return reply

        agent_end, client_end = memory_transport_pair()
        agent = AgentSide(StandAgent, agent_end)
        serving = asyncio.create_task(agent.serve())
        try:
            async with ClientSideConnection(Peer(), client_end) as client:
                await client.initialize(
                    protocol_version=AcpVersion.V2,
                    info=schema.Implementation(name="plain-sdk", version="0"),
                )
                if reply and "source" in reply:
                    assert await agent.tool_source(self.REQUEST) == ToolSourceReply(
                        source={"tool": "plain-sdk"}
                    )
                else:
                    with pytest.raises(AcpTransportError, match="expected an object"):
                        await agent.tool_source(self.REQUEST)
        finally:
            await agent.connection.close()
            await serving

    async def test_source_rides_the_tool_call_as_its_content(self) -> None:
        content = ToolSourceContent(source={"tool": "pg_query"}).wire()
        update = schema.SessionToolCallUpdate(
            tool_call_id="call-1", status=ToolStatus.PENDING, content=[content]
        )

        dumped = update.model_dump(mode="json", by_alias=True, exclude_none=True)
        parsed = schema.SessionToolCallUpdate.model_validate(dumped)

        assert parsed.content is not None
        (item,) = parsed.content
        assert isinstance(item, schema.OtherToolCallContent)
        assert item.model_dump(mode="json") == {
            "type": "_boba/tool_source",
            "source": {"tool": "pg_query"},
        }


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
