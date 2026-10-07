"""Отчёты тела инструмента о ходе работы: канал, частота и счётчик насоса.

Тело пишет отчёты в свой пайп; тест читает другой его конец той же
моделью, что и хост.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest

from boba.toolkit.ports import ProgressReport, StageProgress, ToolProgress
from boba.toolkit.transfer import CommandJournal, CommandKind, TransferProgress


class ProgressPipe:
    """Пайп отчётов: тело пишет, тест читает всё, что дошло."""

    def __init__(self) -> None:
        self._read, self.write = os.pipe()
        os.set_blocking(self._read, False)

    def reports(self) -> list[ProgressReport]:
        try:
            raw = os.read(self._read, 1 << 20)
        except BlockingIOError:
            return []

        found: list[ProgressReport] = []
        for line in raw.decode("utf-8").splitlines():
            found.append(ProgressReport.model_validate_json(line))

        return found

    def close(self) -> None:
        os.close(self._read)
        os.close(self.write)


@pytest.fixture
def pipe() -> Iterator[ProgressPipe]:
    opened = ProgressPipe()
    yield opened
    opened.close()


@pytest.fixture
def unthrottled(monkeypatch: pytest.MonkeyPatch) -> None:
    """Каждый отчёт уходит сразу: тест проверяет содержание, а не частоту."""
    monkeypatch.setattr(ToolProgress, "INTERVAL_SEC", 0.0)
    monkeypatch.setattr(TransferProgress, "INTERVAL_SEC", 0.0)


class TestToolProgress:
    def test_report_travels_as_a_line_of_the_channel(
        self, pipe: ProgressPipe, unthrottled: None
    ) -> None:
        progress = ToolProgress(pipe.write)

        progress.report(3, 10, "three of ten")

        expected = [ProgressReport(done=3, total=10, message="three of ten")]
        if pipe.reports() != expected:
            raise AssertionError("отчёт доехал как записан")

    def test_report_that_does_not_grow_is_not_sent(
        self, pipe: ProgressPipe, unthrottled: None
    ) -> None:
        progress = ToolProgress(pipe.write)

        progress.report(5, None, "five")
        progress.report(5, None, "five again")
        progress.report(4, None, "four")
        progress.report(6, None, "six")

        done = [report.done for report in pipe.reports()]
        if done != [5, 6]:
            raise AssertionError(f"прогресс только растёт: {done}")

    def test_frequent_reports_are_held_and_the_last_one_leaves_on_close(
        self, pipe: ProgressPipe
    ) -> None:
        progress = ToolProgress(pipe.write)

        for step in range(1, 51):
            progress.report(step, 50, f"step {step}")

        early = pipe.reports()
        if len(early) != 1 or early[0].done != 1:
            raise AssertionError(f"частые отчёты придержаны: {early}")

        progress.close()

        late = pipe.reports()
        if late != [ProgressReport(done=50, total=50, message="step 50")]:
            raise AssertionError(f"последний отчёт дослан: {late}")

    def test_detached_progress_writes_nothing(self) -> None:
        """Запуск человеком: канала отчётов нет, тело работает как обычно."""
        progress = ToolProgress(-1)

        progress.report(1, None, "nobody listens")
        progress.close()

    def test_long_message_is_clipped_to_one_atomic_line(
        self, pipe: ProgressPipe, unthrottled: None
    ) -> None:
        progress = ToolProgress(pipe.write)

        progress.report(1, None, "x" * 100_000)

        sent = pipe.reports()
        if len(sent) != 1 or len(sent[0].message) != ToolProgress.MESSAGE_CHARS:
            raise AssertionError("длинный текст обрезан до одной строки канала")


class TestStageProgress:
    def test_stage_before_a_long_wait_leaves_at_once(self, pipe: ProgressPipe) -> None:
        """Этап перед ожиданием сервера не придерживается частотой: иначе
        пользователь видел бы прошлый этап, пока тело ждёт."""
        stages = StageProgress(ToolProgress(pipe.write), None)

        stages.next("connecting to the server")
        stages.next("connected")
        stages.next("sending the query and waiting for the answer")

        seen = [report.message for report in pipe.reports()]
        if seen[-1] != "sending the query and waiting for the answer":
            raise AssertionError(f"the last stage is on the wire: {seen}")
        if len(seen) != 3:
            raise AssertionError(f"no stage is held back: {seen}")

    def test_counter_inside_a_stage_is_held_by_the_rate(
        self, pipe: ProgressPipe
    ) -> None:
        stages = StageProgress(ToolProgress(pipe.write), None)

        stages.next("reading rows")
        for row in range(1, 101):
            stages.counted(f"{row} rows so far")
        stages.next("read 100 rows")

        seen = [report.message for report in pipe.reports()]
        if seen != ["reading rows", "read 100 rows"]:
            raise AssertionError(f"counters do not flood the channel: {seen}")

    def test_every_stage_is_the_next_report(
        self, pipe: ProgressPipe, unthrottled: None
    ) -> None:
        stages = StageProgress(ToolProgress(pipe.write), 3)

        stages.next("connecting")
        stages.next("executing")
        stages.next("fetching")

        seen = [
            (report.done, report.total, report.message) for report in pipe.reports()
        ]
        if seen != [(1, 3, "connecting"), (2, 3, "executing"), (3, 3, "fetching")]:
            raise AssertionError(f"стадии идут по порядку: {seen}")


class TestTransferProgress:
    def test_commands_and_volume_are_told_together(
        self, pipe: ProgressPipe, unthrottled: None
    ) -> None:
        meter = TransferProgress(ToolProgress(pipe.write), "postgres")
        journal = CommandJournal("pg_stream_in", meter)

        with journal.command(
            "copy t from stdin\nwith (format binary)", CommandKind.ACTION
        ) as running:
            meter.moved(1 << 20)
            meter.moved(1 << 20)
            running.status = "COPY 1200"

        messages = [report.message for report in pipe.reports()]
        if len(messages) != 4:
            raise AssertionError(f"команда, объём и итог: {messages}")

        command = "copy t from stdin with (format binary)"
        sent = f"running on postgres, waiting for the server: {command}"
        if messages[0] != sent:
            raise AssertionError(f"команда названа целиком: {messages[0]}")

        volumes = ("moving data — 1.0 MiB so far", "moving data — 2.0 MiB so far")
        for message, volume in zip(messages[1:3], volumes, strict=True):
            if not message.startswith(volume) or not message.endswith(f": {command}"):
                raise AssertionError(f"объём растёт, команда названа: {message}")
            if " MiB/s" not in message:
                raise AssertionError(f"скорость потока названа: {message}")

        done = messages[3]
        if not done.startswith("postgres answered in ") or ": COPY 1200 — " not in done:
            raise AssertionError(f"итог команды со временем и объёмом: {done}")
        if not done.endswith(f": {command}"):
            raise AssertionError(f"итог называет команду: {done}")

    def test_frequent_frames_do_not_flood_the_channel(self, pipe: ProgressPipe) -> None:
        meter = TransferProgress(ToolProgress(pipe.write), "postgres")
        meter.command("copy t to stdout")

        for _ in range(10_000):
            meter.moved(65536)

        if len(pipe.reports()) > 2:
            raise AssertionError("поток кадров не заливает канал отчётами")
