"""Логи инструмента: кадры из stderr уезжают в общий журнал приложения."""

from __future__ import annotations

import logging
import os

import pytest

from boba.sandbox.profile import SandboxProfile
from boba.sandbox.runner import SandboxLogRelay, SandboxRunner
from boba.toolkit.launcher import LaunchPayload
from boba.toolkit.payload import PayloadLogging
from boba.workspace.launcher import LauncherMarker


def _bin_dirs() -> list[str]:
    """В тестах каталоги берутся из PATH; в проде их задаёт конфиг."""
    dirs: list[str] = []

    for entry in os.environ.get("PATH", "").split(os.pathsep):
        if not entry.startswith("/"):
            continue

        dirs.append(entry)

    return dirs


LABEL = "doc:read_document"


@pytest.fixture(autouse=True)
def chainlit_context() -> None:
    "релей не зависит от сессии chainlit"


class TestRelay:
    """Читатель stderr один — релей; исход операции он не трогает."""

    @staticmethod
    def _drop(line: str) -> None:
        """Окно живого вывода в этих тестах не участвует."""

    @classmethod
    def _relay(cls) -> SandboxLogRelay:
        return SandboxLogRelay(LABEL, cls._drop)

    def test_tee_gets_human_lines(self, caplog: pytest.LogCaptureFixture) -> None:
        """В окно живого вывода уходит текст без маркеров протокола."""
        lines: list[str] = []
        relay = SandboxLogRelay(LABEL, lines.append)
        frame = LaunchPayload.encode_log("INFO", "boba.tool.pg", "запрос пошёл")
        with caplog.at_level(logging.DEBUG):
            relay.feed(f"{frame}\n".encode())
            relay.feed(b"raw stderr line\n")

        assert lines == ["boba.tool.pg: запрос пошёл", "raw stderr line"]

    def test_log_frame_keeps_level_and_logger(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        relay = self._relay()
        frame = LaunchPayload.encode_log("WARNING", "boba.tool.pg", "долгий запрос")
        with caplog.at_level(logging.DEBUG):
            relay.feed(f"{frame}\n".encode())

        record = caplog.records[-1]
        assert record.levelno == logging.WARNING
        assert LABEL in record.getMessage()
        assert "boba.tool.pg" in record.getMessage()
        assert "долгий запрос" in record.getMessage()

    def test_multiline_message_survives_as_one_record(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Трейсбек в сообщении не должен рвать разбор на части."""
        relay = self._relay()
        frame = LaunchPayload.encode_log("ERROR", "t", "строка1\nстрока2")
        with caplog.at_level(logging.DEBUG):
            relay.feed(f"{frame}\n".encode())

        assert len(caplog.records) == 1
        assert "строка1\nстрока2" in caplog.records[0].getMessage()

    def test_frames_split_across_reads(self, caplog: pytest.LogCaptureFixture) -> None:
        """Границы чтения из пайпа не совпадают с границами строк."""
        relay = self._relay()
        raw = f"{LaunchPayload.encode_log('INFO', 't', 'привет')}\n".encode()
        with caplog.at_level(logging.DEBUG):
            for start in range(0, len(raw), 5):
                relay.feed(raw[start : start + 5])

        assert len(caplog.records) == 1
        assert "привет" in caplog.records[0].getMessage()

    def test_raw_stderr_stays_out_of_the_app_log(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Сырой stderr живёт в журнале вывода инструмента, не в журнале приложения."""
        lines: list[str] = []
        relay = SandboxLogRelay(LABEL, lines.append)
        with caplog.at_level(logging.DEBUG):
            relay.feed(b"UserWarning: deprecated\n")

        assert not caplog.records
        assert lines == ["UserWarning: deprecated"]

    def test_broken_frame_is_not_lost(self, caplog: pytest.LogCaptureFixture) -> None:
        relay = self._relay()
        with caplog.at_level(logging.DEBUG):
            relay.feed(f"{LaunchPayload.LOG_MARKER}{{битый\n".encode())

        assert "битый" in caplog.records[-1].getMessage()

    def test_launcher_lines_stay_recognised(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        relay = self._relay()
        with caplog.at_level(logging.DEBUG):
            relay.feed(f"{LauncherMarker.LOG}image mounted\n".encode())

        record = caplog.records[-1]
        assert record.levelno == logging.INFO
        assert "image mounted" in record.getMessage()

    def test_tail_without_newline_is_flushed(self) -> None:
        lines: list[str] = []
        relay = SandboxLogRelay(LABEL, lines.append)

        relay.feed(b"last line without newline")
        assert lines == []

        relay.flush()
        assert lines == ["last line without newline"]

    def test_empty_lines_are_skipped(self, caplog: pytest.LogCaptureFixture) -> None:
        relay = self._relay()
        with caplog.at_level(logging.DEBUG):
            relay.feed(b"\n   \n")

        assert not caplog.records


class TestStderrCleanup:
    """Отрелеенное не остаётся в stderr результата: там только объяснение сбоя."""

    def test_relayed_lines_are_recognised(self) -> None:
        assert SandboxLogRelay.relayed(LaunchPayload.encode_log("INFO", "t", "m"))
        assert SandboxLogRelay.relayed(f"{LauncherMarker.LOG}mounted")

    def test_traceback_lines_are_kept(self) -> None:
        assert SandboxLogRelay.relayed("Traceback (most recent call last):") is False
        assert SandboxLogRelay.relayed("RuntimeError: boom") is False


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
    "binaries": {"dirs": _bin_dirs()},
    "tmpfs": (),
    "network": False,
    "env_set": {"PATH": "/usr/bin:/bin"},
    "timeout_sec": 30,
    "max_memory_bytes": 512 * 1024 * 1024,
    "max_cpu_sec": 30,
    "max_file_size_bytes": 64 * 1024 * 1024,
    "max_open_files": 1024,
    "max_processes": 256,
    "cgroup_base": "",
    "oom_score_adj": 0,
    "cwd": "/tmp",  # noqa: S108
}


class TestLevelSource:
    """Уровень логов у payload'а один с приложением: своей ручки в конфиге нет."""

    @staticmethod
    def _env(level: int) -> dict[str, str]:
        app_logger = logging.getLogger(SandboxRunner.APP_LOGGER)
        previous = app_logger.level
        app_logger.setLevel(level)
        try:
            profile = SandboxProfile.model_validate(_PROFILE_BASE)
            return SandboxRunner._env_of(profile)
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
