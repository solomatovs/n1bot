"""Надёжность потокового вызова: отмены, глухие и мёртвые тела, битые кадры,
высвобождение дескрипторов. Тесты намеренно валят вызов и проверяют, что
лончер прибирает процесс и каналы, а причина доезжает до вызывающего.
"""

from __future__ import annotations

import gc
import json
import os
import signal
import threading
import time
from pathlib import Path

import pytest
from pydantic import SecretStr

from boba.cancellation import ToolStopped, run_cancellation
from boba.stand.fake_toolmod import (
    FakeChunkHead,
    FakeConfig,
    FakePidHead,
    fake_relay,
    fake_stream,
)
from boba.toolkit.chain import CallRelay, ChainCheck, ChainMismatchError, RelayStats
from boba.toolkit.entry import ToolArgv, ToolMain
from boba.toolkit.frames import FrameProtocolError, ToolFrame
from boba.toolkit.launcher import LauncherError
from boba.toolkit.ports import StreamSpec
from boba.toolkit.protocol import ToolCommand
from boba.toolrun.process import ProcessLauncherConfig, ProcessToolCaller

CFG = FakeConfig(token=SecretStr("t0ken"), limit=5)

MODULE = "boba.stand.fake_toolmod"

STREAM_TOOL = ToolMain.toolset(fake_stream)[0]
RELAY_TOOL = ToolMain.toolset(fake_relay)[0]


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


class TestForeignKind:
    def test_undeclared_kind_fails_the_call_loudly(self, tmp_path: Path) -> None:
        """Кадр с kind вне декларации порта: тело падает на границе, вызов
        кончается внятной ошибкой, а не молчаливым пропуском данных."""
        launcher = _launcher(tmp_path)

        with launcher.open(_command("fake_stream", "--prefix", "k:")) as call:
            call.send(ToolFrame.of(FakePidHead(pid=1), b"alien"))
            call.done_sending()

            list(call.frames())

            with pytest.raises(LauncherError, match="no envelope"):
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


class TestFramedChain:
    def test_framed_stream_flows_through_relay_frames(self, tmp_path: Path) -> None:
        """Кадровая цепочка через CallRelay.frames: выход fake_stream идёт во
        вход второго fake_stream, хост видит каждый кадр."""
        launcher = _launcher(tmp_path)

        spec = StreamSpec.of_schema(ToolArgv.schema_of(STREAM_TOOL))
        ChainCheck.ensure(spec, spec)

        with (
            launcher.open(_command("fake_stream", "--prefix", "x:")) as source,
            launcher.open(_command("fake_stream", "--prefix", "y:")) as sink,
        ):
            source.send(ToolFrame.of(FakeChunkHead(seq=1), b"one"))
            source.send(ToolFrame.of(FakeChunkHead(seq=2), b"two"))
            source.done_sending()

            stats = CallRelay.frames(source, sink)

            source_outcome = source.result()
            relayed = [frame.body for frame in sink.frames()]
            sink_outcome = sink.result()

        assert not stats.spliced
        assert stats.frames == 3
        assert stats.bytes == 10
        assert relayed[:2] == [b"y:x:one", b"y:x:two"]
        assert "streamed 2" in str(source_outcome.reply)
        assert "streamed 3" in str(sink_outcome.reply)

    def test_framed_source_is_refused_by_raw_sink(self) -> None:
        """Кадровый выход в сырой вход не собирается: рамки кадров попали бы
        в данные — стыковка отбивается до запуска."""
        source_spec = StreamSpec.of_schema(ToolArgv.schema_of(STREAM_TOOL))
        sink_spec = StreamSpec.of_schema(ToolArgv.schema_of(RELAY_TOOL))

        with pytest.raises(ChainMismatchError):
            ChainCheck.ensure(source_spec, sink_spec)


class _SpliceWorker:
    """Перекачка CallRelay.splice своим потоком: relay блокирует до конца
    потока и обязан идти параллельно закачке входа источника."""

    def __init__(self, source_fd: int, sink_fd: int) -> None:
        self._source_fd = source_fd
        self._sink_fd = sink_fd
        self._stats: list[RelayStats] = []
        self._worker = threading.Thread(target=self._relay, daemon=True)
        self._worker.start()

    def wait(self) -> RelayStats:
        self._worker.join(timeout=60)

        assert self._stats, "splice relay did not finish"
        return self._stats[0]

    def _relay(self) -> None:
        self._stats.append(CallRelay.splice(self._source_fd, self._sink_fd))


class TestSpliceChain:
    def test_kernel_splice_moves_raw_bytes_verbatim(self, tmp_path: Path) -> None:
        """Zero-copy сырая цепочка fake_relay -> fake_relay: голые байты без
        единого преобразования. Вход первого — send_bytes, канал между ними
        переливает ядро, выход второго читается с его tap-дескриптора и
        сверяется байт-в-байт."""
        launcher = _launcher(tmp_path)

        spec = StreamSpec.of_schema(ToolArgv.schema_of(RELAY_TOOL))
        ChainCheck.ensure(spec, spec)

        payload = b"\x5a" * (512 * 1024) + b"csv,rows\n" * 1000

        first = launcher.open_tap(_command("fake_relay"))
        second = launcher.open_tap(_command("fake_relay"))

        with first.call as source, second.call as sink:
            relay = _SpliceWorker(first.frames_fd, CallRelay.input_fd(sink))

            # сырой вход первого: голые байты в его stdin-дескриптор
            source_in = CallRelay.input_fd(source)
            os.write(source_in, payload)
            os.close(source_in)

            stats = relay.wait()

            collected = bytearray()
            while True:
                chunk = os.read(second.frames_fd, 1 << 20)
                if not chunk:
                    break

                collected.extend(chunk)

            os.close(second.frames_fd)

            source_outcome = source.result()
            sink_outcome = sink.result()

        assert stats.spliced
        assert stats.bytes == len(payload)
        assert bytes(collected) == payload
        assert f"relayed {len(payload)}" in str(source_outcome.reply)
        assert f"relayed {len(payload)}" in str(sink_outcome.reply)

    def test_tapped_call_frames_are_empty(self, tmp_path: Path) -> None:
        """Кадры tap-вызова отданы дескриптором: frames() пуст, конверт цел."""
        launcher = _launcher(tmp_path)

        tapped = launcher.open_tap(_command("fake_stream", "--prefix", "t:"))

        with tapped.call as source:
            relay = _SpliceWorker(tapped.frames_fd, os.open(os.devnull, os.O_WRONLY))

            source.done_sending()
            drained = relay.wait()

            assert list(source.frames()) == []
            outcome = source.result()

        assert drained.spliced is True
        assert "streamed 0" in str(outcome.reply)

    def test_dead_sink_breaks_the_chain_loudly(self, tmp_path: Path) -> None:
        """Смерть приёмника посреди перекачки: splice выходит, источник
        умирает по EPIPE (как в shell-конвейере), никто не виснет."""
        launcher = _launcher(tmp_path)

        tapped = launcher.open_tap(_command("fake_stream", "--prefix", "d:"))
        sink = launcher.open(_command("fake_hostage"))

        with tapped.call as source, sink:
            pid_frame = next(sink.frames())
            os.kill(pid_frame.header_as(FakePidHead).pid, signal.SIGKILL)

            started = time.monotonic()
            relay = _SpliceWorker(tapped.frames_fd, CallRelay.input_fd(sink))

            source.send(ToolFrame.of(FakeChunkHead(seq=1), b"\x00" * (2 << 20)))
            source.done_sending()

            relay.wait()

            assert time.monotonic() - started < 30

            with pytest.raises(LauncherError):
                source.result()

            with pytest.raises(LauncherError):
                sink.result()


class TestResources:
    def test_no_fd_leak_across_calls(self, tmp_path: Path) -> None:
        """Дескрипторы после серии вызовов — как до неё: успех, отказ, отмена."""
        launcher = _launcher(tmp_path, timeout_sec=15.0)

        def one_ok() -> None:
            with launcher.open(_command("fake_stream", "--prefix", "p:")) as call:
                call.send(ToolFrame.of(FakeChunkHead(seq=1), b"data"))
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

        # дескрипторы соседних тестов закрываются сборщиком: считать после него
        gc.collect()
        before = _open_fds()
        for _ in range(3):
            one_ok()
            one_closed()
            one_broken()

        gc.collect()
        assert _open_fds() == before
