"""Текущее состояние при resume: живой ход, ожидание разрешения и наблюдатель.

Проверки идут через JSON-RPC настоящих процессов boba с FakeLlmApp.
Исторические исходы передаются только объявившему расширение клиенту.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from acp.experimental.v2 import schema
from test_cli_acp import (
    AcpClient,
    AcpProcess,
    FakeModel,
    StandRegistration,
    call,
    handshake,
)

from boba.stand.ui.fake_llm import FailureName, ScenarioName
from boba.transport.acp.protocol import BobaExtension, HistoryUpdate, StopReason

pytestmark = pytest.mark.anyio


class TestResumeState:
    @pytest.mark.parametrize("replay", [False, True])
    @pytest.mark.parametrize("permission", [False, True])
    async def test_resume_preserves_current_activity(
        self,
        boba: AcpProcess,
        registration: StandRegistration,
        tmp_path: Path,
        replay: bool,
        permission: bool,
    ) -> None:
        client = AcpClient()
        client.hold = asyncio.Event()
        conn = await boba.start(client, registration)
        try:
            await handshake(conn)
            opened = await conn.new_session(
                cwd=str(tmp_path), mcp_servers=[StandRegistration.STAND]
            )
            session_id = opened.session_id
            tool = {
                "name": "mcp__stand__progress",
                "arguments": {"steps": 1, "seconds": 30},
            }
            expected = schema.RunningSessionStateUpdate()
            if permission:
                tool = {"name": "mcp__stand__shout", "arguments": {"text": "hello"}}
                expected = schema.RequiresActionSessionStateUpdate()

            await conn.prompt(session_id=session_id, prompt=[*call(tool)])
            if permission:
                await client.next_permission(0)
            else:
                async with asyncio.timeout(10):
                    while registration.received[-1].method != "tools/call":
                        await asyncio.sleep(0.01)

            before = len(client.updates_of(session_id))
            cursor = None
            if replay:
                cursor = schema.ReplayFromStartVariant()

            await conn.resume_session(
                cwd=str(tmp_path),
                session_id=session_id,
                mcp_servers=[StandRegistration.STAND],
                replay_from=cursor,
            )
            updates = client.updates_of(session_id)[before:]
            states = [u for u in updates if u.session_update == "state_update"]
            assert states == [expected]
            assert updates[-1] == expected
            questions = [u for u in updates if isinstance(u, schema.UserMessageUpdate)]
            assert bool(questions) is replay

            request_id = boba.frames.request_id("session/resume")
            answered_at = boba.frames.position_of_response(request_id)
            notices = boba.frames.incoming_notifications(answered_at)
            assert notices[-1]["params"]["update"]["state"] == expected.state

            await conn.cancel_session(session_id=session_id)
            stopped = await client.idle(session_id)
            assert stopped.stop_reason == StopReason.CANCELLED
            assert client.updates_of(session_id)[-1] is stopped
        finally:
            client.hold.set()
            await conn.close()
            await boba.close_stdin()

        assert await boba.wait() == 0

    @pytest.mark.parametrize("history", [False, True])
    async def test_observer_reports_unknown_and_negotiates_history(
        self,
        boba: AcpProcess,
        registration: StandRegistration,
        model: FakeModel,
        tmp_path: Path,
        history: bool,
    ) -> None:
        owner = AcpClient()
        document = boba.settings_document()
        document["limits"]["apiRetries"] = 0
        conn = await boba.start(owner, registration, document)
        observer = AcpProcess(tmp_path, model)
        try:
            await handshake(conn)
            opened = await conn.new_session(cwd=str(tmp_path), mcp_servers=[])
            session_id = opened.session_id
            await boba.turn(
                owner,
                session_id,
                [schema.TextContentBlock(text=ScenarioName.ANSWER.value)],
            )
            broken = f"{ScenarioName.ANSWER.value} {FailureName.STATUS_500.value}"
            _, failed = await boba.turn(
                owner, session_id, [schema.TextContentBlock(text=broken)]
            )
            assert failed.stop_reason == StopReason.REFUSAL

            client = AcpClient()
            observing = await observer.start(client, registration, document)
            try:
                await handshake(observing, history=history)
                await observing.resume_session(
                    cwd=str(tmp_path),
                    session_id=session_id,
                    mcp_servers=[],
                    replay_from=schema.ReplayFromStartVariant(),
                )
                updates = client.updates_of(session_id)
                states = [u for u in updates if u.session_update == "state_update"]
                assert states == [schema.OtherSessionStateUpdate(state="unknown")]
                assert not client.of(schema.SessionNotice)
                assert len(client.of(schema.UserMessageUpdate)) == 2
                assert client.of(schema.AgentMessageUpdate)
                recorded = [
                    HistoryUpdate.model_validate(u.model_dump(by_alias=True))
                    for u in client.of(schema.OtherSessionUpdate)
                    if u.session_update == BobaExtension.HISTORY.value
                ]
                if history:
                    assert [u.event.kind for u in recorded] == [
                        "turn_end",
                        "notice",
                        "turn_end",
                    ]
                else:
                    assert recorded == []

                request_id = observer.frames.request_id("session/resume")
                answered_at = observer.frames.position_of_response(request_id)
                notices = observer.frames.incoming_notifications(answered_at)
                assert notices[-1]["params"]["update"]["state"] == "unknown"
            finally:
                await observing.close()
                await observer.close_stdin()

            assert await observer.wait() == 0
        finally:
            await observer.kill()
            await conn.close()
            await boba.close_stdin()

        assert await boba.wait() == 0
