from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator
from uuid import uuid4

import pytest
from acp import RequestError, schema
from starlette.responses import StreamingResponse

from boba.agent import models as m
from boba.agent.tools import McpBinding

from .client import V1System

pytestmark = [pytest.mark.anyio, pytest.mark.integration]


async def test_v1_streams_before_prompt_response_and_replays_after_restart(
    system: V1System,
) -> None:
    client = await system.client()
    sid = await client.new_session()
    release = asyncio.Event()

    async def body() -> AsyncGenerator[bytes, None]:
        yield b'data: {"choices":[{"delta":{"reasoning_content":"Think"}}]}\n\n'
        yield b'data: {"choices":[{"delta":{"content":"First "}}]}\n\n'
        await release.wait()
        yield b'data: {"choices":[{"delta":{"content":"answer"},"finish_reason":"stop"}]}\n\n'
        yield b"data: [DONE]\n\n"

    system.inference.response = StreamingResponse(
        body(), media_type="text/event-stream"
    )
    prompt = asyncio.create_task(
        client.connection.send_request(
            "session/prompt",
            {"sessionId": sid, "prompt": [{"type": "text", "text": "First question"}]},
        )
    )
    try:
        await client.until("agent_message_chunk")
        assert not prompt.done()
        release.set()
        async with asyncio.timeout(system.timeout):
            result = await prompt

        assert schema.PromptResponse.model_validate(result).stop_reason == "end_turn"
        text = ""
        for item in client.updates:
            schema.SessionNotification.model_validate(item.model_dump(by_alias=True))
            if isinstance(item.update, schema.AgentMessageChunk):
                chunk = item.update
                assert isinstance(chunk.content, schema.TextContentBlock)
                text += chunk.content.text

        assert text == "First answer"
        assert "agent_message" not in tuple(client.received_kinds())
        assert "state_update" not in tuple(client.received_kinds())
        system.inference.response = system.answer("Second answer")
        result = await client.connection.send_request(
            "session/prompt",
            {"sessionId": sid, "prompt": [{"type": "text", "text": "Second question"}]},
        )
        assert schema.PromptResponse.model_validate(result).stop_reason == "end_turn"
        await system.assert_replay(sid)
        await system.restart()
        restored = await system.client()
        response = await restored.connection.send_request(
            "session/load", {"sessionId": sid, "cwd": "/workspace", "mcpServers": []}
        )
        schema.LoadSessionResponse.model_validate(response)
        messages = []
        for item in restored.updates:
            schema.SessionNotification.model_validate(item.model_dump(by_alias=True))
            if isinstance(item.update, schema.AgentMessageChunk):
                chunk = item.update
                assert isinstance(chunk.content, schema.TextContentBlock)
                messages.append(chunk.content.text)

        assert messages == ["First answer", "Second answer"]
    finally:
        release.set()
        if not prompt.done():
            prompt.cancel()


async def test_tool_search_and_observer_share_history(
    system: V1System,
) -> None:
    client = await system.client()
    system.inference.responses = (
        system.call("select", "tool_search", {"select": ["mcp__workspace__echo"]}),
        system.call("execute", "mcp__workspace__echo", {"text": "Hello"}),
        system.answer("Tool finished"),
    )
    sid = await client.new_session()
    observer = await system.client()
    await observer.resume(sid)
    result = await client.connection.send_request(
        "session/prompt",
        {"sessionId": sid, "prompt": [{"type": "text", "text": "Run the tool"}]},
    )
    assert schema.PromptResponse.model_validate(result).stop_reason == "end_turn"
    assert len(client.mcp.tool_calls) == 1
    kinds = tuple(client.received_kinds())
    assert kinds.count("tool_call") == 2
    assert kinds.count("tool_call_update") >= 4
    terminal = {}
    for item in client.updates:
        update = item.update
        if isinstance(update, schema.ToolCallProgress):
            terminal[update.tool_call_id] = update.status

    assert tuple(terminal.values()) == ("completed", "completed")
    assert "agent_message" not in kinds
    await observer.until("agent_message_chunk")
    assert observer.messages()[-1].content.text == "Tool finished"
    for item in client.updates:
        schema.SessionNotification.model_validate(item.model_dump(by_alias=True))

    await system.assert_replay(sid)


async def test_v1_permission_uses_legacy_tool_call_and_persists_session_choice(
    system: V1System,
) -> None:
    client = await system.client()
    client.permission = "session"
    system.inference.responses = (
        system.call("select", "tool_search", {"select": ["mcp__workspace__echo"]}),
        system.call("first", "mcp__workspace__echo", {"text": "one"}),
        system.call("second", "mcp__workspace__echo", {"text": "two"}),
        system.answer("Finished"),
    )
    sid = await client.new_session()
    result = await client.connection.send_request(
        "session/prompt",
        {"sessionId": sid, "prompt": [{"type": "text", "text": "Use echo twice"}]},
    )
    assert schema.PromptResponse.model_validate(result).stop_reason == "end_turn"
    assert len(client.permissions) == 1
    assert client.permissions[0].tool_call.tool_call_id == "first"
    assert len(client.mcp.tool_calls) == 2
    messages = []
    for item in client.updates:
        if not isinstance(item.update, schema.AgentMessageChunk):
            continue

        chunk = item.update
        assert isinstance(chunk.content, schema.TextContentBlock)
        messages.append(chunk.content.text)

    assert messages == ["Finished"]
    await system.assert_replay(sid)


async def test_v1_cancel_returns_cancelled_and_keeps_partial_text_once(
    system: V1System,
) -> None:
    client = await system.client()
    sid = await client.new_session()
    release = asyncio.Event()

    async def body() -> AsyncGenerator[bytes, None]:
        yield b'data: {"choices":[{"delta":{"content":"Partial"}}]}\n\n'
        await release.wait()
        yield b"data: [DONE]\n\n"

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
        await client.until("agent_message_chunk")
        await client.connection.send_notification("session/cancel", {"sessionId": sid})
        async with asyncio.timeout(system.timeout):
            result = await prompt

        assert schema.PromptResponse.model_validate(result).stop_reason == "cancelled"
        texts = []
        for item in client.updates:
            if isinstance(item.update, schema.AgentMessageChunk):
                chunk = item.update
                assert isinstance(chunk.content, schema.TextContentBlock)
                texts.append(chunk.content.text)

        assert texts == ["Partial"]
    finally:
        release.set()
        if not prompt.done():
            prompt.cancel()


async def test_mcp_reverse_requests_are_scoped_and_elicitation_is_persisted(
    system: V1System,
) -> None:
    client = await system.client()
    client.mcp.binding = McpBinding.CONNECTION
    sid = await client.new_session()
    client.mcp.callbacks = {
        "ping": {},
        "roots/list": {},
        "elicitation/create": {
            "mode": "form",
            "message": "Confirm reading this document",
            "requestedSchema": {
                "type": "object",
                "properties": {"confirm": {"type": "boolean"}},
                "required": ["confirm"],
            },
        },
    }
    system.inference.responses = (
        system.call("select", "tool_search", {"select": ["mcp__workspace__echo"]}),
        system.call("execute", "mcp__workspace__echo", {"text": "Read"}),
        system.answer("Read successfully"),
    )
    await client.connection.send_request(
        "session/prompt",
        {"sessionId": sid, "prompt": [{"type": "text", "text": "Read the document"}]},
    )
    await system.idle(sid)
    assert len(client.elicitations) == 1
    assert client.mcp.callback_results["ping"] == {}
    assert client.mcp.callback_results["roots/list"] == {
        "roots": [{"uri": "file:///workspace"}]
    }
    assert client.mcp.callback_results["elicitation/create"] == {
        "action": "accept",
        "content": {"confirm": True},
    }
    rows = (await system.history.read(sid)).rows
    questions = []
    answers = []
    for row in rows:
        if isinstance(row, m.ElicitationRequestRecord):
            questions.append(row)
        if isinstance(row, m.ElicitationResponseRecord):
            answers.append(row)
    assert len(questions) == len(answers) == 1
    assert questions[0].payload.call_id == "execute"
    assert questions[0].payload.related_message_id is not None
    assert answers[0].payload.elicitation_id == questions[0].record_id

    for connection_id, method in [
        ("unknown", "ping"),
        ("workspace-connection", "sampling/createMessage"),
        ("workspace-connection", "elicitation/create"),
    ]:
        values = client.mcp.callbacks.get(method, {})
        with pytest.raises(RequestError):
            await client.connection.send_request(
                "mcp/message",
                {"connectionId": connection_id, "method": method, "params": values},
            )

    stranger = await system.client(owner=uuid4().hex)
    with pytest.raises(RequestError, match="Unknown MCP connection"):
        await stranger.connection.send_request(
            "mcp/message", {"connectionId": "workspace-connection", "method": "ping"}
        )

    await system.assert_replay(sid)


async def test_missing_attachment_reader_preserves_tool_failure_without_chat_noise(
    system: V1System,
) -> None:
    client = await system.client()
    sid = await client.new_session()
    system.inference.responses = (
        system.call("select-reader", "tool_search", {"select": ["read_pdf"]}),
        system.answer("The attachment reader is unavailable."),
    )
    prompt = '[Attached file "school.pdf" is saved at: /client/attachments/school.pdf]'
    await client.connection.send_request(
        "session/prompt",
        {"sessionId": sid, "prompt": [{"type": "text", "text": prompt}]},
    )
    await system.idle(sid)

    records = (await system.history.read(sid)).rows
    results = []
    decisions = []
    for record in records:
        if isinstance(record, m.ToolResultRecord):
            if record.payload.status in {"pending", "running"}:
                continue
            results.append(record)

        if isinstance(record, m.PermissionDecisionRecord):
            decisions.append(record)

    assert len(results) == 1
    assert results[0].payload.status == "failed"
    error = results[0].payload.content[0]
    assert isinstance(error, m.TextPart)
    assert "tool_rejected" in error.text
    assert "Tool 'read_pdf' is not available in the current catalog" in error.text
    assert len(decisions) == 1
    assert decisions[0].payload.decision == "allow"
    assert not client.mcp.tool_calls

    request = json.loads(system.inference.requests[-1])
    assert request["messages"][-1]["role"] == "tool"
    assert request["messages"][-1]["content"] == [{"type": "text", "text": error.text}]
    first_request = json.loads(system.inference.requests[0])
    assert first_request["messages"][-1]["content"] == [
        {"type": "text", "text": prompt}
    ]
    for item in client.updates:
        if item.update.session_update == "tool_call_update":
            continue

        update_json = item.update.model_dump_json(by_alias=True)
        assert "permission_decision" not in update_json
        assert "Tool tool_search, call select-reader failed:" not in update_json

    await system.assert_replay(sid)


async def test_v1_task_status_returns_external_snapshot(system: V1System) -> None:
    from uuid import uuid4

    from boba.tasks.models import TaskCreate

    client = await system.client()
    sid = await client.new_session()
    command = TaskCreate(
        task_id=uuid4().hex, kind="scheduled", payload={}, run_timeout_sec=30
    )
    await system.hub.task_service.add(system.owner, command)
    system.inference.responses = (
        system.call("poll", "task_status", {"task_id": command.task_id}),
        system.answer("The task is pending"),
    )
    response = await client.connection.send_request(
        "session/prompt",
        {
            "sessionId": sid,
            "prompt": [{"type": "text", "text": "Check the task"}],
        },
    )
    assert schema.PromptResponse.model_validate(response).stop_reason == "end_turn"
    result = m.ToolResultRecord.model_validate(
        (await system.history.latest(sid, category="tool_result", key="poll"))[0]
    )
    assert result.payload.status == "completed"
    assert result.payload.state is not None
    assert result.payload.state.status == "pending"
    assert not client.mcp.tool_calls
    await system.assert_replay(sid)
