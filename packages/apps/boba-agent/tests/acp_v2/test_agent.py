from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
from acp import RequestError
from starlette.responses import JSONResponse, Response, StreamingResponse

from boba.agent import models as m
from boba.agent.tools import McpBinding

from .client import V2System

pytestmark = [pytest.mark.anyio, pytest.mark.integration]


async def test_v2_resume_replays_only_when_requested_and_before_response(
    system: V2System,
) -> None:
    first = await system.client()
    first.wire.clear()
    sid = await first.new_session()
    initial = tuple(first.received_kinds())
    assert "replay_complete" not in initial
    assert initial.index("state_update") < initial.index("response")

    system.inference.response = system.answer("Saved answer")
    await first.prompt(sid, "Saved question")
    await system.idle(sid)

    resumed = await system.client()
    resumed.wire.clear()
    response = await resumed.connection.send_request(
        "session/resume", {"sessionId": sid, "cwd": "/workspace"}
    )
    assert "availableCommands" not in response
    assert response["configOptions"]
    kinds = tuple(resumed.received_kinds())
    assert "replay_complete" not in kinds
    assert "user_message" not in kinds
    assert resumed.messages("agent_message") == ()
    assert kinds.index("state_update") < kinds.index("response")

    replayed = await system.client()
    replayed.wire.clear()
    await replayed.resume(sid)
    kinds = tuple(replayed.received_kinds())
    assert kinds.index("user_message") < kinds.index("replay_complete")
    assert kinds.index("agent_message") < kinds.index("replay_complete")
    assert kinds.index("replay_complete") < kinds.index("state_update")
    assert kinds.index("state_update") < kinds.index("response")
    assert len(replayed.messages("agent_message")) == 1
    assert "notice" not in kinds
    assert len(system.inference.requests) == 1

    head = await system.history.head(sid)
    with pytest.raises(RequestError) as failure:
        await replayed.connection.send_request(
            "session/resume",
            {"sessionId": sid, "cwd": "/workspace", "replayFrom": {"type": "_unknown"}},
        )

    assert failure.value.code == -32602
    assert await system.history.head(sid) == head


async def test_standard_client_context_keeps_user_role_and_profile_system_prompt(
    system: V2System,
) -> None:
    client = await system.client()
    sid = await client.new_session()
    system.inference.response = system.answer("Context received")
    await client.connection.send_request(
        "session/prompt",
        {
            "sessionId": sid,
            "prompt": [
                {"type": "text", "text": "Review this context"},
                {
                    "type": "resource",
                    "resource": {
                        "uri": "file:///workspace/AGENTS.md",
                        "mimeType": "text/plain",
                        "text": "Client instructions 世界\r\n",
                    },
                },
            ],
        },
    )
    await system.idle(sid)
    request = json.loads(system.inference.requests[0])
    assert request["messages"][0] == {
        "role": "system",
        "content": [{"type": "text", "text": "You are a test assistant."}],
    }
    assert request["messages"][-1]["role"] == "user"
    assert (
        request["messages"][-1]["content"][-1]["text"] == "Client instructions 世界\r\n"
    )
    instructions = await system.history.latest(sid, category="prompt", key="client")
    assert instructions == ()
    await system.assert_replay(sid)


@pytest.mark.parametrize("binding", [McpBinding.REQUEST, McpBinding.CONNECTION])
async def test_mcp_binding_is_negotiated_before_tool_execution(
    system: V2System, binding: McpBinding
) -> None:
    client = await system.client()
    client.mcp.binding = binding
    system.inference.responses = (
        system.call("select", "tool_search", {"select": ["mcp__workspace__echo"]}),
        system.call("call", "mcp__workspace__echo", {"text": "Test"}),
        system.answer("Done"),
    )
    sid = await client.new_session()
    peer = system.hub.tools.clients[sid]["workspace"]
    assert peer.state().binding is binding
    assert peer.state().capabilities.tools is not None
    if binding is McpBinding.CONNECTION:
        assert client.mcp.methods[:4] == [
            "mcp/connect",
            "initialize",
            "notifications/initialized",
            "tools/list",
        ]
        assert peer.state().protocol_version == "2025-11-25"

    else:
        assert client.mcp.methods[:2] == ["mcp/connect", "server/discover"]
        assert peer.state().protocol_version == "2026-07-28"
        assert "initialize" not in client.mcp.methods

    await client.prompt(sid, "Run the tool")
    await system.idle(sid)
    assert len(client.mcp.tool_calls) == 1
    await system.assert_replay(sid)
    await client.connection.send_request(
        "session/resume",
        {
            "sessionId": sid,
            "cwd": "/workspace",
            "mcpServers": [
                {"type": "acp", "name": "workspace", "serverId": "workspace"}
            ],
        },
    )
    assert client.mcp.methods.count("mcp/connect") == 1
    assert system.hub.tools.clients[sid]["workspace"] is peer
    await client.close()
    if binding is McpBinding.CONNECTION:
        assert client.mcp.methods[-1] == "mcp/disconnect"


async def test_tool_search_finds_tools_by_words_in_a_long_query(
    system: V2System,
) -> None:
    client = await system.client()
    sid = await client.new_session()
    system.inference.responses = (
        system.call("search", "tool_search", {"query": "unknown words echo file"}),
        system.answer("Found echo"),
    )
    await client.prompt(sid, "Find the echo tool")
    await system.idle(sid)
    request = json.loads(system.inference.requests[-1])
    result = request["messages"][-1]
    assert result["role"] == "tool"
    assert "mcp__workspace__echo" in json.dumps(result["content"])
    await system.assert_replay(sid)


@pytest.mark.parametrize("code", [-32000, -32602, -32603])
async def test_mcp_negotiation_does_not_fallback_after_client_error(
    system: V2System, code: int
) -> None:
    client = await system.client()
    client.mcp.connect_error = code
    with pytest.raises(RequestError) as failure:
        await client.new_session()

    assert failure.value.code == code
    assert client.mcp.methods == ["mcp/connect"]
    assert not system.hub.tools.clients
    assert not system.inference.requests


async def test_mcp_negotiation_rejects_unsupported_version(system: V2System) -> None:
    client = await system.client()
    client.mcp.versions = ["2099-01-01"]
    with pytest.raises(RequestError) as failure:
        await client.new_session()

    assert failure.value.code == -32004
    assert "2099-01-01" in str(failure.value)
    assert client.mcp.methods == ["mcp/connect", "server/discover"]
    assert not system.hub.tools.clients


async def test_modern_mcp_can_use_negotiated_connection_binding(
    system: V2System,
) -> None:
    client = await system.client()
    client.mcp.binding = McpBinding.CONNECTION
    client.mcp.protocol_version = "2026-07-28"
    sid = await client.new_session()
    peer = system.hub.tools.clients[sid]["workspace"]
    assert peer.state().binding is McpBinding.CONNECTION
    assert peer.state().protocol_version == "2026-07-28"
    assert client.mcp.methods == [
        "mcp/connect",
        "initialize",
        "server/discover",
        "tools/list",
    ]


async def test_mcp_timeout_does_not_select_another_binding(system: V2System) -> None:
    client = await system.client()
    client.mcp.connect_ready.clear()
    system.hub.tools.list_timeout = 0.02
    try:
        with pytest.raises(RequestError) as failure:
            await client.new_session()

        assert failure.value.code == -32004
        assert "negotiation exceeded" in str(failure.value)
        assert client.mcp.methods == ["mcp/connect"]
        assert not system.hub.tools.clients

    finally:
        client.mcp.connect_ready.set()


async def test_acp_tools_accept_all_pending_inputs_and_resume_after_restart(  # noqa: PLR0915
    system: V2System,
) -> None:
    first = await system.client()
    first.mcp.release_tool.clear()
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
            await first.mcp.tool_started.wait()

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
        first.mcp.release_tool.set()
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
    system: V2System, choice: str
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
        assert len(client.mcp.tool_calls) == 2
        assert all(not row.payload.is_error for row in results)

    else:
        assert len(client.permissions) == 2
        assert not client.mcp.tool_calls
        assert all(row.payload.is_error for row in results)

    assert any(isinstance(row, m.PermissionResponseRecord) for row in rows)
    assert len(client.messages("agent_message")) == 1
    assert client.messages("agent_message")[0].content[0].text == "Finished"
    await system.assert_replay(sid)
    await system.restart()
    resumed = await system.client()
    await resumed.resume(sid)
    assert not resumed.permissions
    assert len(resumed.messages("agent_message")) == 1
    assert resumed.messages("agent_message")[0].content[0].text == "Finished"
    assert len(system.inference.requests) == 4
    state = await system.history.latest(sid, kind="permission_state")
    if choice == "session":
        permissions = m.PermissionStateRecord.model_validate(state[0])
        assert any(
            rule.tool == "mcp__workspace__echo" and rule.decision == "allow"
            for rule in permissions.payload.rules
        )


@pytest.mark.parametrize(
    ("answer", "decision", "failed"),
    [
        ('{"decision":"allow"}', "allow", False),
        ('{"decision":"deny"}', "deny", False),
        ('{"decision":"allow","unexpected":true}', "deny", True),
        ("not json", "deny", True),
        ("http_error", "deny", True),
        ("truncated", "deny", True),
    ],
)
async def test_permission_classifier_saves_failure_and_denies_execution(
    system: V2System, answer: str, decision: str, failed: bool
) -> None:
    finish = "stop"
    content = answer
    if answer == "truncated":
        finish = "length"
        content = '{"decision":"allow"}'

    classification: Response = JSONResponse(
        {"choices": [{"message": {"content": content}, "finish_reason": finish}]}
    )
    if answer == "http_error":
        classification = JSONResponse(
            {"error": {"message": "Classifier unavailable"}}, status_code=400
        )

    system.inference.responses = (
        system.call("select", "tool_search", {"select": ["mcp__workspace__echo"]}),
        system.call("execute", "mcp__workspace__echo", {"text": "Hello"}),
        classification,
        system.answer("Finished"),
    )
    client = await system.client()
    sid = await client.new_session()
    agent = system.hub.agents[sid]
    state = agent.make(m.PermissionStateRecord, payload={"mode": "auto", "rules": []})
    await agent.append([state])
    await client.prompt(sid, "Use echo")
    await system.idle(sid)
    decisions = await system.history.latest(sid, kind="permission_decision")
    verdict = m.PermissionDecisionRecord.model_validate(decisions[0])
    assert verdict.payload.call_id == "execute"
    assert verdict.payload.decision == decision
    assert bool(client.mcp.tool_calls) == (decision == "allow")
    assert not client.permissions
    notices = await system.history.latest(sid, kind="notice")
    if failed:
        assert len(notices) == 1
        notice = m.TextNoticeRecord.model_validate(notices[0])
        assert notice.payload.level == "error"
        assert notice.payload.content[0].text == verdict.payload.reason
        assert "execution denied" in verdict.payload.reason
        assert "mcp__workspace__echo" in verdict.payload.reason
        assert notice.request_id is not None
        assert notice.request_id in verdict.payload.reason
        await client.until("notice")

    else:
        assert not notices

    request = json.loads(system.inference.requests[2])
    assert "response_format" in request
    assert "tools" not in request
    assert "response_format" not in json.loads(system.inference.requests[3])
    await system.assert_replay(sid)


async def test_acp_cancel_saves_partial_reply_and_next_prompt_recovers(
    system: V2System,
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
    system: V2System,
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
    system: V2System,
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
    system: V2System,
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
    system: V2System,
) -> None:
    for granted in (frozenset(), frozenset({"unknown"})):
        client = await system.client(granted=granted)
        with pytest.raises(RequestError) as caught:
            await client.new_session()

        assert caught.value.code == -32002
        assert not client.updates

    assert not system.hub.agents
    assert not system.inference.requests


async def test_reasoning_survives_next_request_and_agent_restart(
    system: V2System,
) -> None:
    client = await system.client()
    sid = await client.new_session()
    reasoning = "Saved reasoning  世界\r\n"
    system.inference.response = system.response(
        {"reasoning_content": reasoning, "content": "First answer"}, "stop"
    )
    await client.prompt(sid, "First question")
    await system.idle(sid)

    system.inference.response = system.answer("Second answer")
    await client.prompt(sid, "Second question")
    await system.idle(sid)
    request = json.loads(system.inference.requests[1])
    assistants = []
    for message in request["messages"]:
        if message["role"] != "assistant":
            continue

        assistants.append(message)

    assert assistants == [
        {
            "role": "assistant",
            "reasoning_content": reasoning,
            "content": [{"type": "text", "text": "First answer"}],
        }
    ]

    await system.restart()
    client = await system.client()
    await client.resume(sid)
    system.inference.response = system.answer("Third answer")
    await client.prompt(sid, "Third question")
    await system.idle(sid)
    assert len(system.inference.requests) == 3
    await system.assert_replay(sid)


async def test_mcp_reverse_requests_are_scoped_and_elicitation_is_persisted(
    system: V2System,
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
    system: V2System,
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
    await client.until("agent_message")

    records = (await system.history.read(sid)).rows
    results = []
    decisions = []
    for record in records:
        if isinstance(record, m.ToolResultRecord):
            results.append(record)

        if isinstance(record, m.PermissionDecisionRecord):
            decisions.append(record)

    assert len(results) == 1
    assert results[0].payload.is_error
    error = results[0].payload.content[0]
    assert isinstance(error, m.TextPart)
    assert "Tool tool_search, call select-reader failed:" in error.text
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
        if item.update["sessionUpdate"] == "tool_call_update":
            continue

        assert "permission_decision" not in json.dumps(item.update)
        assert "Tool tool_search, call select-reader failed:" not in json.dumps(
            item.update
        )

    await system.assert_replay(sid)
