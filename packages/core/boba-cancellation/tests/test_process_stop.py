"""Остановка процесса по сигналу: настоящий процесс python под ProcessStop.

Процесс исполняет работу с finally и действием остановки; тест шлёт ему
сигнал и смотрит код выхода и следы, которые процесс оставил в файле.
"""

from __future__ import annotations

import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

JOB = """
import asyncio, sys
from pathlib import Path
from boba.cancellation import ProcessStop

trace = Path(sys.argv[1])
mode = sys.argv[2]


class Noted:
    def __call__(self, received):
        with trace.open("a") as out:
            out.write(f"action {received.name}\\n")


async def work():
    if mode == "fails":
        raise SystemExit("the work failed")

    if mode == "ends":
        return

    with trace.open("a") as out:
        out.write("started\\n")

    try:
        await asyncio.sleep(600)
    finally:
        with trace.open("a") as out:
            out.write("closed\\n")


async def main():
    stop = ProcessStop()
    stop.on_stop(Noted())
    await stop.run(work())


asyncio.run(main())
"""

EXIT_SEC = 10.0


def _job(trace: Path, mode: str) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        [sys.executable, "-c", JOB, str(trace), mode], stderr=subprocess.PIPE
    )


def _started(trace: Path) -> None:
    deadline = time.monotonic() + EXIT_SEC
    while not trace.exists():
        if time.monotonic() > deadline:
            raise AssertionError("the job never started its work")

        time.sleep(0.05)


@pytest.mark.parametrize("sent", [signal.SIGTERM, signal.SIGINT])
def test_signal_stops_the_work_and_the_process_exits_with_zero(
    tmp_path: Path, sent: signal.Signals
) -> None:
    trace = tmp_path / "trace"
    job = _job(trace, "sleeps")
    _started(trace)

    job.send_signal(sent)
    code = job.wait(timeout=EXIT_SEC)

    if code != 0:
        raise AssertionError(f"the stopped job exits with 0, got {code}")

    lines = trace.read_text().splitlines()
    if lines != ["started", f"action {sent.name}", "closed"]:
        raise AssertionError(f"the action runs, then the work closes: {lines}")


def test_second_signal_does_not_repeat_the_actions(tmp_path: Path) -> None:
    trace = tmp_path / "trace"
    job = _job(trace, "sleeps")
    _started(trace)

    job.send_signal(signal.SIGTERM)
    job.send_signal(signal.SIGTERM)
    code = job.wait(timeout=EXIT_SEC)

    actions = trace.read_text().count("action")
    if code != 0 or actions != 1:
        raise AssertionError(f"one stop for two signals: code {code}, {actions}")


def test_work_that_ends_by_itself_leaves_the_exit_code_alone(tmp_path: Path) -> None:
    ended = _job(tmp_path / "ended", "ends")
    failed = _job(tmp_path / "failed", "fails")

    if ended.wait(timeout=EXIT_SEC) != 0:
        raise AssertionError("the finished job exits with 0")

    code = failed.wait(timeout=EXIT_SEC)
    if code != 1:
        raise AssertionError(f"the failure of the work is the exit code, got {code}")

    stderr = failed.stderr
    if stderr is None or b"the work failed" not in stderr.read():
        raise AssertionError("the failure text of the work reaches stderr")
