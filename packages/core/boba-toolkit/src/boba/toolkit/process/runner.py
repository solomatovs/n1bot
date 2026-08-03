"""Subprocess-обёртка: argv + timeout + size-cap, потоки читаются через select."""

from __future__ import annotations

import os
import select
import subprocess
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from boba.toolkit.cancellation import TurnCancellation, current_cancellation
from boba.toolkit.workspace.options import ResourceLimits

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
    limits: ResourceLimits | None = None,
    cgroup_dir: str | None = None,
) -> RunResult:
    if not argv:
        raise ValueError("run_subprocess: argv must not be empty")
    if not cwd:
        raise ValueError("run_subprocess: cwd must be a non-empty string")

    preexec: Callable[[], None] | None = None
    if cgroup_dir is not None:
        procs_path = os.path.join(cgroup_dir, "cgroup.procs")

        def enter_cgroup() -> None:
            """Вход в cgroup до exec: всё дерево рождается уже внутри leaf'а."""
            fd = os.open(procs_path, os.O_WRONLY)
            os.write(fd, b"0")
            os.close(fd)

        preexec = enter_cgroup

    started = time.monotonic()
    proc = subprocess.Popen(  # noqa: S603 — argv приходит готовый из builder'а
        argv,
        shell=False,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0,
        close_fds=True,
        cwd=cwd,
        env=dict(env),
        preexec_fn=preexec,  # noqa: PLW1509 — только async-signal-safe вызовы
    )
    if limits is not None:
        limits.apply_to_process(proc.pid)
    cancellation = current_cancellation()
    with cancellation.abort_with(proc.kill):
        _feed_stdin(proc, stdin_data)
        out_bytes, err_bytes, trunc_out, trunc_err, timed_out = _pump(
            proc, timeout_sec, max_output_bytes, cancellation,
        )
    cancellation.raise_if_cancelled()
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
    if proc.stdin is None:
        raise ShellRunnerInvariantError(
            "_feed_stdin: proc.stdin expected (Popen started with PIPE)",
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
    cancellation: TurnCancellation,
) -> tuple[bytes, bytes, bool, bool, bool]:
    if proc.stdout is None or proc.stderr is None:
        raise ShellRunnerInvariantError(
            "_pump: proc.stdout and proc.stderr expected (Popen started with PIPE)"
        )

    deadline = time.monotonic() + timeout_sec
    fds = {proc.stdout.fileno(): "out", proc.stderr.fileno(): "err"}
    buffers = {"out": bytearray(), "err": bytearray()}
    truncated = {"out": False, "err": False}
    open_fds = set(fds.keys())
    timed_out = False

    while open_fds:
        if cancellation.cancelled:
            break
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
