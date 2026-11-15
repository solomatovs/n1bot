from __future__ import annotations

import pytest
from acp import RequestError
from acp.experimental.v2 import schema

from boba.agent import models as m

from .client import V2System

pytestmark = [pytest.mark.anyio, pytest.mark.integration]


async def test_mode_config_updates_observers_and_tool_policy(system: V2System) -> None:
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
    response = await client.connection.send_request(
        "session/set_config_option",
        {
            "sessionId": sid,
            "configId": "mode",
            "value": "plan",
        },
    )
    schema.SetSessionConfigOptionResponse.model_validate(response)
    await observer.until("config_option_update")
    update = schema.ConfigOptionUpdate.model_validate(observer.updates[-1].update)
    options = update.config_options
    assert any(
        item.config_id == "mode" and item.current_value == "plan" for item in options
    )
    current = await system.history.latest(sid, kind="permission_state")
    state = m.PermissionStateRecord.model_validate(current[0]).payload
    assert state.mode is m.PermissionMode.PLAN
    assert state.rules == (rule,)

    system.inference.responses = (
        system.call("search", "tool_search", {"select": ["mcp__workspace__echo"]}),
        system.call("denied", "mcp__workspace__echo", {"text": "Not executed"}),
        system.answer("The policy denied execution"),
    )
    await client.prompt(sid, "Try the tool")
    await system.idle(sid)
    assert client.mcp.tool_calls == []
    assert client.permissions == []
    await system.restart()
    restored = await system.client()
    response = await restored.connection.send_request(
        "session/resume",
        {
            "sessionId": sid,
            "cwd": "/workspace",
            "replayFrom": {"type": "start"},
        },
    )
    loaded = schema.ResumeSessionResponse.model_validate(response)
    assert loaded.config_options is not None
    assert any(
        item.config_id == "mode" and item.current_value == "plan"
        for item in loaded.config_options
    )
    with pytest.raises(RequestError) as failure:
        await restored.connection.send_request(
            "session/set_config_option",
            {
                "sessionId": sid,
                "configId": "mode",
                "value": "bypass_permissions",
            },
        )

    assert failure.value.code == -32602
