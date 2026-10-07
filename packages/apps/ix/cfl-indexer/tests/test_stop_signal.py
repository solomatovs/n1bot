"""Остановка индексатора сигналом: процессы спейсов завершаются, процесс
выходит сам.

Настоящий процесс python повторяет форму работы индексатора: пул процессов,
итогов которого ждёт поток рядом с циклом событий. Действие остановки —
StoppedSpaces индексатора.
"""

from __future__ import annotations

import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

JOB = """
import asyncio, multiprocessing, sys, time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

from boba.cancellation import ProcessStop
from boba.cfl_indexer.worker import StoppedSpaces


def space(trace):
    Path(trace).write_text("indexing")
    time.sleep(600)


def spaces(trace):
    context = multiprocessing.get_context("spawn")
    with ProcessPoolExecutor(max_workers=1, mp_context=context) as pool:
        pool.submit(space, trace).result()


async def work():
    await asyncio.to_thread(spaces, sys.argv[1])


async def main():
    stop = ProcessStop()
    stop.on_stop(StoppedSpaces())
    await stop.run(work())


if __name__ == "__main__":
    asyncio.run(main())
"""

EXIT_SEC = 15.0


async def test_sigterm_ends_the_space_processes_and_the_indexer(
    tmp_path: Path,
) -> None:
    script = tmp_path / "job.py"
    script.write_text(JOB)
    trace = tmp_path / "trace"
    job = subprocess.Popen([sys.executable, str(script), str(trace)])
    deadline = time.monotonic() + EXIT_SEC
    while not trace.exists():
        if time.monotonic() > deadline:
            job.kill()
            raise AssertionError("the space process never started")

        time.sleep(0.05)

    job.send_signal(signal.SIGTERM)
    try:
        code = job.wait(timeout=EXIT_SEC)
    except subprocess.TimeoutExpired as exc:
        job.kill()
        msg = f"the indexer did not exit in {EXIT_SEC}s with a running space"
        raise AssertionError(msg) from exc

    if code != 0:
        raise AssertionError(f"the stopped indexer exits with 0, got {code}")

    left = subprocess.run(
        ["/usr/bin/pgrep", "-f", str(script)], capture_output=True, check=False
    )
    if left.returncode == 0:
        raise AssertionError(f"no space process outlives the indexer: {left.stdout!r}")
