from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest
from acp import RequestError
from acp.experimental.v2 import schema
from starlette.responses import StreamingResponse

from boba.agent import models as m

from .client import V2System

pytestmark = [pytest.mark.anyio, pytest.mark.integration]


async def test_delete_stops_shared_session_and_preserves_history(
    system: V2System,
) -> None:
    client = await system.client()
    sid = await client.new_session()
    observer = await system.client()
    await observer.resume(sid)
    release = asyncio.Event()

    async def body() -> AsyncIterator[bytes]:
        yield b'data: {"choices":[{"delta":{"content":"Partial"}}]}\n\n'
        await release.wait()

    system.inference.response = StreamingResponse(
        body(), media_type="text/event-stream"
    )
    await client.prompt(sid, "Start")
    await observer.until("agent_message_chunk")
    agent = system.hub.agents[sid]
    result = await client.connection.send_request("session/delete", {"sessionId": sid})
    schema.DeleteSessionResponse.model_validate(result)
    assert agent.closed
    assert not agent.subscribers
    assert sid not in system.hub.agents
    assert not observer.adapter.busy()
    rows = (await system.history.read(sid)).rows
    assert isinstance(rows[-1], m.SessionDeletedRecord)
    assert any(isinstance(row, m.AssistantMessageRecord) for row in rows)
    again = await client.connection.send_request("session/delete", {"sessionId": sid})
    schema.DeleteSessionResponse.model_validate(again)
    listed = await client.connection.send_request("session/list", {})
    assert schema.ListSessionsResponse.model_validate(listed).sessions == []
    await system.restart()
    restored = await system.client()
    with pytest.raises(RequestError) as failure:
        await restored.resume(sid)

    assert failure.value.code == -32002
    release.set()


async def test_fork_copies_history_and_continues_independently(
    system: V2System,
) -> None:
    client = await system.client()
    sid = await client.new_session()
    system.inference.responses = (
        system.call("select", "tool_search", {"select": ["mcp__workspace__echo"]}),
        system.call("execute", "mcp__workspace__echo", {"text": "Saved text"}),
        system.answer("Original answer"),
        system.answer("Branch answer"),
    )
    await client.prompt(sid, "Use the tool")
    await system.idle(sid)
    original = (await system.history.read(sid)).rows
    response = await client.connection.send_request(
        "session/fork",
        {
            "sessionId": sid,
            "cwd": "/branch",
            "additionalDirectories": ["/extra"],
            "mcpServers": [],
        },
    )
    branch = schema.ForkSessionResponse.model_validate(response).session_id
    assert branch != sid
    copied = (await system.history.read(branch)).rows
    for source, target in zip(original, copied, strict=False):
        assert target.model_dump(exclude={"session_id"}) == source.model_dump(
            exclude={"session_id"}
        )

    for source in original:
        if not isinstance(source, m.LlmRequestBoundaryRecord):
            continue

        first = await system.history.openai(sid, source.record_id)
        repeat = await system.history.openai(branch, source.record_id)
        assert repeat.request == first.request

    snapshot = await system.history.openai(
        branch, through_seq=await system.history.head(branch)
    )
    assert "mcp__workspace__echo" not in str(snapshot.request["tools"])
    environment = await system.history.latest(branch, kind="environment", key="client")
    state = m.EnvironmentRecord.model_validate(environment[0]).payload.state
    assert state is not None
    assert state.working_directory == "/branch"
    assert state.workspace_roots == ("/extra",)
    await client.prompt(branch, "Continue here")
    await system.idle(branch)
    assert (await system.history.read(sid)).rows == original
    assert len(system.inference.requests) == 4
    assert len(client.mcp.tool_calls) == 1
    await system.restart()
    restored = await system.client()
    result = await restored.connection.send_request(
        "session/resume",
        {"sessionId": branch, "cwd": "/branch", "replayFrom": {"type": "start"}},
    )
    schema.ResumeSessionResponse.model_validate(result)
    assert len(restored.messages("agent_message")) == 2


async def test_other_owner_cannot_fork_or_delete_session(system: V2System) -> None:
    client = await system.client()
    sid = await client.new_session()
    stranger = await system.client(owner="other-owner")
    with pytest.raises(RequestError) as failure:
        await stranger.connection.send_request(
            "session/fork", {"sessionId": sid, "cwd": "/workspace"}
        )

    assert failure.value.code == -32002
    await stranger.connection.send_request("session/delete", {"sessionId": sid})
    assert not system.hub.agents[sid].closed
    assert len(await system.history.latest(sid, kind="session_deleted")) == 0


async def test_fork_rejects_running_session_and_cleans_failed_open(
    system: V2System,
) -> None:
    client = await system.client()
    sid = await client.new_session()
    release = asyncio.Event()

    async def body() -> AsyncIterator[bytes]:
        yield b'data: {"choices":[{"delta":{"content":"Partial"}}]}\n\n'
        await release.wait()
        yield b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'

    system.inference.response = StreamingResponse(
        body(), media_type="text/event-stream"
    )
    await client.prompt(sid, "Start")
    await client.until("agent_message_chunk")
    try:
        with pytest.raises(RequestError) as running:
            await client.connection.send_request(
                "session/fork", {"sessionId": sid, "cwd": "/workspace"}
            )

        assert running.value.code == -32602
    finally:
        release.set()

    await system.idle(sid)
    client.mcp.connect_error = -32000
    with pytest.raises(RequestError):
        await client.connection.send_request(
            "session/fork",
            {
                "sessionId": sid,
                "cwd": "/workspace",
                "mcpServers": [
                    {"type": "acp", "name": "workspace", "serverId": "workspace"}
                ],
            },
        )

    listed = await client.connection.send_request("session/list", {})
    assert listed["sessions"] == [{"sessionId": sid, "cwd": "/workspace"}]
    assert set(system.hub.agents) == {sid}
    assert not system.hub.agents[sid].closed
