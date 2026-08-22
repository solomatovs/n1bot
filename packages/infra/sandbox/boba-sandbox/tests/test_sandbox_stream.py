"""Тап каналов: окна наполняются по ходу процесса, обвязка едет своим каналом."""

from __future__ import annotations

import os
import shutil
from typing import Any

import pytest
from zygote_stand import SandboxStand, ZygoteStand

from boba.toolkit.channels import JournalChannel, ToolChannel, WrapChannel
from boba.toolkit.stream import (
    ChannelSinks,
    StreamSink,
    ToolChannelsTap,
    ToolStreamBuffer,
)

_PROFILE_OVERRIDES: dict[str, Any] = {
    "timeout_sec": 30,
    "process_memory_bytes": 512 * 1024 * 1024,
    "process_cpu_sec": 30,
}


@pytest.fixture(autouse=True)
def clean_tap() -> Any:
    yield
    ToolChannelsTap.set(None)


class Windows(ChannelSinks):
    """Журнал каналов вызова в памяти: окно на канал, заводится по обращению."""

    def __init__(self, window_bytes: int = 64 * 1024) -> None:
        self._window_bytes = window_bytes
        self._buffers: dict[str, ToolStreamBuffer] = {}
        self.wake_sizes: list[int] = []

    def sink_of(self, channel: JournalChannel) -> StreamSink:
        return self.buffer_of(channel)

    def buffer_of(self, channel: JournalChannel) -> ToolStreamBuffer:
        buffer = self._buffers.get(channel.value)
        if buffer is not None:
            return buffer

        def wake() -> None:
            self._on_wake(channel)

        buffer = ToolStreamBuffer(self._window_bytes, wake)
        self._buffers[channel.value] = buffer
        return buffer

    def text_of(self, channel: JournalChannel) -> str:
        return self.buffer_of(channel).snapshot().text

    def _on_wake(self, channel: JournalChannel) -> None:
        if channel is not ToolChannel.STDOUT:
            return

        self.wake_sizes.append(len(self.text_of(channel)))


@pytest.mark.skipif(shutil.which("bwrap") is None, reason="bwrap не установлен")
@pytest.mark.skipif(os.geteuid() == 0, reason="под root userns ведёт себя иначе")
class TestCallTextTap:
    """Текстовый запуск: stdout и stderr тела ложатся в свои каналы журнала."""

    def teardown_method(self) -> None:
        ZygoteStand.stop()

    @staticmethod
    def _caller(**profile_kw: Any) -> Any:
        profile = SandboxStand.profile(**{**_PROFILE_OVERRIDES, **profile_kw})
        return ZygoteStand.caller("bash", profile)

    def test_streams_go_to_their_own_channels_and_result_is_kept(self) -> None:
        windows = Windows()
        ToolChannelsTap.set(windows)

        outcome = self._caller().call_text("echo привет; echo беда >&2", stdin="")

        out = windows.text_of(ToolChannel.STDOUT)
        err = windows.text_of(ToolChannel.STDERR)
        if "привет" not in out:
            raise AssertionError('"привет" in out')
        if "беда" not in err:
            raise AssertionError('"беда" in err')
        if "беда" in out:
            raise AssertionError('"беда" not in out')
        # обвязка на каждом вызове отчитывается таймингом подготовки
        wrap = windows.text_of(WrapChannel.STDERR)
        if "setup" not in wrap:
            raise AssertionError(f"в канале обвязки нет тайминга подготовки: {wrap!r}")
        if "привет" in wrap:
            raise AssertionError("вывод команды не должен попадать в канал обвязки")
        if "привет" not in outcome.result.stdout:
            raise AssertionError('"привет" in outcome.result.stdout')
        if "беда" not in outcome.result.stderr:
            raise AssertionError('"беда" in outcome.result.stderr')

    def test_without_tap_nothing_changes(self) -> None:
        ToolChannelsTap.set(None)

        outcome = self._caller().call_text("echo одинокий", stdin="")

        if "одинокий" not in outcome.result.stdout:
            raise AssertionError('"одинокий" in outcome.result.stdout')

    def test_window_stays_bounded_on_huge_output(self) -> None:
        """Мегабайты вывода не оседают в окне: оно держит только хвост.

        Результат отдаётся целиком, а окно продолжает ехать до конца
        процесса — в нём последние строки.
        """
        window_bytes = 64 * 1024
        windows = Windows(window_bytes)
        ToolChannelsTap.set(windows)

        # ~1.6 МБ: 200000 строк по 8 байт
        outcome = self._caller().call_text("seq -w 1 200000", stdin="")

        window = windows.buffer_of(ToolChannel.STDOUT).snapshot()
        if len(window.text.encode()) > window_bytes:
            raise AssertionError("len(window.text.encode()) <= window_bytes")
        if window.dropped_bytes <= 1_000_000:
            raise AssertionError("window.dropped_bytes > 1_000_000")
        if "200000" not in window.text:
            raise AssertionError('"200000" in window.text')
        if "\n000002\n" in window.text:
            raise AssertionError('"\\n000002\\n" not in window.text')
        if not (outcome.result.stdout.startswith("000001\n")):
            raise AssertionError('outcome.result.stdout.startswith("000001\\n")')

    def test_window_fills_while_the_process_runs(self) -> None:
        """Пробуждения приходят по ходу процесса, а не одним махом в конце."""
        windows = Windows()
        ToolChannelsTap.set(windows)

        self._caller().call_text("echo старт; sleep 0.3; echo финиш", stdin="")

        sizes = windows.wake_sizes

        if len(sizes) < 2:
            raise AssertionError("len(sizes) >= 2")
        if sizes != sorted(sizes):
            raise AssertionError("sizes == sorted(sizes)")
        if sizes[0] >= sizes[-1]:
            raise AssertionError("sizes[0] < sizes[-1]")
