"""Subprocess-обёртка: argv + timeout + size-cap, потоки читаются через select."""

from __future__ import annotations

import os
import select
import subprocess
import time
from collections.abc import Mapping
from dataclasses import dataclass

__all__ = ["RunResult", "ShellRunnerInvariantError", "run_subprocess"]


class ShellRunnerInvariantError(Exception):
    """Нарушен инвариант runner'а (в отличие от assert переживает -O)."""


@dataclass(frozen=True)
class RunResult:
    """Результат запуска: код возврата, потоки, факт обрезки, таймаут."""

    exit_code: int
    stdout: str
    stderr: str
    truncated_stdout: bool
    truncated_stderr: bool
    duration_ms: int
    timed_out: bool


def run_subprocess(  # noqa: PLR0913 — параметры процесса, независимы
    argv: list[str],
    *,
    stdin_data: bytes,
    timeout_sec: int,
    max_output_bytes: int,
    cwd: str,
    env: Mapping[str, str],
) -> RunResult:
    """Запустить argv и собрать stdout/stderr.

    RunResult возвращается и на таймаут (exit_code=-9); non-zero exit —
    валидный результат, исключения не бросаются.
    """
    if not argv:
        msg = "run_subprocess: argv не может быть пустым"
        raise ValueError(msg)
    if not cwd:
        msg = "run_subprocess: cwd должен быть непустой строкой"
        raise ValueError(msg)

    started = time.monotonic()
    proc = subprocess.Popen(  # noqa: S603 — argv приходит готовый из builder'а
        argv,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0,
        close_fds=True,
        cwd=cwd,
        env=dict(env),
    )
    _feed_stdin(proc, stdin_data)
    out_bytes, err_bytes, trunc_out, trunc_err, timed_out = _pump(
        proc, timeout_sec, max_output_bytes,
    )
    duration_ms = int((time.monotonic() - started) * 1000)
    exit_code = proc.returncode if proc.returncode is not None else -9
    return RunResult(
        exit_code=exit_code,
        stdout=out_bytes.decode("utf-8", errors="replace"),
        stderr=err_bytes.decode("utf-8", errors="replace"),
        truncated_stdout=trunc_out,
        truncated_stderr=trunc_err,
        duration_ms=duration_ms,
        timed_out=timed_out,
    )


def _feed_stdin(proc: subprocess.Popen[bytes], data: bytes) -> None:
    """Записать data в stdin и закрыть пайп; b"" = сразу EOF."""
    if proc.stdin is None:
        raise ShellRunnerInvariantError(
            "_feed_stdin: ожидался proc.stdin (Popen запущен с PIPE)",
        )
    try:
        if data:
            proc.stdin.write(data)
    except BrokenPipeError:
        pass
    finally:
        proc.stdin.close()


def _pump(
    proc: subprocess.Popen[bytes],
    timeout_sec: int,
    max_output_bytes: int,
) -> tuple[bytes, bytes, bool, bool, bool]:
    """Чтение stdout/stderr с лимитами -> (out, err, trunc_out, trunc_err, timeout)."""
    if proc.stdout is None or proc.stderr is None:
        raise ShellRunnerInvariantError(
            "_pump: ожидались proc.stdout и proc.stderr (Popen запущен с PIPE)"
        )

    deadline = time.monotonic() + timeout_sec
    fds = {proc.stdout.fileno(): "out", proc.stderr.fileno(): "err"}
    buffers = {"out": bytearray(), "err": bytearray()}
    truncated = {"out": False, "err": False}
    open_fds = set(fds.keys())
    timed_out = False

    while open_fds:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            timed_out = True
            break
        ready, _, _ = select.select(list(open_fds), [], [], min(remaining, 1.0))
        if not ready:
            continue
        for fd in ready:
            tag = fds[fd]
            if not _read_chunk(fd, buffers[tag], truncated, tag, max_output_bytes):
                open_fds.discard(fd)

    if timed_out:
        proc.kill()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()

    proc.stdout.close()
    proc.stderr.close()
    return (
        bytes(buffers["out"]),
        bytes(buffers["err"]),
        truncated["out"],
        truncated["err"],
        timed_out,
    )


def _read_chunk(
    fd: int,
    buf: bytearray,
    truncated: dict[str, bool],
    tag: str,
    max_output_bytes: int,
) -> bool:
    """Прочитать порцию из fd, обрезать по лимиту. Возврат: True если fd жив."""
    chunk = os.read(fd, 65536)
    if not chunk:
        return False
    if truncated[tag]:
        return True
    room = max_output_bytes - len(buf)
    if len(chunk) <= room:
        buf.extend(chunk)
    else:
        if room > 0:
            buf.extend(chunk[:room])
        truncated[tag] = True
    return True
