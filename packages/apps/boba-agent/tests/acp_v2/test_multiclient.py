import asyncio
from uuid import uuid4

import pytest
from acp import RequestError
from acp._transport import memory_transport_pair
from acp.connection import Connection
from acp.experimental.v2 import schema
from agent_stand import Llm

from boba.agent.acp_connection import AcpAgent
from boba.agent.acp_server import AgentProfiles, SessionHub
from boba.agent.settings import (
    AgentProfile,
    ModelSettings,
    SettingsLayer,
)
from boba.llm.http.openai import WireChunk

from .client import V2Client


class StreamingLlm:
    def __init__(self):
        self.started = asyncio.Event()
        self.finish = asyncio.Event()
        self.requests = []

    def for_user(self, owner, providers):
        return self

    async def generate(self, provider, request, output, emit):
        self.requests.append(request)
        output.status = 200
        output.address = "https://test.invalid/chat/completions"
        for delta in output.response.deltas(
            WireChunk.model_validate({"choices": [{"delta": {"content": "Начало "}}]})
        ):
            await emit(delta.position, delta.kind, delta.text)
        self.started.set()
        await self.finish.wait()
        for delta in output.response.deltas(
            WireChunk.model_validate(
                {"choices": [{"delta": {"content": "конец"}, "finish_reason": "stop"}]}
            )
        ):
            await emit(delta.position, delta.kind, delta.text)


class ConcurrentLlm(Llm):
    """Транспорт стенда: вызывает tool_search и отвечает после результата задачи."""

    async def generate(self, provider, request, output, emit):
        self.requests.append(request)
        output.status = 200
        output.address = "https://test.invalid/chat/completions"
        message = {"content": "Task observed"}
        if request["messages"][-1]["role"] == "user":
            message = {
                "tool_calls": [
                    {
                        "id": uuid4().hex,
                        "type": "function",
                        "function": {"name": "tool_search", "arguments": "{}"},
                    }
                ]
            }

        chunk = WireChunk.model_validate(
            {"choices": [{"message": message, "finish_reason": "stop"}]}
        )
        for delta in output.response.deltas(chunk):
            await emit(delta.position, delta.kind, delta.text)


@pytest.mark.anyio
@pytest.mark.integration
async def test_parallel_sessions_dispatch_observe_and_replay_tasks(
    history, system, agent_timeout
):
    llm = ConcurrentLlm()
    profiles = AgentProfiles(
        {"test": AgentProfile(roles=["*"], model=ModelSettings(model="test"))}
    )
    hub = SessionHub(
        history, llm, profiles, SettingsLayer(), "test", providers=system.providers
    )
    await hub.start(system.postgres)
    clients = []
    sessions = []
    try:
        for _ in range(16):
            client = V2Client(hub, system.owner, agent_timeout, frozenset({"test"}))
            clients.append(client)
            await client.initialize()
            sessions.append(await client.new_session())

        for _ in range(3):
            async with asyncio.TaskGroup() as group:
                for client, sid in zip(clients, sessions, strict=True):
                    group.create_task(client.prompt(sid, "Find available tools"))

            async with asyncio.timeout(agent_timeout), asyncio.TaskGroup() as group:
                for sid in sessions:
                    group.create_task(hub.agents[sid].idle.wait())

        assert len(llm.requests) == 96
        for client, sid in zip(clients, sessions, strict=True):
            assert hub.agents[sid].last_error is None
            calls = await history.latest(sid, category="tool_call")
            assert len(calls) == 3
            results = await history.latest(sid, category="tool_result")
            assert len(results) == 3
            for result in results:
                assert result.payload.status == "completed"

            await history.openai(sid)
            await client.resume(sid)

    finally:
        for client in clients:
            await client.close()

        await hub.close()
        routes = []
        for sid in sessions:
            routes.append("agent:" + sid)

        async with history.pool.connection() as connection:
            await connection.execute(
                "delete from boba.tasks where route_key = any(%s)",
                (routes,),
            )
            await connection.execute(
                "delete from boba.records where session_id = any(%s)", (sessions,)
            )
            await connection.execute(
                "delete from boba.sessions where session_id = any(%s)", (sessions,)
            )


class Client:
    def __init__(self, adapter: AcpAgent):
        self.events = []
        agent_end, client_end = memory_transport_pair()
        self.server = adapter.connect(agent_end)
        self.connection = Connection(self.receive, client_end)

    async def receive(self, method, params, is_notification):
        assert is_notification
        self.events.append((method, params))

    async def request(self, method, params, is_notification=False):
        assert not is_notification
        return await self.connection.send_request(method, params)

    async def close(self):
        await self.connection.close()
        await self.server.close()


@pytest.mark.anyio
@pytest.mark.integration
async def test_two_acp_clients_share_stream_and_replay_from_postgres(  # noqa: PLR0915
    history, system, agent_timeout: float
):
    model = ModelSettings(
        model="test",
        system_prompt="You are a test assistant.",
    )
    llm = StreamingLlm()
    profiles = AgentProfiles({"test": AgentProfile(roles=["*"], model=model)})
    hub = SessionHub(
        history, llm, profiles, SettingsLayer(), "test", providers=system.providers
    )
    await hub.start(system.postgres)
    owner = system.owner
    first, second = (
        AcpAgent(hub, owner, frozenset({"test"}), "test"),
        AcpAgent(hub, owner, frozenset({"test"}), "test"),
    )
    clients = [Client(first), Client(second)]
    sid = None
    try:
        for client in clients:
            response = await client.request(
                "initialize",
                {"protocolVersion": 2, "info": {"name": "agent-test", "version": "1"}},
                False,
            )
            schema.InitializeResponse.model_validate(response)
        response = await clients[0].request("session/new", {"cwd": "/workspace"}, False)
        assert isinstance(response, dict)
        schema.NewSessionResponse.model_validate(response)
        sid = response["sessionId"]
        accepted = await clients[0].request(
            "session/prompt",
            {"sessionId": sid, "prompt": [{"type": "text", "text": "Hello"}]},
            False,
        )
        schema.PromptResponse.model_validate(accepted)
        async with asyncio.timeout(agent_timeout):
            await llm.started.wait()
        response = await clients[1].request(
            "session/resume",
            {"sessionId": sid, "cwd": "/workspace", "replayFrom": {"type": "start"}},
            False,
        )
        schema.ResumeSessionResponse.model_validate(response)
        await clients[0].request("session/close", {"sessionId": sid}, False)
        assert not hub.agents[sid].idle.is_set()
        llm.finish.set()
        async with asyncio.timeout(agent_timeout):
            await hub.agents[sid].idle.wait()
        assert hub.agents[sid].last_error is None
        await asyncio.sleep(0.02)
        updates = [params["update"] for _, params in clients[1].events]
        chunks = [
            item for item in updates if item["sessionUpdate"] == "agent_message_chunk"
        ]
        assert "".join(item["content"]["text"] for item in chunks) == "Начало конец"
        assert len({item["messageId"] for item in chunks}) == 1
        final = [
            item
            for item in updates
            if item.get("messageId") == chunks[0]["messageId"]
            and item["sessionUpdate"] == "agent_message"
        ]
        assert len(final) == 1
        assert final[0]["content"] == [{"type": "text", "text": "Начало конец"}]
        stranger = AcpAgent(hub, uuid4().hex, frozenset({"test"}), "test")
        other = Client(stranger)
        clients.append(other)
        await other.request(
            "initialize",
            {"protocolVersion": 2, "info": {"name": "agent-test", "version": "1"}},
            False,
        )
        with pytest.raises(RequestError):
            await other.request(
                "session/resume", {"sessionId": sid, "cwd": "/workspace"}, False
            )
        assert len(llm.requests) == 1
    finally:
        for client in clients:
            await client.close()
        await first.close_all()
        await second.close_all()
        await hub.close()
        if sid:
            async with history.pool.connection() as conn:
                await conn.execute(
                    "delete from boba.records where session_id = %s", (sid,)
                )
                await conn.execute(
                    "delete from boba.sessions where session_id = %s", (sid,)
                )
