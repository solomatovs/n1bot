"""Subprocess-обёртка: argv + timeout + size-cap, потоки читаются через select."""

from __future__ import annotations

import os
import select
import subprocess
import time
from collections.abc import Callable, Mapping

from boba.cancellation import TurnCancellation, current_cancellation
from boba.toolkit.launcher import RunResult
from boba.workspace.launcher import ResourceLimits

__all__ = ["RunResult", "ShellRunnerInvariantError", "run_subprocess"]


class ShellRunnerInvariantError(Exception):
    """Нарушен инвариант runner'а (в отличие от assert переживает -O)."""


def run_subprocess(  # noqa: PLR0913
    argv: list[str],
    *,
    stdin_data: bytes,
    timeout_sec: int,
    max_output_bytes: int,
    cwd: str,
    env: Mapping[str, str],
    stdout_sink: Callable[[bytes], None] | None,
    keep_stdout: bool,
    stderr_sink: Callable[[bytes], None] | None = None,
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
    proc = subprocess.Popen(  # noqa: S603
        argv,
        shell=False,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0,
        close_fds=True,
        cwd=cwd,
        env=dict(env),
        preexec_fn=preexec,  # noqa: PLW1509
    )
    if limits is not None:
        limits.apply_to_process(proc.pid)
    cancellation = current_cancellation()
    with cancellation.abort_with(proc.kill):
        _feed_stdin(proc, stdin_data)
        out_bytes, err_bytes, trunc_out, trunc_err, timed_out = _pump(
            proc, timeout_sec, max_output_bytes, cancellation,
            stdout_sink, stderr_sink, keep_stdout,
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


def _pump(  # noqa: PLR0913
    proc: subprocess.Popen[bytes],
    timeout_sec: int,
    max_output_bytes: int,
    cancellation: TurnCancellation,
    stdout_sink: Callable[[bytes], None] | None,
    stderr_sink: Callable[[bytes], None] | None,
    keep_stdout: bool,
) -> tuple[bytes, bytes, bool, bool, bool]:
    if proc.stdout is None or proc.stderr is None:
        raise ShellRunnerInvariantError(
            "_pump: proc.stdout and proc.stderr expected (Popen started with PIPE)"
        )

    deadline = time.monotonic() + timeout_sec
    fds = {proc.stdout.fileno(): "out", proc.stderr.fileno(): "err"}
    buffers = {"out": bytearray(), "err": bytearray()}
    sinks: dict[str, Callable[[bytes], None] | None] = {
        "out": stdout_sink,
        "err": stderr_sink,
    }
    # stderr копится и при живом релее: его хвост объясняет падение процесса
    keeps = {"out": keep_stdout, "err": True}
    truncated = {"out": False, "err": False}
    open_fds = set(fds.keys())

    try:
        timed_out = _select_loop(
            deadline, fds, buffers, sinks, keeps, truncated, open_fds,
            max_output_bytes, cancellation,
        )
    except Exception:
        # потребитель оборвал поток: процесс дальше не нужен
        proc.kill()
        proc.wait()
        proc.stdout.close()
        proc.stderr.close()
        raise

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


def _select_loop(  # noqa: PLR0913
    deadline: float,
    fds: dict[int, str],
    buffers: dict[str, bytearray],
    sinks: dict[str, Callable[[bytes], None] | None],
    keeps: dict[str, bool],
    truncated: dict[str, bool],
    open_fds: set[int],
    max_output_bytes: int,
    cancellation: TurnCancellation,
) -> bool:
    """Чтение обоих потоков до EOF; True — упёрлись в дедлайн."""
    while open_fds:
        if cancellation.cancelled:
            return False
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return True
        ready, _, _ = select.select(list(open_fds), [], [], min(remaining, 1.0))
        if not ready:
            continue
        for fd in ready:
            tag = fds[fd]
            more = _read_chunk(
                fd, buffers[tag], truncated, tag,
                max_output_bytes, sinks[tag], keeps[tag],
            )
            if not more:
                open_fds.discard(fd)
    return False


def _read_chunk(  # noqa: PLR0913
    fd: int,
    buf: bytearray,
    truncated: dict[str, bool],
    tag: str,
    max_output_bytes: int,
    sink: Callable[[bytes], None] | None,
    keep: bool,
) -> bool:
    chunk = os.read(fd, 65536)
    if not chunk:
        return False
    if sink is not None:
        sink(chunk)
    if not keep:
        return True
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
