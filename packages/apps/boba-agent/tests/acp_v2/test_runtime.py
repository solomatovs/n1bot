import asyncio
from uuid import uuid4

import pytest
from agent_stand import Llm

from boba.agent import models as m
from boba.agent.acp_server import AcpOperations
from boba.agent.acp_v2 import AcpV2
from boba.agent.runtime import Agent


class Tool:
    def __init__(self):
        self.started = asyncio.Event()
        self.finish = asyncio.Event()

    async def call(self, agent, reply, call):
        self.started.set()
        await self.finish.wait()
        return (m.TextPart(text="Result"),)


@pytest.mark.anyio
@pytest.mark.integration
async def test_inputs_persist_during_tool_and_enter_one_request_with_live_acp(
    history, inference, system, agent_timeout: float
):
    sid = "runtime-test-" + uuid4().hex
    llm, tool = Llm(), Tool()
    agent = Agent(sid, history, llm, tool, version="test")
    events = []

    async def send(method, params):
        events.append((method, params))

    try:
        await agent.append(
            [
                agent.make(
                    m.ModelRecord,
                    payload={"provider": "test", "value": {"model": "test"}},
                )
            ]
        )
        from boba.agent.acp_server import SessionHub
        from boba.agent.settings import SettingsLayer

        hub = SessionHub(
            history,
            llm,
            inference.profiles,
            SettingsLayer(),
            "test",
            providers=system.providers,
        )
        operations = AcpOperations(hub, "runtime-test", frozenset({"test"}), "test")
        await agent.subscribe(send, AcpV2(operations))
        await agent.submit([m.TextPart(text="Start")])
        async with asyncio.timeout(agent_timeout):
            await tool.started.wait()
        submitted = []
        for text in ("Первое", "Второе\r\n世界", "Третье"):
            submitted.append(await agent.submit([m.TextPart(text=text)]))
        pending = await history.pending_inputs(sid)
        assert [item.record_id for item in pending] == submitted
        assert all(item.seq is not None for item in pending)
        assert len(llm.requests) == 1
        tool.finish.set()
        async with asyncio.timeout(agent_timeout):
            await agent.idle.wait()
        assert agent.last_error is None
        assert len(llm.requests) == 2
        messages = llm.requests[-1]["messages"]
        assert [x["role"] for x in messages] == [
            "user",
            "assistant",
            "tool",
            "user",
            "user",
            "user",
        ]
        assert [x["content"][0]["text"] for x in messages[-3:]] == [
            "Первое",
            "Второе\r\n世界",
            "Третье",
        ]
        rows = (await history.read(sid)).rows
        boundaries = [r for r in rows if r.category == "request"]
        assert (
            await history.openai(sid, boundaries[0].record_id)
        ).request == llm.requests[0]
        assert (
            await history.openai(sid, through_seq=boundaries[1].seq)
        ).request == llm.requests[1]
        assert len([r for r in rows if r.kind == "token_usage"]) == 2
        assert len([r for r in rows if r.kind == "assistant_message"]) == 2
        assert not await history.pending_inputs(sid)
        # Let the outgoing worker flush committed notifications.
        await asyncio.sleep(0.02)
        updates = [
            params["update"] for method, params in events if method == "session/update"
        ]
        chunks = [u for u in updates if u["sessionUpdate"] == "agent_message_chunk"]
        assert chunks
        complete = [
            u
            for u in updates
            if u["sessionUpdate"] == "agent_message"
            and u["messageId"] == chunks[0]["messageId"]
        ]
        assert len(complete) == 1
        assert complete[0]["content"][0]["text"] == chunks[0]["content"]["text"]
    finally:
        await agent.close()
        async with history.pool.connection() as conn:
            await conn.execute("delete from boba.records where session_id = %s", (sid,))
