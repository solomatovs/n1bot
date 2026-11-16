import asyncio
from uuid import uuid4

import pytest
from agent_stand import Llm

from boba.agent import models as m
from boba.agent.acp_server import AgentProfiles, SessionHub
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
        await self.finish.wait()
        return (m.TextPart(text="Background result 世界"),)


@pytest.mark.anyio
@pytest.mark.integration
async def test_background_result_replaces_initial_snapshot_and_resumes_generation(
    history,
    task_runtime,
    agent_timeout: float,
):
    sid = "background-runtime-" + uuid4().hex
    llm, tool = Llm(), BackgroundTool()
    agent = task_runtime.create(sid, "test", llm, tool)
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
            while True:
                rows = await history.latest(sid, category="tool_call")
                if rows:
                    break
                await asyncio.sleep(0.01)

        assert agent.tasks is not None
        assert await agent.tasks.has_pending()
        assert len(llm.requests) == 1
        tool.finish.set()
        async with asyncio.timeout(agent_timeout):
            await agent.idle.wait()
        assert agent.last_error is None
        assert len(llm.requests) == 2
        assert "Background result 世界" in str(llm.requests[-1]["messages"])
        rows = (await history.read(sid)).rows
        results = [r for r in rows if isinstance(r, m.ToolResultRecord)]
        assert results[0].payload.status == "pending"
        assert results[-1].payload.status == "completed"
        assert len({r.key for r in results}) == 1
        assert results[0].payload.state is not None
        assert results[-1].payload.state is not None
        assert results[0].payload.state.task_id == results[-1].payload.state.task_id
        assert results[-1].payload.state.revision > results[0].payload.state.revision
        assert results[-1].payload.content == (
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
    await hub.start(system.postgres)
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


@pytest.mark.anyio
@pytest.mark.integration
async def test_nested_subagents_do_not_starve_with_one_worker_slot(
    history, system, agent_timeout
):
    from boba.tasks.models import WorkerConfig

    class NestedLlm(Llm):
        async def generate(self, provider, request, output, emit):
            self.requests.append(request)
            output.status = 200
            output.address = "https://test.invalid/chat/completions"
            number = len(self.requests)
            message = {"content": f"Done {number}"}
            if number <= 2:
                message = {
                    "tool_calls": [
                        {
                            "id": f"child-{number}",
                            "type": "function",
                            "function": {
                                "name": "agent",
                                "arguments": '{"prompt":"child"}',
                            },
                        }
                    ]
                }
            if number == 3:
                message = {
                    "tool_calls": [
                        {
                            "id": "search",
                            "type": "function",
                            "function": {
                                "name": "tool_search",
                                "arguments": '{"query":"agent"}',
                            },
                        }
                    ]
                }
            chunk = WireChunk.model_validate(
                {"choices": [{"message": message, "finish_reason": "stop"}]}
            )
            for delta in output.response.deltas(chunk):
                await emit(delta.position, delta.kind, delta.text)

    llm = NestedLlm()
    settings = SettingsLayer(
        tasks=WorkerConfig(concurrency=1, poll_sec=0.02),
        permissions=PermissionSettings(
            default_mode="bypassPermissions", allow_bypass=True
        ),
    )
    hub = SessionHub(
        history, llm, system.profiles, settings, "test", providers=system.providers
    )
    await hub.start(system.postgres)
    try:
        parent = await hub.create(system.owner, frozenset({"test"}), "/workspace")
        await parent.submit([m.TextPart(text="parent")])
        async with asyncio.timeout(agent_timeout):
            await parent.idle.wait()

        assert parent.last_error is None
        assert len(hub.agents) == 3
        assert len(llm.requests) == 6
        for agent in hub.agents.values():
            assert agent.idle.is_set()
            assert agent.last_error is None
            assert agent.tasks is not None
            assert not await agent.tasks.has_pending()

    finally:
        await hub.close()


@pytest.mark.anyio
@pytest.mark.integration
async def test_task_continuations_share_generation_limit(
    history, task_runtime, agent_timeout
):
    class CallingLlm(Llm):
        async def generate(self, provider, request, output, emit):
            self.requests.append(request)
            output.status = 200
            output.address = "https://test.invalid/chat/completions"
            message = {
                "tool_calls": [
                    {
                        "id": f"call-{len(self.requests)}",
                        "type": "function",
                        "function": {"name": "echo", "arguments": "{}"},
                    }
                ]
            }
            chunk = WireChunk.model_validate(
                {"choices": [{"message": message, "finish_reason": "stop"}]}
            )
            for delta in output.response.deltas(chunk):
                await emit(delta.position, delta.kind, delta.text)

    class Echo:
        async def call(self, agent, reply, call):
            return (m.TextPart(text="done"),)

    sid = "task-limit-" + uuid4().hex
    llm = CallingLlm()
    agent = task_runtime.create(sid, "test", llm, Echo())
    agent.max_steps = 2
    await history.create_session(
        sid,
        "test",
        "/workspace",
        [
            agent.make(
                m.ModelRecord, payload={"provider": "test", "value": {"model": "test"}}
            ),
        ],
    )
    try:
        await agent.submit([m.TextPart(text="Start")])
        async with asyncio.timeout(agent_timeout):
            await agent.idle.wait()
        assert len(llm.requests) == 2
        end = m.TurnEndRecord.model_validate(
            (await history.latest(sid, kind="turn_end"))[0]
        )
        assert end.payload.outcome == "limit"
        await agent.submit([m.TextPart(text="Continue")])
        async with asyncio.timeout(agent_timeout):
            await agent.idle.wait()
        assert len(llm.requests) == 4
        assert agent.last_error is None

    finally:
        await agent.close()
        async with history.pool.connection() as connection:
            await connection.execute(
                "delete from boba.records where session_id = %s", (sid,)
            )
            await connection.execute(
                "delete from boba.sessions where session_id = %s", (sid,)
            )
