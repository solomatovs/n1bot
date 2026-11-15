from __future__ import annotations

import pytest
from acp import RequestError, schema

from boba.agent import models as m

from .client import V1System

pytestmark = [pytest.mark.anyio, pytest.mark.integration]


async def test_mode_changes_tool_policy_and_survives_load(system: V1System) -> None:
    client = await system.client()
    sid = await client.new_session()
    observer = await system.client()
    await observer.resume(sid)
    rule = m.PermissionRule(tool="never", decision="deny")
    await system.hub.agents[sid].append(
        [
            system.hub.make(
                m.PermissionStateRecord, payload={"mode": "default", "rules": [rule]}
            )
        ]
    )
    observer.updates.clear()
    result = await client.connection.send_request(
        "session/set_mode", {"sessionId": sid, "modeId": "plan"}
    )
    schema.SetSessionModeResponse.model_validate(result)
    await observer.until("current_mode_update")
    update = observer.updates[-1].update
    assert isinstance(update, schema.CurrentModeUpdate)
    assert update.current_mode_id == "plan"
    current = await system.history.latest(sid, kind="permission_state")
    state = m.PermissionStateRecord.model_validate(current[0]).payload
    assert state.mode is m.PermissionMode.PLAN
    assert state.rules == (rule,)

    system.inference.responses = (
        system.call("search", "tool_search", {"select": ["mcp__workspace__echo"]}),
        system.call("denied", "mcp__workspace__echo", {"text": "Not executed"}),
        system.answer("The policy denied execution"),
    )
    result = await client.connection.send_request(
        "session/prompt",
        {
            "sessionId": sid,
            "prompt": [{"type": "text", "text": "Try the tool"}],
        },
    )
    assert schema.PromptResponse.model_validate(result).stop_reason == "end_turn"
    assert client.mcp.tool_calls == []
    assert client.permissions == []
    await system.restart()
    restored = await system.client()
    response = await restored.connection.send_request(
        "session/load",
        {
            "sessionId": sid,
            "cwd": "/workspace",
            "mcpServers": [],
        },
    )
    loaded = schema.LoadSessionResponse.model_validate(response)
    assert loaded.modes is not None
    assert loaded.modes.current_mode_id == "plan"
    with pytest.raises(RequestError) as failure:
        await restored.connection.send_request(
            "session/set_mode",
            {
                "sessionId": sid,
                "modeId": "bypass_permissions",
            },
        )

    assert failure.value.code == -32602
