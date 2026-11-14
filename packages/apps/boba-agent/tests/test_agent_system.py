"""Сквозные сценарии: ACP SDK, исполнитель, PostgreSQL, HTTP LLM и MCP через ACP."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Mapping
from typing import Any
from uuid import uuid4

import pytest
from acp import RequestError
from acp._transport import memory_transport_pair
from acp.connection import Connection
from acp.experimental.v2 import schema
from conftest import InferenceServer
from starlette.responses import Response, StreamingResponse

from boba.agent import models as m
from boba.agent.acp_server import AcpAgent, AgentProfiles, SessionHub
from boba.agent.acp_validator import MessageChunk, MessageUpdate, NotificationParams
from boba.agent.history import HistoryStore
from boba.agent.llm import OpenAiTransport
from boba.agent.settings import AgentProfile, McpSettings, ModelSettings, SettingsLayer
from boba.db.postgres.connection import PostgresConfig

pytestmark = [pytest.mark.anyio, pytest.mark.integration]


class AgentClient:
    """Клиент и агент связаны реальным ACP Connection; клиент также обслуживает MCP."""

    def __init__(
        self, hub: SessionHub, owner: str, timeout: float, granted: frozenset[str]
    ) -> None:
        self.timeout = timeout
        self.updates: list[NotificationParams] = []
        self.permissions: list[dict[str, Any]] = []
        self.tool_calls: list[dict[str, Any]] = []
        self.permission = "allow"
        self.tool_started = asyncio.Event()
        self.release_tool = asyncio.Event()
        self.release_tool.set()
        self.changed = asyncio.Condition()
        self.adapter = AcpAgent(hub, owner, granted, "test")
        agent_end, client_end = memory_transport_pair()
        self.server = Connection(self.adapter, agent_end)
        self.adapter.attach(self.server)
        self.connection = Connection(self.receive, client_end)

    async def initialize(self) -> None:
        result = await self.connection.send_request(
            "initialize", {"protocolVersion": 2}
        )
        schema.InitializeResponse.model_validate(result)

    async def receive(self, method: str, params: Any, is_notification: bool) -> Any:
        if method == "session/update":
            assert is_notification
            update = NotificationParams.model_validate(params)
            async with self.changed:
                self.updates.append(update)
                self.changed.notify_all()

            return None

        if method == "session/request_permission":
            assert not is_notification
            schema.RequestPermissionRequest.model_validate(params)
            self.permissions.append(params)

            return {"outcome": {"outcome": "selected", "optionId": self.permission}}

        assert method == "mcp/message", f"Unexpected client operation: {method}"
        assert not is_notification
        if params["method"] == "tools/list":
            return {
                "result": {
                    "tools": [
                        {
                            "name": "echo",
                            "description": "Echo text in the user workspace",
                            "inputSchema": {
                                "type": "object",
                                "properties": {"text": {"type": "string"}},
                                "required": ["text"],
                            },
                        }
                    ]
                }
            }

        assert params["method"] == "tools/call"
        self.tool_calls.append(params["params"])
        self.tool_started.set()
        await self.release_tool.wait()

        return {"result": {"content": [{"type": "text", "text": "Tool result 世界"}]}}

    async def new_session(self) -> str:
        result = await self.connection.send_request(
            "session/new",
            {
                "cwd": "/workspace",
                "mcpServers": [
                    {"type": "acp", "name": "workspace", "serverId": "workspace"}
                ],
            },
        )
        parsed = schema.NewSessionResponse.model_validate(result)
        await self.until("replay_complete")

        return parsed.session_id

    async def resume(self, session_id: str) -> None:
        result = await self.connection.send_request(
            "session/resume",
            {
                "sessionId": session_id,
                "cwd": "/workspace",
                "replayFrom": {"type": "start"},
            },
        )
        schema.ResumeSessionResponse.model_validate(result)
        await self.until("replay_complete")

    async def prompt(self, session_id: str, text: str) -> str:
        result = await self.connection.send_request(
            "session/prompt",
            {
                "sessionId": session_id,
                "prompt": [{"type": "text", "text": text}],
            },
        )

        return schema.PromptResponse.model_validate(result).message_id

    async def until(self, kind: str, *, message_id: str | None = None) -> None:
        async with asyncio.timeout(self.timeout), self.changed:
            await self.changed.wait_for(lambda: self._received(kind, message_id))

    def _received(self, kind: str, message_id: str | None) -> bool:
        for item in self.updates:
            if item.update.get("sessionUpdate") != kind:
                continue

            if message_id is None:
                return True

            if item.update.get("messageId") == message_id:
                return True

        return False

    def messages(self, kind: str) -> tuple[MessageUpdate, ...]:
        selected = (
            MessageUpdate.model_validate(item.update)
            for item in self.updates
            if item.update.get("sessionUpdate") == kind
        )

        return tuple(selected)

    def chunks(self) -> tuple[MessageChunk, ...]:
        selected = (
            MessageChunk.model_validate(item.update)
            for item in self.updates
            if item.update.get("sessionUpdate") == "agent_message_chunk"
        )

        return tuple(selected)

    async def close(self) -> None:
        self.release_tool.set()
        await self.adapter.close_all()
        await self.connection.close()
        await self.server.close()


class AgentSystem:
    """Стенд запускает настоящий SessionHub с PostgreSQL и локальным HTTP-инференсом."""

    def __init__(
        self,
        history: HistoryStore,
        inference: InferenceServer,
        postgres: PostgresConfig,
        timeout: float,
    ) -> None:
        self.history = history
        self.inference = inference
        self.postgres = postgres
        self.timeout = timeout
        self.owner = uuid4().hex
        self.profiles = AgentProfiles(
            {
                "hidden": AgentProfile(
                    roles=["administrators"],
                    model=ModelSettings(
                        provider=inference.settings, model="hidden-model"
                    ),
                ),
                "test": AgentProfile(
                    roles=["users"],
                    model=ModelSettings(
                        provider=inference.settings,
                        model="system-test-model",
                        sampling={"temperature": 0.2},
                        inference={"max_completion_tokens": 128},
                        provider_parameters={"parallel_tool_calls": True},
                        system_prompt="You are a test assistant.",
                    ),
                ),
                "alternative": AgentProfile(
                    roles=["operators"],
                    model=ModelSettings(
                        provider=inference.settings, model="alternative-model"
                    ),
                ),
            }
        )
        self.llm = OpenAiTransport(self.profiles)
        self.settings = SettingsLayer(
            mcp=McpSettings(list_timeout_sec=timeout, call_timeout_sec=timeout)
        )
        self.hub = SessionHub(history, self.llm, self.profiles, self.settings, "test")
        self.clients: list[AgentClient] = []

    async def client(
        self, *, owner: str | None = None, granted: frozenset[str] = frozenset({"test"})
    ) -> AgentClient:
        if owner is None:
            owner = self.owner

        client = AgentClient(self.hub, owner, self.timeout, granted)
        self.clients.append(client)
        await client.initialize()

        return client

    async def restart(self) -> None:
        for client in self.clients:
            await client.close()

        self.clients.clear()
        await self.hub.close()
        self.hub = SessionHub(
            self.history, self.llm, self.profiles, self.settings, "test"
        )
        await self.hub.start(self.postgres)

    async def idle(self, session_id: str) -> None:
        async with asyncio.timeout(self.timeout):
            await self.hub.agents[session_id].idle.wait()

        assert self.hub.agents[session_id].last_error is None

    async def assert_replay(self, session_id: str) -> None:
        rows = (await self.history.read(session_id)).rows
        boundaries = tuple(
            row for row in rows if isinstance(row, m.LlmRequestBoundaryRecord)
        )
        assert len(boundaries) == len(self.inference.requests)
        for boundary, sent in zip(boundaries, self.inference.requests, strict=True):
            by_id = await self.history.openai(session_id, boundary.record_id)
            by_seq = await self.history.openai(session_id, through_seq=boundary.seq)
            assert by_id.request == json.loads(sent)
            assert by_seq.request == by_id.request

    def answer(self, text: str) -> Response:
        return self.response({"content": text}, "stop")

    def call(self, call_id: str, name: str, arguments: Mapping[str, Any]) -> Response:
        return self.response(
            {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": call_id,
                        "function": {"name": name, "arguments": json.dumps(arguments)},
                    }
                ]
            },
            "tool_calls",
        )

    def response(self, delta: Mapping[str, Any], finish: str) -> Response:
        chunk = {"choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
        usage = {"choices": [], "usage": {"prompt_tokens": 7, "completion_tokens": 2}}
        body = (
            f"data: {json.dumps(chunk)}\n\n"
            f"data: {json.dumps(usage)}\n\ndata: [DONE]\n\n"
        )

        return Response(body, media_type="text/event-stream")

    async def close(self) -> None:
        for client in self.clients:
            await client.close()

        await self.hub.close()
        await self.llm.close()
        async with self.history.pool.connection() as connection:
            await connection.execute(
                "delete from boba.records where session_id in "
                "(select session_id from boba.sessions where owner_id = %s)",
                (self.owner,),
            )
            await connection.execute(
                "delete from boba.sessions where owner_id = %s", (self.owner,)
            )


@pytest.fixture
async def system(
    history: HistoryStore,
    inference: InferenceServer,
    postgres: PostgresConfig,
    agent_timeout: float,
) -> AsyncIterator[AgentSystem]:
    stand = AgentSystem(history, inference, postgres, agent_timeout)
    await stand.hub.start(postgres)
    try:
        yield stand

    finally:
        await stand.close()


async def test_acp_tools_accept_all_pending_inputs_and_resume_after_restart(  # noqa: PLR0915
    system: AgentSystem,
) -> None:
    first = await system.client()
    first.release_tool.clear()
    finish_response = asyncio.Event()

    async def body() -> AsyncIterator[bytes]:
        yield b'data: {"choices":[{"delta":{"content":"First "}}]}\n\n'
        await finish_response.wait()
        yield (
            b'data: {"choices":[{"delta":{"content":"answer"},'
            b'"finish_reason":"stop"}]}\n\n'
        )
        yield b"data: [DONE]\n\n"

    system.inference.responses = (
        system.call("select", "tool_search", {"select": ["mcp__workspace__echo"]}),
        system.call("execute", "mcp__workspace__echo", {"text": "Hello"}),
        StreamingResponse(body(), media_type="text/event-stream"),
    )
    sid = await first.new_session()
    try:
        await first.prompt(sid, "Start")
        async with asyncio.timeout(system.timeout):
            await first.tool_started.wait()

        second = await system.client()
        await second.resume(sid)
        texts = ("Next one", "Next two 世界\r\n", "Next three")
        ids = []
        for text in texts:
            ids.append(await first.prompt(sid, text))

        assert [
            row.record_id for row in await system.history.pending_inputs(sid)
        ] == ids
        assert len(system.inference.requests) == 2
        assert not second.chunks()
        first.release_tool.set()
        await second.until("agent_message_chunk")
        assert not system.hub.agents[sid].idle.is_set()
        assert second.chunks()[0].content.text == "First "
        final_request = json.loads(system.inference.requests[-1])
        assert [item["role"] for item in final_request["messages"]][-5:] == [
            "assistant",
            "tool",
            "user",
            "user",
            "user",
        ]
        assert [
            item["content"][0]["text"] for item in final_request["messages"][-3:]
        ] == list(texts)
        selected = json.loads(system.inference.requests[1])
        assert "mcp__workspace__echo" in [
            item["function"]["name"] for item in selected["tools"]
        ]
        assert "mcp__workspace__echo" not in [
            item["function"]["name"]
            for item in json.loads(system.inference.requests[0])["tools"]
        ]
        finish_response.set()
        await system.idle(sid)
        await second.until("agent_message", message_id=second.chunks()[0].message_id)
        chunks = second.chunks()
        final = next(
            message
            for message in second.messages("agent_message")
            if message.message_id == chunks[0].message_id
        )
        assert final.content[0].text == "First answer"
        assert not await system.history.pending_inputs(sid)
        await system.assert_replay(sid)

        await system.restart()
        resumed = await system.client()
        await resumed.resume(sid)
        restored = next(
            message
            for message in resumed.messages("agent_message")
            if message.message_id == final.message_id
        )
        assert restored == final
        assert len(resumed.messages("user_message")) == 4
        assert not resumed.chunks()
        assert len(system.inference.requests) == 3
        assert any(
            item.update.get("sessionUpdate") == "config_option_update"
            for item in resumed.updates
        )
        await system.assert_replay(sid)

    finally:
        finish_response.set()


@pytest.mark.parametrize("choice", ["session", "deny"])
async def test_permission_selection_controls_execution_and_survives_resume(
    system: AgentSystem, choice: str
) -> None:
    client = await system.client()
    client.permission = choice
    system.inference.responses = (
        system.call("select", "tool_search", {"select": ["mcp__workspace__echo"]}),
        system.call("first", "mcp__workspace__echo", {"text": "one"}),
        system.call("second", "mcp__workspace__echo", {"text": "two"}),
        system.answer("Finished"),
    )
    sid = await client.new_session()
    await client.prompt(sid, "Use echo twice")
    await system.idle(sid)
    rows = (await system.history.read(sid)).rows
    results = tuple(
        row
        for row in rows
        if isinstance(row, m.ToolResultRecord) and row.payload.call_id != "select"
    )
    assert len(results) == 2
    if choice == "session":
        assert len(client.permissions) == 1
        assert len(client.tool_calls) == 2
        assert all(not row.payload.is_error for row in results)

    else:
        assert len(client.permissions) == 2
        assert not client.tool_calls
        assert all(row.payload.is_error for row in results)

    assert any(isinstance(row, m.PermissionResponseRecord) for row in rows)
    await system.assert_replay(sid)
    await system.restart()
    resumed = await system.client()
    await resumed.resume(sid)
    assert not resumed.permissions
    assert len(system.inference.requests) == 4
    state = await system.history.latest(sid, kind="permission_state")
    if choice == "session":
        permissions = m.PermissionStateRecord.model_validate(state[0])
        assert any(
            rule.tool == "mcp__workspace__echo" and rule.decision == "allow"
            for rule in permissions.payload.rules
        )


async def test_acp_cancel_saves_partial_reply_and_next_prompt_recovers(
    system: AgentSystem,
) -> None:
    blocked = asyncio.Event()

    async def body() -> AsyncIterator[bytes]:
        yield b'data: {"choices":[{"delta":{"content":"Partial"}}]}\n\n'
        await blocked.wait()

    system.inference.responses = (
        StreamingResponse(body(), media_type="text/event-stream"),
        system.answer("Recovered"),
    )
    client = await system.client()
    sid = await client.new_session()
    try:
        await client.prompt(sid, "Start streaming")
        await client.until("agent_message_chunk")
        await client.connection.send_notification("session/cancel", {"sessionId": sid})
        await system.idle(sid)
        rows = (await system.history.read(sid)).rows
        reply = next(row for row in rows if isinstance(row, m.AssistantMessageRecord))
        assert reply.payload.completion == "cancelled"
        assert reply.payload.content == (m.TextPart(text="Partial"),)
        endings = tuple(row for row in rows if isinstance(row, m.TurnEndRecord))
        assert endings[-1].payload.outcome == "cancelled"
        await client.prompt(sid, "Continue")
        await system.idle(sid)
        request = json.loads(system.inference.requests[-1])
        assert request["messages"][-2]["content"][0]["text"] == "Partial"
        assert request["messages"][-1]["content"][0]["text"] == "Continue"
        await system.assert_replay(sid)

    finally:
        blocked.set()


async def test_retries_share_request_id_and_session_is_private(
    system: AgentSystem,
) -> None:
    system.inference.responses = (
        Response("busy", status_code=503),
        Response("busy", status_code=503),
        system.answer("Ready"),
    )
    client = await system.client()
    sid = await client.new_session()
    await client.prompt(sid, "Retry")
    await system.idle(sid)
    rows = (await system.history.read(sid)).rows
    boundaries = tuple(
        row for row in rows if isinstance(row, m.LlmRequestBoundaryRecord)
    )
    attempts = tuple(row for row in rows if isinstance(row, m.LlmRequestAttemptRecord))
    assert len(boundaries) == 1
    assert [row.payload.attempt for row in attempts] == [1, 2, 3]
    assert [row.payload.status_code for row in attempts] == [503, 503, 200]
    assert {row.request_id for row in attempts} == {boundaries[0].record_id}
    assert all(
        body == system.inference.requests[0] for body in system.inference.requests
    )
    assert (
        len(tuple(row for row in rows if isinstance(row, m.AssistantMessageRecord)))
        == 1
    )
    assert len(tuple(row for row in rows if isinstance(row, m.TokenUsageRecord))) == 1
    other = await system.client(owner=uuid4().hex)
    with pytest.raises(RequestError) as caught:
        await other.resume(sid)

    assert caught.value.code == -32002
    assert not other.updates
    listing = await other.connection.send_request("session/list", {})
    assert listing["sessions"] == []
    draft = await system.history.openai(sid, boundaries[0].record_id)
    assert draft.request == json.loads(system.inference.requests[0])


async def test_two_sessions_stream_concurrently_and_keep_separate_histories(
    system: AgentSystem,
) -> None:
    release = asyncio.Event()

    async def body() -> AsyncIterator[bytes]:
        yield b'data: {"choices":[{"delta":{"content":"Concurrent"}}]}\n\n'
        await release.wait()
        yield b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
        yield b"data: [DONE]\n\n"

    system.inference.responses = (
        StreamingResponse(body(), media_type="text/event-stream"),
        StreamingResponse(body(), media_type="text/event-stream"),
    )
    first = await system.client()
    second = await system.client()
    first_id = await first.new_session()
    second_id = await second.new_session()
    try:
        await asyncio.gather(
            first.prompt(first_id, "First session"),
            second.prompt(second_id, "Second session"),
        )
        await asyncio.gather(
            first.until("agent_message_chunk"), second.until("agent_message_chunk")
        )
        assert len(system.inference.requests) == 2
        assert not system.hub.agents[first_id].idle.is_set()
        assert not system.hub.agents[second_id].idle.is_set()
        assert {item.session_id for item in first.updates} == {first_id}
        assert {item.session_id for item in second.updates} == {second_id}
        release.set()
        await asyncio.gather(system.idle(first_id), system.idle(second_id))
        for sid, text in ((first_id, "First session"), (second_id, "Second session")):
            rows = (await system.history.read(sid)).rows
            users = tuple(row for row in rows if isinstance(row, m.UserMessageRecord))
            assert len(users) == 1
            assert users[0].payload.content == (m.TextPart(text=text),)
            boundary = next(
                row for row in rows if isinstance(row, m.LlmRequestBoundaryRecord)
            )
            rebuilt = await system.history.openai(sid, boundary.record_id)
            assert rebuilt.request in tuple(
                json.loads(body) for body in system.inference.requests
            )

    finally:
        release.set()


async def test_profile_catalog_controls_choices_switching_and_resume(
    system: AgentSystem,
) -> None:
    granted = frozenset({"test", "alternative"})
    assert system.profiles.granted_by_roles(frozenset({"users"})) == frozenset({"test"})
    assert system.profiles.granted_by_roles(frozenset({"operators"})) == frozenset(
        {"alternative"}
    )
    assert not system.profiles.granted_by_roles(frozenset())
    assert system.profiles.known("hidden")
    assert not system.profiles.known("unknown")
    assert system.profiles.default(granted) is system.profiles.require("test", granted)
    with pytest.raises(LookupError, match="not configured"):
        system.profiles.require("unknown", frozenset({"unknown"}))

    client = await system.client(granted=granted)
    sid = await client.new_session()
    options = await system.history.latest(sid, kind="parameter_option", key="model")
    definition = m.ParameterOptionRecord.model_validate(options[0])
    assert tuple(choice.value_id for choice in definition.payload.choices) == (
        "test",
        "alternative",
    )
    system.inference.responses = (
        system.answer("Original profile"),
        system.answer("Alternative profile"),
    )
    await client.prompt(sid, "First request")
    await system.idle(sid)
    first_request = json.loads(system.inference.requests[0])
    assert first_request["model"] == "system-test-model"
    assert first_request["temperature"] == 0.2
    assert first_request["max_completion_tokens"] == 128
    assert first_request["parallel_tool_calls"] is True
    head = await system.history.head(sid)
    with pytest.raises(RequestError) as rejected:
        await client.connection.send_request(
            "session/set_config_option",
            {
                "sessionId": sid,
                "configId": "model",
                "value": "hidden",
            },
        )

    assert rejected.value.code == -32602
    assert await system.history.head(sid) == head
    await client.connection.send_request(
        "session/set_config_option",
        {
            "sessionId": sid,
            "configId": "model",
            "value": "alternative",
        },
    )
    changes = (await system.history.read(sid, after_seq=head)).rows
    assert tuple(row.kind for row in changes) == (
        "model",
        "sampling_removed",
        "inference_removed",
        "provider_parameters_removed",
        "system_prompt_removed",
    )
    await client.prompt(sid, "Second request")
    await system.idle(sid)
    second_request = json.loads(system.inference.requests[1])
    assert second_request["model"] == "alternative-model"
    assert "temperature" not in second_request
    assert "max_completion_tokens" not in second_request
    assert "parallel_tool_calls" not in second_request
    assert all(message["role"] != "system" for message in second_request["messages"])
    await system.assert_replay(sid)
    await system.restart()
    resumed = await system.client(granted=granted)
    await resumed.resume(sid)
    state = await system.history.latest(sid, kind="model")
    assert m.ModelRecord.model_validate(state[0]).payload.provider == "alternative"
    assert len(system.inference.requests) == 2


async def test_unknown_or_missing_profile_grants_reject_new_session(
    system: AgentSystem,
) -> None:
    for granted in (frozenset(), frozenset({"unknown"})):
        client = await system.client(granted=granted)
        with pytest.raises(RequestError) as caught:
            await client.new_session()

        assert caught.value.code == -32002
        assert not client.updates

    assert not system.hub.agents
    assert not system.inference.requests
