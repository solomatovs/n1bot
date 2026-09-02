"""Надёжность потокового вызова: отмены, глухие и мёртвые тела, битые кадры,
высвобождение дескрипторов. Тесты намеренно валят вызов и проверяют, что
лончер прибирает процесс и каналы, а причина доезжает до вызывающего.
"""

from __future__ import annotations

import json
import os
import signal
import time
from pathlib import Path

import pytest
from pydantic import SecretStr

from boba.cancellation import ToolStopped, run_cancellation
from boba.stand.fake_toolmod import FakeConfig, FakePidHead
from boba.toolkit.frames import FrameProtocolError, ToolFrame
from boba.toolkit.launcher import LauncherError
from boba.toolkit.protocol import ToolCommand
from boba.toolrun.process import ProcessLauncherConfig, ProcessToolCaller

CFG = FakeConfig(token=SecretStr("t0ken"), limit=5)

MODULE = "boba.stand.fake_toolmod"


def _launcher(workdir: Path, **overrides: object) -> ProcessToolCaller:
    values: dict[str, object] = {
        "provider": "process",
        "workdir": str(workdir),
        "shell": "/bin/bash",
        "timeout_sec": 60.0,
        "channel_limit_bytes": 1_000_000,
        "stderr_tail_bytes": 4096,
        "kill_grace_sec": 0.5,
    }
    values.update(overrides)

    return ProcessToolCaller("fake", ProcessLauncherConfig.model_validate(values))


def _command(tool_name: str, *flags: str) -> ToolCommand:
    config = json.dumps({"cfg": CFG.revealed()}).encode("utf-8")
    argv = ("python3", "-m", MODULE, tool_name, *flags)

    return ToolCommand(argv=argv, config=config)


def _open_fds() -> int:
    return len(os.listdir("/proc/self/fd"))


class TestCancellation:
    def test_cancel_right_after_open_is_not_lost(self, tmp_path: Path) -> None:
        """Гонка отмены: cancel сразу после open обязан убить вызов."""
        launcher = _launcher(tmp_path)

        with run_cancellation() as cancellation:
            call = launcher.open(_command("fake_hostage"))
            cancellation.cancel()

            started = time.monotonic()
            with pytest.raises(ToolStopped):
                call.result()

        assert time.monotonic() - started < 10

    def test_open_on_cancelled_run_raises_and_leaks_nothing(
        self, tmp_path: Path
    ) -> None:
        """Уже отменённый ход: open падает сразу и прибирает процесс с каналами."""
        launcher = _launcher(tmp_path)
        before = _open_fds()

        with run_cancellation() as cancellation:
            cancellation.cancel()

            with pytest.raises(ToolStopped):
                launcher.open(_command("fake_hostage"))

        assert _open_fds() == before


class TestDeafBody:
    def test_send_unblocks_when_timeout_kills_the_body(self, tmp_path: Path) -> None:
        """Тело не читает вход: send стоит на полном пайпе, пока таймаут не
        добьёт тело; после смерти вызов объясняется итогом, а не зависает."""
        launcher = _launcher(tmp_path, timeout_sec=2.0)

        with launcher.open(_command("fake_deaf", "--sleep-sec", "30")) as call:
            payload = ToolFrame.of(FakePidHead(pid=0), b"\x00" * (4 * 1024 * 1024))

            started = time.monotonic()
            call.send(payload)
            blocked_for = time.monotonic() - started

            # запись обязана была встать до срабатывания таймаута вызова
            assert blocked_for > 0.5

            with pytest.raises(LauncherError, match="no envelope"):
                call.result()

        assert blocked_for < 30


class TestDeadBody:
    def test_kill_mid_stream_reports_no_envelope(self, tmp_path: Path) -> None:
        """SIGKILL тела посреди стрима: кадры до смерти доходят, итог — ошибка."""
        launcher = _launcher(tmp_path)

        with launcher.open(_command("fake_hostage")) as call:
            stream = call.frames()
            first = next(stream)
            pid = first.header_as(FakePidHead).pid

            os.kill(pid, signal.SIGKILL)

            rest = list(stream)

            with pytest.raises(LauncherError, match="no envelope"):
                call.result()

        assert rest == []

    def test_send_after_body_death_does_not_hang(self, tmp_path: Path) -> None:
        """Запись в мёртвое тело: первая молчит, следующая — ошибка входа."""
        launcher = _launcher(tmp_path)

        with launcher.open(_command("fake_hostage")) as call:
            stream = call.frames()
            first = next(stream)
            pid = first.header_as(FakePidHead).pid

            os.kill(pid, signal.SIGKILL)
            list(stream)

            # пайп рвётся не позже второй записи; обе обязаны вернуться сразу
            started = time.monotonic()
            try:
                call.send(ToolFrame.of(FakePidHead(pid=0), b"one"))
                call.send(ToolFrame.of(FakePidHead(pid=0), b"two"))
            except LauncherError:
                pass

            assert time.monotonic() - started < 5

            with pytest.raises(LauncherError):
                call.result()


class TestBrokenFrames:
    def test_garbage_frames_raise_at_reader_but_result_survives(
        self, tmp_path: Path
    ) -> None:
        """Мусор в канале кадров: читатель видит обрыв протокола, конверт цел."""
        launcher = _launcher(tmp_path)

        with launcher.open(_command("fake_garbage")) as call:
            call.done_sending()

            with pytest.raises(FrameProtocolError):
                list(call.frames())

            outcome = call.result()

        assert "garbage sent" in str(outcome.reply)


class TestSingleReader:
    def test_second_frames_reader_is_refused(self, tmp_path: Path) -> None:
        launcher = _launcher(tmp_path)

        with launcher.open(_command("fake_stream", "--prefix", "x:")) as call:
            call.frames()

            with pytest.raises(LauncherError, match="already have a reader"):
                call.frames()

            call.done_sending()
            call.result()


class TestResources:
    def test_no_fd_leak_across_calls(self, tmp_path: Path) -> None:
        """Дескрипторы после серии вызовов — как до неё: успех, отказ, отмена."""
        launcher = _launcher(tmp_path, timeout_sec=15.0)

        def one_ok() -> None:
            with launcher.open(_command("fake_stream", "--prefix", "p:")) as call:
                call.send(ToolFrame.of(FakePidHead(pid=0), b"data"))
                call.done_sending()
                list(call.frames())
                call.result()

        def one_closed() -> None:
            call = launcher.open(_command("fake_hostage"))
            call.close()

            with pytest.raises(ToolStopped):
                call.result()

        def one_broken() -> None:
            with launcher.open(_command("fake_garbage")) as call:
                call.done_sending()

                with pytest.raises(FrameProtocolError):
                    list(call.frames())

                call.result()

        one_ok()

        before = _open_fds()
        for _ in range(3):
            one_ok()
            one_closed()
            one_broken()

        assert _open_fds() == before
