from uuid import uuid4

import mcp.types as mt
import pytest
from acp import RequestError

from boba.agent import models as m
from boba.agent.runtime import Agent
from boba.agent.tools import ToolRunner
from boba.llm.http.openai import WireChunk


class Summary:
    async def generate(self, provider, request, output, emit):
        assert request["tool_choice"] == "none"
        output.status = 200
        output.address = "https://test.invalid/v1/chat/completions"
        for delta in output.response.deltas(
            WireChunk.model_validate(
                {
                    "choices": [
                        {
                            "message": {"content": "Saved summary 世界\r\n"},
                            "finish_reason": "stop",
                        }
                    ]
                }
            )
        ):
            await emit(delta.position, delta.kind, delta.text)


@pytest.mark.anyio
@pytest.mark.integration
async def test_catalog_changes_and_compaction_keep_replay_deterministic(history):
    sid = "producer-test-" + uuid4().hex
    tools = ToolRunner()
    agent = Agent(sid, history, Summary(), tools, version="test")
    remote = {
        "alpha": {
            "name": "alpha",
            "description": "First",
            "inputSchema": {"type": "object", "properties": {}},
        }
    }

    async def request(method, params):
        if method == "mcp/connect":
            raise RequestError(-32601, "Method not found")

        assert method == "mcp/message"
        if params["method"] == "server/discover":
            return {
                "result": {
                    "supportedVersions": ["2026-07-28"],
                    "capabilities": {"tools": {}},
                }
            }

        assert params["method"] == "tools/list"
        return {"result": {"tools": list(remote.values())}}

    async def notify(method, params):
        raise AssertionError(f"Unexpected notification: {method}")

    async def build():
        head = await history.head(sid)
        return head, (await history.openai(sid, through_seq=head)).request

    try:
        user = agent.make(
            m.UserMessageRecord, payload={"content": [m.TextPart(text="Hello")]}
        )
        await agent.append(
            [
                agent.make(
                    m.ModelRecord,
                    payload={"provider": "test", "value": {"model": "test"}},
                ),
                user,
                *tools.builtins(agent),
            ]
        )
        await tools.connect(
            agent,
            [{"type": "acp", "name": "workspace", "serverId": "ws"}],
            request,
            notify,
            set(),
            mt.ClientCapabilities(),
        )
        await tools.search(agent, {"select": ["mcp__workspace__alpha"]})
        first_id, first = await build()
        head = await history.head(sid)
        remote["alpha"]["inputSchema"]["properties"]["changed"] = {"type": "string"}
        await tools.refresh(agent)
        assert (
            await history.head(sid) == head
        )  # A list refresh does not replace a selected schema.
        _, same = await build()
        assert same["tools"] == first["tools"]
        del remote["alpha"]
        await tools.refresh(agent)
        _, removed = await build()
        assert [item["function"]["name"] for item in removed["tools"]] == [
            "task_status",
            "tool_search",
        ]
        remote["alpha"] = {
            "name": "alpha",
            "description": "Again",
            "inputSchema": {"type": "object", "properties": {}},
        }
        await tools.refresh(agent)
        _, readded = await build()
        assert readded["tools"] == removed["tools"]
        await tools.search(agent, {"select": ["mcp__workspace__alpha"]})
        await agent.append(
            [
                agent.make(
                    m.ToolAddedRecord,
                    key="mcp__workspace__alpha",
                    payload={"tool": {"description": "Updated catalog description"}},
                )
            ]
        )
        before_id, before = await build()
        await tools.compact(agent)
        _, after = await build()
        assert after["tools"] == before["tools"]
        assert after["messages"] == [
            {
                "role": "user",
                "content": [{"type": "text", "text": "Saved summary 世界\r\n"}],
            }
        ]
        assert (await history.openai(sid, through_seq=first_id)).request == first
        assert (await history.openai(sid, through_seq=before_id)).request == before

        await tools.compact(agent)
        _, twice = await build()
        assert twice["tools"] == before["tools"]

        del remote["alpha"]
        await tools.refresh(agent)
        remote["alpha"] = {
            "name": "alpha",
            "description": "Returned after compaction",
            "inputSchema": {"type": "object", "properties": {}},
        }
        await tools.refresh(agent)
        await tools.compact(agent)
        _, without_schema = await build()
        assert without_schema["tools"] == removed["tools"]
        assert (await history.openai(sid, through_seq=before_id)).request == before

        rows = (await history.read(sid)).rows
        assert any(row.kind == "request_output" for row in rows)
        assert any(row.copied_from_id for row in rows)
    finally:
        await agent.close()
        async with history.pool.connection() as conn:
            await conn.execute("delete from boba.records where session_id = %s", (sid,))


@pytest.mark.anyio
@pytest.mark.integration
async def test_compaction_preserves_pending_call_group_and_remaps_results(history):
    sid = "compact-pending-" + uuid4().hex
    tools = ToolRunner()
    agent = Agent(sid, history, Summary(), tools, version="test")
    try:
        model = agent.make(
            m.ModelRecord, payload={"provider": "test", "value": {"model": "test"}}
        )
        origin = agent.make(
            m.NoticeRecord,
            payload={"level": "info", "content": [m.TextPart(text="Origin")]},
        )
        await agent.append([model, origin])
        assistant = agent.make(
            m.AssistantMessageRecord,
            parent_operation_id=origin.operation_id,
            payload={
                "content": [
                    m.ToolCallReferencePart(call_id="a"),
                    m.ToolCallReferencePart(call_id="b"),
                ]
            },
        )
        pieces: list[m.HistoryRecord] = [assistant]
        calls = []
        for key in ("a", "b"):
            call = agent.make(
                m.ToolCallRecord,
                key=key,
                payload={
                    "assistant_record_id": assistant.record_id,
                    "name": "test",
                    "arguments": {},
                },
            )
            calls.append(call)
            pieces.extend(
                [
                    call,
                    agent.make(
                        m.ToolResultRecord,
                        key=key,
                        payload={
                            "call_record_id": call.record_id,
                            "status": "pending",
                            "content": [m.TextPart(text="Pending " + key)],
                        },
                    ),
                ]
            )
        await agent.append(
            agent.records.group(pieces, assistant.operation_id, origin.operation_id)
        )
        latest = agent.make(
            m.ToolResultRecord,
            key="a",
            payload={
                "call_record_id": calls[0].record_id,
                "status": "completed",
                "content": [m.TextPart(text="Completed a")],
            },
        )
        await agent.append([latest])
        original = await history.openai(sid)
        await tools.compact(agent)
        rebuilt = await history.openai(sid)
        assert rebuilt.request["messages"][1:] == original.request["messages"]
        rows = (await history.read(sid)).rows
        copies = {
            row.copied_from_id: row for row in rows if row.copied_from_id is not None
        }
        copied_call = m.ToolCallRecord.model_validate(copies[calls[0].record_id])
        copied_result = m.ToolResultRecord.model_validate(copies[latest.record_id])
        assert (
            copied_call.payload.assistant_record_id
            == copies[assistant.record_id].record_id
        )
        assert copied_result.payload.call_record_id == copied_call.record_id
        assert copied_result.operation_id != copied_call.operation_id
    finally:
        await agent.close()
        async with history.pool.connection() as connection:
            await connection.execute(
                "delete from boba.records where session_id = %s", (sid,)
            )
