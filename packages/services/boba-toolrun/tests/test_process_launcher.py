"""Запуск инструмента субпроцессом хоста: конверт, каналы, отказы, таймаут."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from pydantic import SecretStr

from boba.cancellation import ToolStopped
from boba.stand.fake_toolmod import FakeChunkHead, FakeConfig
from boba.toolkit.entry import ToolMain
from boba.toolkit.frames import ToolFrame
from boba.toolkit.launcher import (
    ChannelOverflowError,
    CollectedCall,
    LauncherError,
    PayloadFailureError,
)
from boba.toolkit.protocol import ReplyError, ReplyOk, ToolCommand
from boba.toolkit.wrap import ToolProcessWrap
from boba.toolrun.process import (
    ProcessCallError,
    ProcessLauncherConfig,
    ProcessToolCaller,
)

CFG = FakeConfig(token=SecretStr("t0ken"), limit=5)

MODULE_ARGV = ("python3", "-m", "boba.stand.fake_toolmod", "fake_echo")


def _launcher(workdir: Path, **overrides: object) -> ProcessToolCaller:
    values: dict[str, object] = {
        "provider": "process",
        "workdir": str(workdir),
        "shell": "/bin/bash",
        "timeout_sec": 60.0,
        "channel_limit_bytes": 1_000_000,
        "stderr_tail_bytes": 4096,
        "kill_grace_sec": 1.0,
    }
    values.update(overrides)

    return ProcessToolCaller("fake", ProcessLauncherConfig.model_validate(values))


def _fresh_tool():
    """Свежий tool-объект: guard_all подменяет тела на месте."""
    from importlib import reload

    from boba.stand import fake_toolmod

    reload(fake_toolmod)
    return ToolMain.toolset(fake_toolmod.fake_echo)[0]


def _call(tool, **kwargs: object) -> tuple[object, object]:
    """await coroutine-тела: у фасадного тула тело асинхронное."""

    async def go() -> object:
        return await tool.coroutine(**kwargs)

    result = asyncio.run(go())
    assert isinstance(result, tuple)

    content, artifact = result
    return content, artifact


class TestRunTool:
    def test_envelope_round_trip(self, tmp_path: Path) -> None:
        tool = _fresh_tool()
        ToolProcessWrap.guard_all([tool], _launcher(tmp_path))

        content, artifact = _call(tool, text="hi", repeat=2, cfg=CFG)

        assert "hi hi" in str(content)
        assert artifact is not None

    def test_expected_failure_becomes_payload_error(self, tmp_path: Path) -> None:
        tool = _fresh_tool()
        ToolProcessWrap.guard_all([tool], _launcher(tmp_path))

        with pytest.raises(PayloadFailureError) as err:
            _call(tool, text="boom", repeat=1, cfg=CFG)

        assert err.value.kind == "fake_unavailable"

    def test_entry_error_arrives_as_error_reply(self, tmp_path: Path) -> None:
        launcher = _launcher(tmp_path)
        command = ToolCommand(argv=(*MODULE_ARGV, "--text", "hi"), config=b"{}")

        outcome = CollectedCall.of(launcher, command)

        assert isinstance(outcome.reply, ReplyError)
        assert outcome.run.exit_code == int(ToolMain.Exit.ENTRY_ERROR)

    def test_missing_envelope_is_refused(self, tmp_path: Path) -> None:
        launcher = _launcher(tmp_path)
        argv = ("python3", "-m", "boba.no_such_toolmod", "fake_echo")
        command = ToolCommand(argv=argv, config=b"")

        with pytest.raises(LauncherError, match="no envelope"):
            CollectedCall.of(launcher, command)

    def test_non_module_command_is_refused(self, tmp_path: Path) -> None:
        launcher = _launcher(tmp_path)
        command = ToolCommand(argv=("/bin/true", "x", "y", "z"), config=b"")

        with pytest.raises(ProcessCallError, match="not a tool module command"):
            launcher.open(command)


class TestCallText:
    def test_streams_and_exit_code(self, tmp_path: Path) -> None:
        launcher = _launcher(tmp_path)

        outcome = launcher.call_text("echo out; echo err >&2; exit 3", "")

        assert outcome.result.stdout == "out\n"
        assert "err" in outcome.result.stderr
        assert outcome.result.exit_code == 3
        assert not outcome.succeeded

    def test_stdin_reaches_the_command(self, tmp_path: Path) -> None:
        launcher = _launcher(tmp_path)

        outcome = launcher.call_text("cat", "ping")

        assert outcome.result.stdout == "ping"
        assert outcome.succeeded

    def test_command_runs_in_workdir(self, tmp_path: Path) -> None:
        launcher = _launcher(tmp_path)

        outcome = launcher.call_text("pwd", "")

        assert outcome.result.stdout.strip() == str(tmp_path)

    def test_timeout_kills_the_command(self, tmp_path: Path) -> None:
        launcher = _launcher(tmp_path, timeout_sec=0.5, kill_grace_sec=0.2)

        outcome = launcher.call_text("sleep 30", "")

        assert outcome.result.timed_out
        assert not outcome.succeeded

    def test_channel_overflow_kills_the_command(self, tmp_path: Path) -> None:
        launcher = _launcher(tmp_path, channel_limit_bytes=1024)

        with pytest.raises(ChannelOverflowError):
            launcher.call_text("yes overflow", "")


class TestStreamingCall:
    """Потоковый вызов: кадр в ответ на кадр, конверт после конца входа."""

    STREAM_ARGV = ("python3", "-m", "boba.stand.fake_toolmod", "fake_stream")

    def _command(self, prefix: str) -> ToolCommand:
        config = json.dumps({"cfg": CFG.revealed()}).encode("utf-8")
        return ToolCommand(argv=(*self.STREAM_ARGV, "--prefix", prefix), config=config)

    def test_frames_answer_frames_and_envelope_closes_call(
        self, tmp_path: Path
    ) -> None:
        launcher = _launcher(tmp_path)

        with launcher.open(self._command("re:")) as call:
            call.send(ToolFrame.of(FakeChunkHead(seq=1), b"one"))
            call.send(ToolFrame.of(FakeChunkHead(seq=2), b"two"))
            call.done_sending()

            bodies: list[bytes] = []
            kinds: list[str] = []
            for frame in call.frames():
                kinds.append(frame.kind)
                bodies.append(frame.body)

            outcome = call.result()

        assert kinds == ["chunk", "chunk", "done"]
        assert bodies[:2] == [b"re:one", b"re:two"]
        assert isinstance(outcome.reply, ReplyOk)
        assert "streamed 2" in outcome.reply.content

    def test_frames_arrive_before_input_is_closed(self, tmp_path: Path) -> None:
        launcher = _launcher(tmp_path)

        with launcher.open(self._command("x:")) as call:
            call.send(ToolFrame.of(FakeChunkHead(seq=1), b"early"))

            stream = call.frames()
            first = next(stream)

            assert first.body == b"x:early"

            call.done_sending()
            rest = list(stream)
            outcome = call.result()

        assert [frame.kind for frame in rest] == ["done"]
        assert isinstance(outcome.reply, ReplyOk)

    def test_large_body_survives_pipe_limit(self, tmp_path: Path) -> None:
        launcher = _launcher(tmp_path, channel_limit_bytes=8_000_000)
        payload = bytes(1_000_000)

        with launcher.open(self._command("")) as call:
            call.send(ToolFrame.of(FakeChunkHead(seq=1), payload))
            call.done_sending()

            frames = list(call.frames())
            outcome = call.result()

        assert frames[0].body == payload
        assert isinstance(outcome.reply, ReplyOk)

    def test_close_without_result_kills_the_call(self, tmp_path: Path) -> None:
        launcher = _launcher(tmp_path)

        call = launcher.open(self._command("y:"))
        call.send(ToolFrame.of(FakeChunkHead(seq=1), b"hang"))
        call.close()

        with pytest.raises(ToolStopped):
            call.result()
