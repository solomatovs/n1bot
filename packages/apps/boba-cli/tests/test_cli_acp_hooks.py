"""Хук PreToolUse через вход ACP: процесс `boba acp --settings <файл 0600 с
hooks>`, сервер MCP стенда в `session/new.mcpServers`, стенд-обработчик по
HTTP. Обработчик получает событие с полным именем `mcp__<server>__<tool>` и
заголовком из файла; сервер MCP — подменённый аргумент; `tool_call.rawInput`
и журнал — аргументы модели; `deny` обработчика — вызов `failed` с причиной."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from acp import text_block
from test_cli_acp import (
    AcpProcess,
    FakeModel,
    McpStand,
    RecordingClient,
    boba,
    call,
    handshake,
    result_text,
)

from boba.cli.acp import StopReasonName
from boba.stand.hooks import HookStandRules, HookStandServer

pytestmark = pytest.mark.anyio

__all__ = ["boba"]

SERVER = "stand"
SHOUT = f"mcp__{SERVER}__shout"
ADD = f"mcp__{SERVER}__add"
AUTHORIZATION = "Bearer jwt-1"
SENT_SHOUT = {"text": "hi", "intent": "stand call of shout"}


def hooks_section(handler: HookStandServer) -> dict[str, Any]:
    return {
        "PreToolUse": [
            {
                "matcher": f"mcp__{SERVER}__.*",
                "hooks": [
                    {
                        "type": "http",
                        "url": handler.url,
                        "headers": {"Authorization": AUTHORIZATION},
                        "timeout": 5,
                        "onFailure": "block",
                    }
                ],
            }
        ]
    }


def tool_use_inputs(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for record in records:
        if record["type"] != "assistant":
            continue

        for block in record["message"]["content"]:
            if block["type"] == "tool_use":
                found.append(block["input"])

    return found


class TestHooks:
    async def test_settings_hook_seals_before_the_server_and_denies(
        self, boba: AcpProcess, mcp: McpStand, model: FakeModel, tmp_path: Path
    ) -> None:
        handler = HookStandServer(
            HookStandRules(
                replacements={SHOUT: {"text": "sealed hi"}},
                denied={ADD: "no grant for add"},
            )
        )
        await handler.start()
        try:
            await self._run(boba, mcp, tmp_path, handler)
        finally:
            await handler.stop()

    @staticmethod
    async def _run(
        boba: AcpProcess, mcp: McpStand, tmp_path: Path, handler: HookStandServer
    ) -> None:
        client = RecordingClient()
        document = boba.settings_document(mode="bypassPermissions", allow_bypass=True)
        document["hooks"] = hooks_section(handler)
        conn = await boba.start(client, document)
        try:
            await handshake(conn)
            session = await conn.new_session(
                cwd=str(tmp_path), mcp_servers=[mcp.server(SERVER)]
            )
            session_id = session.session_id

            response = await conn.prompt(
                session_id=session_id,
                prompt=[
                    text_block(call({"name": "shout", "arguments": {"text": "hi"}}))
                ],
            )
            assert response.stop_reason == StopReasonName.END_TURN.value
            start = client.starts()[0]
            assert start.raw_input == SENT_SHOUT
            done = client.progress_of(start.tool_call_id)[-1]
            assert done.status == "completed"
            assert result_text(done) == "SEALED HI"

            received = handler.app.received
            assert len(received) == 1
            event = received[0].event
            assert event.tool_name == SHOUT
            assert event.tool_input == SENT_SHOUT
            assert event.tool_use_id == start.tool_call_id
            assert event.session_id == session_id
            assert event.cwd == str(tmp_path)
            assert event.permission_mode.value == "bypassPermissions"
            assert event.mcp_server is not None
            assert event.mcp_server.name == SERVER
            assert "text" in event.tool_schema["properties"]
            assert received[0].headers["authorization"] == AUTHORIZATION

            response = await conn.prompt(
                session_id=session_id,
                prompt=[
                    text_block(call({"name": "add", "arguments": {"a": 1, "b": 2}}))
                ],
            )
            assert response.stop_reason == StopReasonName.END_TURN.value
            denied = client.starts()[1]
            failed = client.progress_of(denied.tool_call_id)[-1]
            assert failed.status == "failed"
            assert result_text(failed) == (
                "The PreToolUse hook denied the call of add: no grant for add"
            )
            assert handler.app.received[1].event.tool_name == ADD
        finally:
            await conn.close()
            await boba.close_stdin()

        assert await boba.wait() == 0
        assert tool_use_inputs(boba.journal(session_id))[0] == SENT_SHOUT
        assert AUTHORIZATION not in json.dumps(boba.journal(session_id))
