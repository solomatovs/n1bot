"""Subprocess-обёртка: argv + необязательный timeout, потоки читаются через select.

Кроме stdout/stderr процесса насос читает произвольные дескрипторы каналов
(`pass_fds` + карта fd -> приёмник) и пишет stdin внутри того же цикла —
вход больше буфера пайпа не блокирует ни одну из сторон.

Ошибки: ShellRunnerInvariantError — нарушен инвариант runner'а.
"""

from __future__ import annotations

import contextlib
import io
import os
import select
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import IO, ClassVar

from boba.cancellation import TurnCancellation, current_cancellation
from boba.toolkit.launcher import RunResult
from boba.workspace.launcher import ResourceLimits

__all__ = ["RunResult", "ShellRunnerInvariantError", "run_subprocess"]


class ShellRunnerInvariantError(Exception):
    """Нарушен инвариант runner'а (в отличие от assert переживает -O)."""


@dataclass
class _ReadSlot:
    """Читаемый дескриптор: приёмник и, если нужен, буфер для RunResult."""

    READ_CHUNK: ClassVar[int] = 65536

    sink: Callable[[bytes], None] | None
    buffer: bytearray | None


class _StdinFeed:
    """Запись stdin порциями из select-цикла; закрывается по исчерпании."""

    WRITE_CHUNK: ClassVar[int] = 65536

    def __init__(self, pipe: IO[bytes], data: bytes) -> None:
        self._pipe = pipe
        # сырой слой поверх того же дескриптора: пишет ровно один write(2) и
        # возвращает, сколько байт ушло, — буферизованный объект так не умеет
        self._writer = io.FileIO(pipe.fileno(), "w", closefd=False)
        self._data = data
        self._offset = 0
        self._open = True

    @property
    def fd(self) -> int:
        return self._pipe.fileno()

    @property
    def active(self) -> bool:
        return self._open

    def write_some(self) -> None:
        """Одна порция; BrokenPipe — ребёнок закрыл вход, это законно."""
        if not self._open:
            return

        chunk = self._data[self._offset : self._offset + self.WRITE_CHUNK]
        if not chunk:
            self.close()
            return

        try:
            written = self._writer.write(chunk)
        except BrokenPipeError:
            self.close()
            return

        # None — труба забита прямо сейчас: остаток уйдёт на следующем обходе
        if written is None:
            return

        self._offset += written
        if self._offset >= len(self._data):
            self.close()

    def close(self) -> None:
        if not self._open:
            return

        self._open = False
        self._writer.close()
        # close на пайпе с умершим читателем даёт BrokenPipe — это законно
        with contextlib.suppress(BrokenPipeError):
            self._pipe.close()


def run_subprocess(  # noqa: PLR0913
    argv: list[str],
    *,
    stdin_data: bytes,
    timeout_sec: int | None,
    cwd: str,
    env: Mapping[str, str],
    stdout_sink: Callable[[bytes], None] | None,
    keep_stdout: bool,
    stderr_sink: Callable[[bytes], None] | None = None,
    limits: ResourceLimits | None = None,
    cgroup_dir: str | None = None,
    pass_fds: Sequence[int] = (),
    channel_sinks: Mapping[int, Callable[[bytes], None]] | None = None,
    on_spawn: Callable[[], None] | None = None,
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
        pass_fds=tuple(pass_fds),
        cwd=cwd,
        env=dict(env),
        preexec_fn=preexec,  # noqa: PLW1509
    )

    # родительские копии концов ребёнка закрываются сразу: иначе нет EOF
    if on_spawn is not None:
        on_spawn()

    if limits is not None:
        limits.apply_to_process(proc.pid)

    cancellation = current_cancellation()
    with cancellation.abort_with(proc.kill):
        out_bytes, err_bytes, timed_out = _pump(
            proc,
            stdin_data,
            timeout_sec,
            cancellation,
            stdout_sink,
            stderr_sink,
            keep_stdout,
            channel_sinks or {},
        )

    cancellation.raise_if_cancelled()

    duration_ms = int((time.monotonic() - started) * 1000)
    exit_code = proc.returncode if proc.returncode is not None else -9

    return RunResult(
        exit_code=exit_code,
        stdout=out_bytes.decode("utf-8", errors="replace"),
        stderr=err_bytes.decode("utf-8", errors="replace"),
        duration_ms=duration_ms,
        timed_out=timed_out,
    )


def _pump(  # noqa: PLR0913
    proc: subprocess.Popen[bytes],
    stdin_data: bytes,
    timeout_sec: int | None,
    cancellation: TurnCancellation,
    stdout_sink: Callable[[bytes], None] | None,
    stderr_sink: Callable[[bytes], None] | None,
    keep_stdout: bool,
    channel_sinks: Mapping[int, Callable[[bytes], None]],
) -> tuple[bytes, bytes, bool]:
    if proc.stdin is None or proc.stdout is None or proc.stderr is None:
        raise ShellRunnerInvariantError(
            "_pump: proc stdin/stdout/stderr expected (Popen started with PIPE)"
        )

    deadline: float | None = None
    if timeout_sec is not None:
        deadline = time.monotonic() + timeout_sec

    # stderr копится и при живом релее: его хвост объясняет падение процесса
    out_buffer = bytearray() if keep_stdout else None
    err_buffer = bytearray()

    slots: dict[int, _ReadSlot] = {
        proc.stdout.fileno(): _ReadSlot(sink=stdout_sink, buffer=out_buffer),
        proc.stderr.fileno(): _ReadSlot(sink=stderr_sink, buffer=err_buffer),
    }
    for fd, sink in channel_sinks.items():
        slots[fd] = _ReadSlot(sink=sink, buffer=None)

    feed = _StdinFeed(proc.stdin, stdin_data)

    try:
        timed_out = _select_loop(deadline, slots, feed, cancellation)
    except Exception:
        # потребитель оборвал поток: процесс дальше не нужен
        proc.kill()
        proc.wait()
        _release(proc, feed)
        raise

    if timed_out:
        proc.kill()

    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()

    _release(proc, feed)

    out = bytes(out_buffer) if out_buffer is not None else b""

    return out, bytes(err_buffer), timed_out


def _release(proc: subprocess.Popen[bytes], feed: _StdinFeed) -> None:
    """Закрытие концов родителя; stdin мог закрыться раньше — по исчерпанию."""
    feed.close()
    if proc.stdout is not None:
        proc.stdout.close()

    if proc.stderr is not None:
        proc.stderr.close()


def _select_loop(
    deadline: float | None,
    slots: Mapping[int, _ReadSlot],
    feed: _StdinFeed,
    cancellation: TurnCancellation,
) -> bool:
    """Чтение всех дескрипторов до EOF и запись stdin; True — дедлайн."""
    open_fds = set(slots.keys())

    while open_fds or feed.active:
        if cancellation.cancelled:
            return False

        wait = 1.0
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return True

            wait = min(remaining, 1.0)

        wlist: list[int] = []
        if feed.active:
            wlist.append(feed.fd)

        ready_read, ready_write, _ = select.select(list(open_fds), wlist, [], wait)

        if ready_write:
            feed.write_some()

        for fd in ready_read:
            more = _read_chunk(fd, slots[fd])
            if not more:
                open_fds.discard(fd)

    return False


def _read_chunk(fd: int, slot: _ReadSlot) -> bool:
    chunk = os.read(fd, _ReadSlot.READ_CHUNK)
    if not chunk:
        return False

    if slot.sink is not None:
        slot.sink(chunk)

    if slot.buffer is not None:
        slot.buffer.extend(chunk)

    return True
