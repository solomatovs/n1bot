"""Логи инструмента: лог-кадры tool_stderr уезжают в журнал приложения."""

from __future__ import annotations

import logging

import pytest

from boba.sandbox.profile import SandboxProfile
from boba.sandbox.runner import RelaySink
from boba.sandbox.workflow import WorkflowRunner
from boba.toolkit.channels import ChannelSink, LogFrame
from boba.toolkit.payload import PayloadLogging

LABEL = "doc:read_document"


class _RawLines(ChannelSink):
    """Приёмник сырых строк канала: копит текст построчно."""

    def __init__(self) -> None:
        self.lines: list[str] = []

    def feed(self, data: bytes) -> None:
        self.lines.append(data.decode("utf-8").rstrip("\n"))

    def close(self) -> None:
        pass


class _BrokenSink(ChannelSink):
    """Приёмник, падающий на каждой записи."""

    def feed(self, data: bytes) -> None:
        raise RuntimeError("sink is broken")

    def close(self) -> None:
        pass


def _frame(lvl: str, name: str, msg: str) -> bytes:
    return (LogFrame(lvl=lvl, name=name, msg=msg).encode() + "\n").encode("utf-8")


class TestRelaySink:
    """Читатель tool_stderr один — релей; исход операции он не трогает."""

    @staticmethod
    def _relay(raw: ChannelSink) -> RelaySink:
        return RelaySink(LABEL, raw)

    def test_raw_lines_reach_raw_sink(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Сырой stderr едет в raw-приёмник, лог-кадр — только в журнал приложения."""
        raw = _RawLines()
        relay = self._relay(raw)

        with caplog.at_level(logging.DEBUG):
            relay.feed(_frame("INFO", "boba.tool.pg", "запрос пошёл"))
            relay.feed(b"raw stderr line\n")

        assert raw.lines == ["raw stderr line"]

    def test_log_frame_keeps_level_and_logger(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        relay = self._relay(_RawLines())

        with caplog.at_level(logging.DEBUG):
            relay.feed(_frame("WARNING", "boba.tool.pg", "долгий запрос"))

        record = caplog.records[-1]
        assert record.levelno == logging.WARNING
        assert LABEL in record.getMessage()
        assert "boba.tool.pg" in record.getMessage()
        assert "долгий запрос" in record.getMessage()

    def test_multiline_message_survives_as_one_record(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Трейсбек в сообщении не должен рвать разбор на части."""
        relay = self._relay(_RawLines())

        with caplog.at_level(logging.DEBUG):
            relay.feed(_frame("ERROR", "t", "строка1\nстрока2"))

        assert len(caplog.records) == 1
        assert "строка1\nстрока2" in caplog.records[0].getMessage()

    def test_frames_split_across_reads(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Границы чтения из пайпа не совпадают с границами строк."""
        relay = self._relay(_RawLines())
        raw = _frame("INFO", "t", "привет")

        with caplog.at_level(logging.DEBUG):
            for start in range(0, len(raw), 5):
                relay.feed(raw[start : start + 5])

        assert len(caplog.records) == 1
        assert "привет" in caplog.records[0].getMessage()

    def test_raw_stderr_stays_out_of_the_app_log(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Сырой stderr живёт в канале инструмента, не в журнале приложения."""
        raw = _RawLines()
        relay = self._relay(raw)

        with caplog.at_level(logging.DEBUG):
            relay.feed(b"UserWarning: deprecated\n")

        assert not caplog.records
        assert raw.lines == ["UserWarning: deprecated"]

    def test_broken_frame_is_not_lost(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Битый кадр — аномалия payload'а: шумовой лог плюс raw-приёмник."""
        raw = _RawLines()
        relay = self._relay(raw)
        line = f"{LogFrame.MARKER}{{битый\n".encode()

        with caplog.at_level(logging.DEBUG):
            relay.feed(line)

        assert "битый" in caplog.records[-1].getMessage()
        assert raw.lines == [f"{LogFrame.MARKER}{{битый"]

    def test_tail_without_newline_is_flushed_on_close(self) -> None:
        raw = _RawLines()
        relay = self._relay(raw)

        relay.feed(b"last line without newline")
        assert raw.lines == []

        relay.close()
        assert raw.lines == ["last line without newline"]

    def test_empty_lines_are_skipped(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        raw = _RawLines()
        relay = self._relay(raw)

        with caplog.at_level(logging.DEBUG):
            relay.feed(b"\n   \n")

        assert not caplog.records
        assert raw.lines == []

    def test_raw_sink_failure_disables_relay_quietly(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Журнальная копия побочна: сбой приёмника не рвёт поток стадии."""
        relay = self._relay(_BrokenSink())

        with caplog.at_level(logging.DEBUG):
            relay.feed(b"first\n")
            relay.feed(b"second\n")
            relay.close()

        disabled = [r for r in caplog.records if "relay disabled" in r.getMessage()]
        assert len(disabled) == 1


_PROFILE_BASE: dict[str, object] = {
    "rootfs": "",
    "ro_binds": (),
    "rw_binds": (),
    "rw_images": (),
    "image_template": "",
    "launcher": {
        "mount_wait_sec": 10.0,
        "mount_poll_sec": 0.05,
        "shutdown_wait_sec": 5.0,
        "lock_wait_sec": 10.0,
        "copy_chunk_bytes": 1 << 20,
    },
    "tmpfs": (),
    "network": False,
    "env_set": {"PATH": "/usr/bin:/bin"},
    "timeout_sec": 30,
    "max_memory_bytes": 512 * 1024 * 1024,
    "max_cpu_sec": 30,
    "max_file_size_bytes": 64 * 1024 * 1024,
    "max_open_files": 1024,
    "max_processes": 256,
    "max_output_bytes": 256 * 1024,
    "cgroup_base": "",
    "oom_score_adj": 0,
    "cwd": "/tmp",  # noqa: S108
}


class TestLevelSource:
    """Уровень логов у payload'а один с приложением: своей ручки в конфиге нет."""

    @staticmethod
    def _env(level: int) -> dict[str, str]:
        app_logger = logging.getLogger(WorkflowRunner.APP_LOGGER)
        previous = app_logger.level
        app_logger.setLevel(level)
        try:
            profile = SandboxProfile.model_validate(_PROFILE_BASE)
            return WorkflowRunner._payload_env(profile)
        finally:
            app_logger.setLevel(previous)

    def test_level_comes_from_app_logger(self) -> None:
        env = self._env(logging.DEBUG)
        assert env[PayloadLogging.LEVEL_ENV] == "DEBUG"

    def test_level_follows_reconfiguration(self) -> None:
        env = self._env(logging.WARNING)
        assert env[PayloadLogging.LEVEL_ENV] == "WARNING"

    def test_profile_env_is_kept(self) -> None:
        env = self._env(logging.INFO)
        assert env["PATH"] == "/usr/bin:/bin"
