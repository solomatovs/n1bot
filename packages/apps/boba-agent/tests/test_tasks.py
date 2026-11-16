import asyncio
from uuid import uuid4

import pytest
from agent_stand import Llm

from boba.agent import models as m
from boba.agent.acp_server import AgentProfiles, SessionHub
from boba.agent.runtime import Agent
from boba.agent.settings import (
    AgentProfile,
    ModelSettings,
    PermissionSettings,
    SettingsLayer,
)
from boba.llm.http.openai import WireChunk


class BackgroundTool:
    def __init__(self):
        self.finish = asyncio.Event()

    async def call(self, agent, reply, call):
        async def action(task_id):
            await self.finish.wait()
            return (m.TextPart(text="Background result 世界"),)

        return await agent.task(reply, call, action, background=True)


@pytest.mark.anyio
@pytest.mark.integration
async def test_background_result_replaces_initial_snapshot_and_resumes_generation(
    history,
    agent_timeout: float,
):
    sid = "background-runtime-" + uuid4().hex
    llm, tool = Llm(), BackgroundTool()
    agent = Agent(sid, history, llm, tool, version="test")
    try:
        await history.create_session(
            sid,
            "test",
            "/workspace",
            [
                agent.make(
                    m.ModelRecord,
                    payload={"provider": "test", "value": {"model": "test"}},
                )
            ],
        )
        await agent.submit([m.TextPart(text="Start")])
        async with asyncio.timeout(agent_timeout):
            await agent.idle.wait()
        assert agent.last_error is None
        assert agent.background
        assert len(llm.requests) == 2
        tool.finish.set()
        await asyncio.gather(*tuple(agent.background))
        async with asyncio.timeout(agent_timeout):
            await agent.idle.wait()
        assert agent.last_error is None
        assert len(llm.requests) == 3
        assert "Background result 世界" in str(llm.requests[-1]["messages"])
        rows = (await history.read(sid)).rows
        results = [r for r in rows if isinstance(r, m.ToolResultRecord)]
        assert [r.payload.status for r in results] == [
            "pending",
            "running",
            "completed",
        ]
        assert len({r.key for r in results}) == 1
        assert results[1].payload.state is not None
        assert results[2].payload.state is not None
        assert results[1].payload.state.task_id == results[2].payload.state.task_id
        assert results[2].payload.state.revision > results[1].payload.state.revision
        assert results[2].payload.content == (
            m.TextPart(text="Background result 世界"),
        )
        assert all(r.category != "input" for r in rows)

    finally:
        await agent.close()
        async with history.pool.connection() as conn:
            await conn.execute("delete from boba.records where session_id = %s", (sid,))


@pytest.mark.anyio
@pytest.mark.integration
async def test_subagent_has_own_history_and_parent_call_keeps_normal_pair(
    history, system, agent_timeout: float
):
    class SubagentLlm(Llm):
        async def generate(self, provider, request, output, emit):
            self.requests.append(request)
            output.status = 200
            output.address = "https://test.invalid/chat/completions"
            if len(self.requests) == 1:
                message = {
                    "tool_calls": [
                        {
                            "id": "child-call",
                            "type": "function",
                            "function": {
                                "name": "agent",
                                "arguments": '{"prompt": "child", "background": false}',
                            },
                        }
                    ]
                }
            else:
                message = {
                    "content": "child done"
                    if len(self.requests) == 2
                    else "parent done"
                }
            for delta in output.response.deltas(
                WireChunk.model_validate(
                    {"choices": [{"message": message, "finish_reason": "stop"}]}
                )
            ):
                await emit(delta.position, delta.kind, delta.text)

    llm = SubagentLlm()
    model = ModelSettings(model="test")
    settings = SettingsLayer(
        permissions=PermissionSettings(
            default_mode="bypassPermissions", allow_bypass=True
        )
    )
    profiles = AgentProfiles({"test": AgentProfile(roles=["*"], model=model)})
    hub = SessionHub(
        history, llm, profiles, settings, "test", providers=system.providers
    )
    ids = []
    try:
        parent = await hub.create(system.owner, frozenset({"test"}), "/workspace")
        await parent.submit([m.TextPart(text="parent")])
        async with asyncio.timeout(agent_timeout):
            await parent.idle.wait()
        assert parent.last_error is None
        assert len(hub.agents) == 2
        child = next(agent for agent in hub.agents.values() if agent is not parent)
        assert child.last_error is None
        assert len(llm.requests) == 3
        parent_rows = (await history.read(parent.session_id)).rows
        child_rows = (await history.read(child.session_id)).rows
        assert any(row.kind == "subagent_session" for row in child_rows)
        result = next(row for row in reversed(parent_rows) if row.kind == "tool_result")
        assert result.payload.status == "completed"
        assert result.payload.content[0].text == "child done"
        assert [part["role"] for part in llm.requests[-1]["messages"]][-3:] == [
            "user",
            "assistant",
            "tool",
        ]
    finally:
        ids = list(hub.agents)
        await hub.close()
        async with history.pool.connection() as conn:
            await conn.execute(
                "delete from boba.records where session_id = any(%s)", (ids,)
            )
            await conn.execute(
                "delete from boba.sessions where session_id = any(%s)", (ids,)
            )
