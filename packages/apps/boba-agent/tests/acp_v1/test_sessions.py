from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest
from acp import RequestError, schema
from starlette.responses import StreamingResponse

from boba.agent import models as m

from .client import V1System

pytestmark = [pytest.mark.anyio, pytest.mark.integration]


async def test_delete_cancels_prompt_for_all_observers(system: V1System) -> None:
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
    prompt = asyncio.create_task(
        client.connection.send_request(
            "session/prompt",
            {"sessionId": sid, "prompt": [{"type": "text", "text": "Start"}]},
        )
    )
    try:
        await observer.until("agent_message_chunk")
        agent = system.hub.agents[sid]
        result = await observer.connection.send_request(
            "session/delete", {"sessionId": sid}
        )
        schema.DeleteSessionResponse.model_validate(result)
        assert agent.closed
        assert not agent.subscribers
        assert sid not in system.hub.agents
        assert not client.adapter.busy()
        async with asyncio.timeout(system.timeout):
            response = await prompt

        assert schema.PromptResponse.model_validate(response).stop_reason == "cancelled"

        rows = (await system.history.read(sid)).rows
        assert isinstance(rows[-1], m.SessionDeletedRecord)
        await client.connection.send_request("session/delete", {"sessionId": sid})
        listed = await client.connection.send_request("session/list", {})
        assert schema.ListSessionsResponse.model_validate(listed).sessions == []
        await system.restart()
        restored = await system.client()
        with pytest.raises(RequestError) as failure:
            await restored.resume(sid)

        assert failure.value.code == -32002
    finally:
        release.set()
        if not prompt.done():
            prompt.cancel()


async def test_fork_preserves_saved_requests_and_runs_new_prompt(
    system: V1System,
) -> None:
    client = await system.client()
    sid = await client.new_session()
    system.inference.responses = (
        system.call("select", "tool_search", {"select": ["mcp__workspace__echo"]}),
        system.call("execute", "mcp__workspace__echo", {"text": "Saved text"}),
        system.answer("Original answer"),
        system.answer("Branch answer"),
    )
    response = await client.connection.send_request(
        "session/prompt",
        {"sessionId": sid, "prompt": [{"type": "text", "text": "Use the tool"}]},
    )
    assert schema.PromptResponse.model_validate(response).stop_reason == "end_turn"
    original = (await system.history.read(sid)).rows
    response = await client.connection.send_request(
        "session/fork", {"sessionId": sid, "cwd": "/branch", "mcpServers": []}
    )
    branch = schema.ForkSessionResponse.model_validate(response).session_id
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

    response = await client.connection.send_request(
        "session/prompt",
        {"sessionId": branch, "prompt": [{"type": "text", "text": "Continue here"}]},
    )
    assert schema.PromptResponse.model_validate(response).stop_reason == "end_turn"
    assert (await system.history.read(sid)).rows == original
    assert len(system.inference.requests) == 4
    assert len(client.mcp.tool_calls) == 1
    await system.restart()
    restored = await system.client()
    response = await restored.connection.send_request(
        "session/load", {"sessionId": branch, "cwd": "/branch", "mcpServers": []}
    )
    schema.LoadSessionResponse.model_validate(response)
    answers = []
    for notification in restored.updates:
        if isinstance(notification.update, schema.AgentMessageChunk):
            answers.append(notification.update)

    assert len(answers) == 2


async def test_other_owner_cannot_access_session_operations(system: V1System) -> None:
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


async def test_fork_running_or_failed_mcp_does_not_leave_active_branch(
    system: V1System,
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
    prompt = asyncio.create_task(
        client.connection.send_request(
            "session/prompt",
            {
                "sessionId": sid,
                "prompt": [{"type": "text", "text": "Start"}],
            },
        )
    )
    try:
        await client.until("agent_message_chunk")
        with pytest.raises(RequestError) as running:
            await client.connection.send_request(
                "session/fork", {"sessionId": sid, "cwd": "/workspace"}
            )

        assert running.value.code == -32602
    finally:
        release.set()

    async with asyncio.timeout(system.timeout):
        schema.PromptResponse.model_validate(await prompt)

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
