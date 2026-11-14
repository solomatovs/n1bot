import asyncio
from typing import cast
from uuid import uuid4

import pytest
from acp import RequestError
from acp.connection import Connection
from acp.experimental.v2 import schema

from boba.agent.acp_server import AcpAgent, SessionHub
from boba.agent.settings import ModelSettings, ProviderSettings, SettingsLayer
from boba.llm.http.openai import WireChunk


class StreamingLlm:
    def __init__(self):
        self.started = asyncio.Event()
        self.finish = asyncio.Event()
        self.requests = []

    def address(self, provider):
        return "https://test.invalid/chat/completions"

    async def generate(self, provider, request, output, emit):
        self.requests.append(request)
        output.status = 200
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


class Client:
    def __init__(self):
        self.events = []

    def add_observer(self, observer):
        self.observer = observer

    async def send_notification(self, method, params):
        self.events.append((method, params))

    async def send_request(self, method, params):
        raise AssertionError(f"Unexpected client request: {method}")


@pytest.mark.anyio
@pytest.mark.integration
async def test_two_acp_clients_share_stream_and_replay_from_postgres(  # noqa: PLR0915
    history, agent_timeout: float
):
    model = ModelSettings(
        provider=ProviderSettings.model_validate(
            {
                "kind": "openai",
                "baseUrl": "https://test.invalid/v1",
                "auth": {"method": "none"},
            }
        ),
        model="test",
        system_prompt="You are a test assistant.",
    )
    llm = StreamingLlm()
    hub = SessionHub(history, llm, {"test": model}, SettingsLayer(), "test")
    owner = uuid4().hex
    first, second = (
        AcpAgent(hub, owner, frozenset({"test"}), "test"),
        AcpAgent(hub, owner, frozenset({"test"}), "test"),
    )
    clients = [Client(), Client()]
    sid = None
    try:
        for adapter, client in zip((first, second), clients, strict=True):
            adapter.attach(cast(Connection, client))
            response = await adapter("initialize", {"protocolVersion": 2}, False)
            schema.InitializeResponse.model_validate(response)
        response = await first("session/new", {"cwd": "/workspace"}, False)
        assert isinstance(response, dict)
        schema.NewSessionResponse.model_validate(response)
        sid = response["sessionId"]
        await first.subscribe(sid)
        accepted = await first(
            "session/prompt",
            {"sessionId": sid, "prompt": [{"type": "text", "text": "Hello"}]},
            False,
        )
        schema.PromptResponse.model_validate(accepted)
        async with asyncio.timeout(agent_timeout):
            await llm.started.wait()
        response = await second(
            "session/resume",
            {"sessionId": sid, "cwd": "/workspace", "replayFrom": {"type": "start"}},
            False,
        )
        schema.ResumeSessionResponse.model_validate(response)
        await second.subscribe(sid)
        await first("session/close", {"sessionId": sid}, False)
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
        stranger.attach(cast(Connection, Client()))
        await stranger("initialize", {"protocolVersion": 2}, False)
        with pytest.raises(RequestError):
            await stranger(
                "session/resume", {"sessionId": sid, "cwd": "/workspace"}, False
            )
        assert len(llm.requests) == 1
    finally:
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
