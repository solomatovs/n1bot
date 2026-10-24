"""Сессии одного процесса `boba --stdio` в своих потоках: ход одной сессии не
ждёт хода другой, отмена хода одной сессии другую не трогает, предел
открытых сессий отказывает с понятной причиной и освобождается закрытием.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import ClassVar

import pytest
from acp.exceptions import RequestError
from acp.experimental.v2 import schema
from test_cli_acp import (
    READ_TIMEOUT_SEC,
    AcpClient,
    AcpProcess,
    StandRegistration,
    call,
    handshake,
)

from boba.cli.acp_server import ContentBlock
from boba.stand.ui.fake_llm import ScenarioName
from boba.transport.acp.protocol import StopReason

pytestmark = pytest.mark.anyio


class TestParallelSessions:
    SLOW: ClassVar[dict[str, object]] = {
        "name": "mcp__stand__progress",
        "arguments": {"steps": 1, "seconds": 30},
    }

    async def test_one_session_answers_while_another_runs_and_is_cancelled(
        self, boba: AcpProcess, registration: StandRegistration, tmp_path: Path
    ) -> None:
        client = AcpClient()
        document = boba.settings_document(mode="bypassPermissions", allow_bypass=True)
        conn = await boba.start(client, registration, document)
        try:
            await handshake(conn)
            slow = await conn.new_session(
                cwd=str(tmp_path), mcp_servers=[StandRegistration.STAND]
            )
            quick = await conn.new_session(
                cwd=str(tmp_path), mcp_servers=[StandRegistration.STAND]
            )
            await conn.prompt(session_id=slow.session_id, prompt=[*call(self.SLOW)])
            async with asyncio.timeout(READ_TIMEOUT_SEC):
                while not registration.received:
                    await asyncio.sleep(0.05)

                while registration.received[-1].method != "tools/call":
                    await asyncio.sleep(0.05)

            answer: list[ContentBlock] = [
                schema.TextContentBlock(text=ScenarioName.ANSWER.value)
            ]
            await conn.prompt(session_id=quick.session_id, prompt=answer)
            done = await client.idle(quick.session_id)

            assert done.stop_reason == StopReason.END_TURN
            slow_idles = [
                update
                for update in client.updates_of(slow.session_id)
                if isinstance(update, schema.IdleSessionStateUpdate)
            ]
            assert slow_idles == []

            await conn.cancel_session(session_id=slow.session_id)
            stopped = await client.idle(slow.session_id)
            assert stopped.stop_reason == StopReason.CANCELLED

            await conn.prompt(session_id=quick.session_id, prompt=answer)
            again = await client.idle(quick.session_id)
            assert again.stop_reason == StopReason.END_TURN
        finally:
            await conn.close()
            await boba.close_stdin()

        assert await boba.wait() == 0


class TestSessionLimit:
    async def test_limit_refuses_and_closing_frees_a_place(
        self, boba: AcpProcess, registration: StandRegistration, tmp_path: Path
    ) -> None:
        client = AcpClient()
        document = boba.settings_document()
        document["sessions"] = {"maxOpen": 1}
        conn = await boba.start(client, registration, document)
        try:
            await handshake(conn)
            first = await conn.new_session(cwd=str(tmp_path), mcp_servers=[])
            with pytest.raises(RequestError) as refused:
                await conn.new_session(cwd=str(tmp_path), mcp_servers=[])

            assert "its limit is 1" in str(refused.value.data)

            await conn.close_session(session_id=first.session_id)
            second = await conn.new_session(cwd=str(tmp_path), mcp_servers=[])
            assert second.session_id != first.session_id
        finally:
            await conn.close()
            await boba.close_stdin()

        assert await boba.wait() == 0
