"""Логи инструмента: кадры из stderr уезжают в общий журнал приложения."""

from __future__ import annotations

import logging
import os

import pytest
from zygote_stand import ROOTFS_IMAGE

from boba.sandbox.runner import SandboxLogRelay, StderrTee
from boba.sandbox.zygote import ZygoteSpawner
from boba.toolkit.channels import JournalChannel, ToolChannel, WrapChannel
from boba.toolkit.images import LauncherMarker
from boba.toolkit.launcher import LaunchPayload
from boba.toolkit.payload import PayloadLogging
from boba.toolkit.stream import ChannelSinks, Chunk, StreamSink
from boba.toolkit.zygote import ZygoteArgs


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


class RecordingSink(StreamSink):
    """Приёмник одного канала в памяти: тест читает записанные строки."""

    def __init__(self) -> None:
        self.lines: list[str] = []

    def feed(self, data: Chunk) -> None:
        self.feed_text(bytes(data).decode("utf-8"))

    def feed_text(self, text: str) -> None:
        self.lines.append(text.rstrip("\n"))


class RecordingSinks(ChannelSinks):
    """Журнал каналов вызова в памяти: приёмник заводится по обращению."""

    def __init__(self) -> None:
        self._sinks: dict[str, RecordingSink] = {}

    def sink_of(self, channel: JournalChannel) -> StreamSink:
        sink = self._sinks.get(channel.value)
        if sink is None:
            sink = RecordingSink()
            self._sinks[channel.value] = sink

        return sink

    def lines_of(self, channel: JournalChannel) -> list[str]:
        sink = self._sinks.get(channel.value)
        if sink is None:
            return []

        return sink.lines


class TestRelay:
    """Читатель stderr один — релей; исход операции он не трогает."""

    @staticmethod
    def _tee(sinks: RecordingSinks | None = None) -> StderrTee:
        """Сырой stderr трактуется как вывод тела: так работает текстовый запуск."""
        if sinks is None:
            sinks = RecordingSinks()

        return StderrTee(sinks, ToolChannel.STDERR)

    @classmethod
    def _relay(cls) -> SandboxLogRelay:
        return SandboxLogRelay(LABEL, cls._tee())

    def test_tee_gets_human_lines(self, caplog: pytest.LogCaptureFixture) -> None:
        """В журнал вызова уходит текст без маркеров протокола."""
        sinks = RecordingSinks()
        relay = SandboxLogRelay(LABEL, self._tee(sinks))
        frame = LaunchPayload.encode_log("INFO", "boba.tool.pg", "запрос пошёл")
        with caplog.at_level(logging.DEBUG):
            relay.feed(f"{frame}\n".encode())
            relay.feed(b"raw stderr line\n")

        lines = sinks.lines_of(ToolChannel.STDERR)
        if lines != ["boba.tool.pg: запрос пошёл", "raw stderr line"]:
            raise AssertionError('lines == ["boba.tool.pg: запрос пошёл", "raw stderr…')

    def test_launcher_lines_go_to_the_wrap_channel(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Ход монтирования — канал обвязки: вывод инструмента им не пачкается."""
        sinks = RecordingSinks()
        relay = SandboxLogRelay(LABEL, self._tee(sinks))
        with caplog.at_level(logging.DEBUG):
            relay.feed(f"{LauncherMarker.LOG}image mounted\n".encode())
            relay.feed(b"tool says hi\n")

        if sinks.lines_of(WrapChannel.STDERR) != ["image mounted"]:
            raise AssertionError(
                'sinks.lines_of(WrapChannel.STDERR) == ["image mounted"]'
            )
        if sinks.lines_of(ToolChannel.STDERR) != ["tool says hi"]:
            raise AssertionError(
                'sinks.lines_of(ToolChannel.STDERR) == ["tool says hi"]'
            )

    def test_log_frame_keeps_level_and_logger(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        relay = self._relay()
        frame = LaunchPayload.encode_log("WARNING", "boba.tool.pg", "долгий запрос")
        with caplog.at_level(logging.DEBUG):
            relay.feed(f"{frame}\n".encode())

        record = caplog.records[-1]
        if record.levelno != logging.WARNING:
            raise AssertionError("record.levelno == logging.WARNING")
        if LABEL not in record.getMessage():
            raise AssertionError("LABEL in record.getMessage()")
        if "boba.tool.pg" not in record.getMessage():
            raise AssertionError('"boba.tool.pg" in record.getMessage()')
        if "долгий запрос" not in record.getMessage():
            raise AssertionError('"долгий запрос" in record.getMessage()')

    def test_multiline_message_survives_as_one_record(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Трейсбек в сообщении не должен рвать разбор на части."""
        relay = self._relay()
        frame = LaunchPayload.encode_log("ERROR", "t", "строка1\nстрока2")
        with caplog.at_level(logging.DEBUG):
            relay.feed(f"{frame}\n".encode())

        if len(caplog.records) != 1:
            raise AssertionError("len(caplog.records) == 1")
        if "строка1\nстрока2" not in caplog.records[0].getMessage():
            raise AssertionError(
                '"строка1\\nстрока2" in caplog.records[0].getMessage()'
            )

    def test_frames_split_across_reads(self, caplog: pytest.LogCaptureFixture) -> None:
        """Границы чтения из пайпа не совпадают с границами строк."""
        relay = self._relay()
        raw = f"{LaunchPayload.encode_log('INFO', 't', 'привет')}\n".encode()
        with caplog.at_level(logging.DEBUG):
            for start in range(0, len(raw), 5):
                relay.feed(raw[start : start + 5])

        if len(caplog.records) != 1:
            raise AssertionError("len(caplog.records) == 1")
        if "привет" not in caplog.records[0].getMessage():
            raise AssertionError('"привет" in caplog.records[0].getMessage()')

    def test_raw_stderr_stays_out_of_the_app_log(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Сырой stderr живёт в журнале вывода инструмента, не в журнале приложения."""
        sinks = RecordingSinks()
        relay = SandboxLogRelay(LABEL, self._tee(sinks))
        with caplog.at_level(logging.DEBUG):
            relay.feed(b"UserWarning: deprecated\n")

        if caplog.records:
            raise AssertionError("not caplog.records")
        if sinks.lines_of(ToolChannel.STDERR) != ["UserWarning: deprecated"]:
            raise AssertionError(
                'sinks.lines_of(ToolChannel.STDERR) == ["UserWarning: deprecated"]'
            )

    def test_broken_frame_is_not_lost(self, caplog: pytest.LogCaptureFixture) -> None:
        relay = self._relay()
        with caplog.at_level(logging.DEBUG):
            relay.feed(f"{LaunchPayload.LOG_MARKER}{{битый\n".encode())

        if "битый" not in caplog.records[-1].getMessage():
            raise AssertionError('"битый" in caplog.records[-1].getMessage()')

    def test_launcher_lines_stay_recognised(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        relay = self._relay()
        with caplog.at_level(logging.DEBUG):
            relay.feed(f"{LauncherMarker.LOG}image mounted\n".encode())

        record = caplog.records[-1]
        if record.levelno != logging.INFO:
            raise AssertionError("record.levelno == logging.INFO")
        if "image mounted" not in record.getMessage():
            raise AssertionError('"image mounted" in record.getMessage()')

    def test_tail_without_newline_is_flushed(self) -> None:
        sinks = RecordingSinks()
        relay = SandboxLogRelay(LABEL, self._tee(sinks))

        relay.feed(b"last line without newline")
        if sinks.lines_of(ToolChannel.STDERR) != []:
            raise AssertionError("sinks.lines_of(ToolChannel.STDERR) == []")

        relay.flush()
        if sinks.lines_of(ToolChannel.STDERR) != ["last line without newline"]:
            raise AssertionError(
                'sinks.lines_of(ToolChannel.STDERR) == ["last line without newline"]'
            )

    def test_empty_lines_are_skipped(self, caplog: pytest.LogCaptureFixture) -> None:
        relay = self._relay()
        with caplog.at_level(logging.DEBUG):
            relay.feed(b"\n   \n")

        if caplog.records:
            raise AssertionError("not caplog.records")


class TestStderrCleanup:
    """Отрелеенное не остаётся в stderr результата: там только объяснение сбоя."""

    def test_relayed_lines_are_recognised(self) -> None:
        if not (SandboxLogRelay.relayed(LaunchPayload.encode_log("INFO", "t", "m"))):
            raise AssertionError('SandboxLogRelay.relayed(LaunchPayload.encode_log("I…')
        if not (SandboxLogRelay.relayed(f"{LauncherMarker.LOG}mounted")):
            raise AssertionError('SandboxLogRelay.relayed(f"{LauncherMarker.LOG}mount…')

    def test_traceback_lines_are_kept(self) -> None:
        if SandboxLogRelay.relayed("Traceback (most recent call last):") is not False:
            raise AssertionError('SandboxLogRelay.relayed("Traceback (most recent cal…')
        if SandboxLogRelay.relayed("RuntimeError: boom") is not False:
            raise AssertionError('SandboxLogRelay.relayed("RuntimeError: boom") is Fa…')


_PROFILE_BASE: dict[str, object] = {
    "host": {
        "mounting": {
            "mount_wait_sec": 10.0,
            "mount_poll_sec": 0.05,
            "shutdown_wait_sec": 5.0,
            "lock_wait_sec": 10.0,
            "copy_chunk_bytes": 1 << 20,
        },
        "binaries": {"dirs": _bin_dirs()},
        "stderr_tail_bytes": 4096,
        "channel_limit_bytes": 67108864,
        "fail_tail_chars": 2000,
        "kill_grace_sec": 5,
        "cgroup_base": "",
    },
    "rootfs": str(ROOTFS_IMAGE),
    "mounts": {
        "tmp": "64M",
        "ro": (),
        "rw": (),
    },
    "isolation": {
        "reap_poll_sec": 0.05,
        "network": False,
        "env": {"PATH": "/usr/bin:/bin"},
    },
    "limits": {
        "timeout_sec": 30,
        "process_memory_bytes": 512 * 1024 * 1024,
        "process_cpu_sec": 30,
        "process_file_bytes": 64 * 1024 * 1024,
        "process_open_files": 1024,
        "process_oom_score_adj": 0,
    },
    "run": {
        "shell": "/bin/bash",
        "cwd": "/tmp",  # noqa: S108
    },
}


class TestLevelSource:
    """Уровень логов у payload'а один с приложением: своей ручки в конфиге нет."""

    @staticmethod
    def _level(level: int) -> str:
        app_logger = logging.getLogger(ZygoteSpawner.APP_LOGGER)
        previous = app_logger.level
        app_logger.setLevel(level)
        try:
            return ZygoteSpawner.log_level()
        finally:
            app_logger.setLevel(previous)

    def test_level_comes_from_app_logger(self) -> None:
        if self._level(logging.DEBUG) != "DEBUG":
            raise AssertionError('self._level(logging.DEBUG) == "DEBUG"')

    def test_level_follows_reconfiguration(self) -> None:
        if self._level(logging.WARNING) != "WARNING":
            raise AssertionError('self._level(logging.WARNING) == "WARNING"')

    def test_level_travels_in_zygote_arguments(self) -> None:
        args = ZygoteArgs(
            socket_fd=3,
            reap_poll_sec=0.05,
            log_level=self._level(logging.WARNING),
        )
        parsed = ZygoteArgs.parse(args.render())
        if parsed.log_level != "WARNING":
            raise AssertionError('parsed.log_level == "WARNING"')

    def test_body_takes_level_from_zygote(self) -> None:
        previous = PayloadLogging._adopted
        PayloadLogging._adopted = logging.WARNING
        try:
            if PayloadLogging.level() != logging.WARNING:
                raise AssertionError("PayloadLogging.level() == logging.WARNING")
        finally:
            PayloadLogging._adopted = previous
