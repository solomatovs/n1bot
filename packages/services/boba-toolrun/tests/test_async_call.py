"""Потоковый вызов из event loop'а: кадры туда и обратно без блокировки loop'а."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from pydantic import SecretStr

from boba.stand.fake_toolmod import FakeChunkHead, FakeConfig
from boba.toolkit.frames import ToolFrame
from boba.toolkit.protocol import ReplyOk, ToolCommand
from boba.toolrun.process import ProcessLauncherConfig, ProcessToolCaller
from boba.toolrun.streaming import AsyncToolCall

CFG = FakeConfig(token=SecretStr("t0ken"), limit=5)

STREAM_ARGV = ("python3", "-m", "boba.stand.fake_toolmod", "fake_stream")


def _launcher(workdir: Path) -> ProcessToolCaller:
    values: dict[str, object] = {
        "provider": "process",
        "workdir": str(workdir),
        "shell": "/bin/bash",
        "timeout_sec": 60.0,
        "channel_limit_bytes": 1_000_000,
        "stderr_tail_bytes": 4096,
        "kill_grace_sec": 1.0,
    }

    return ProcessToolCaller("fake", ProcessLauncherConfig.model_validate(values))


def _command(prefix: str) -> ToolCommand:
    config = json.dumps({"cfg": CFG.revealed()}).encode("utf-8")
    return ToolCommand(argv=(*STREAM_ARGV, "--prefix", prefix), config=config)


class TestAsyncToolCall:
    def test_frames_and_envelope_arrive_in_loop(self, tmp_path: Path) -> None:
        async def go() -> tuple[list[bytes], object]:
            call = AsyncToolCall.opened(_launcher(tmp_path), _command("a:"))

            await call.send(ToolFrame.of(FakeChunkHead(seq=1), b"one"))
            await call.send(ToolFrame.of(FakeChunkHead(seq=2), b"two"))
            await call.done_sending()

            bodies: list[bytes] = []
            async for frame in call.frames():
                bodies.append(frame.body)

            outcome = await call.result()
            return bodies, outcome.reply

        bodies, reply = asyncio.run(go())

        assert bodies[:2] == [b"a:one", b"a:two"]
        assert isinstance(reply, ReplyOk)
        assert "streamed 2" in reply.content

    def test_loop_stays_responsive_while_call_runs(self, tmp_path: Path) -> None:
        """Насос и чтение живут в потоках: loop крутит свои задачи параллельно."""

        async def go() -> int:
            call = AsyncToolCall.opened(_launcher(tmp_path), _command("b:"))
            ticks = 0

            async def tick() -> None:
                nonlocal ticks
                while True:
                    await asyncio.sleep(0.01)
                    ticks += 1

            ticker = asyncio.create_task(tick())

            await call.send(ToolFrame.of(FakeChunkHead(seq=1), b"x"))
            await call.done_sending()

            async for _ in call.frames():
                continue

            await call.result()
            ticker.cancel()

            return ticks

        ticks = asyncio.run(go())

        assert ticks > 0
