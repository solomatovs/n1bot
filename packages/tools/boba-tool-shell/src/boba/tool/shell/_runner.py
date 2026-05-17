"""Subprocess-обёртка: запуск argv с timeout, size-cap, streaming-сбором.

Принципы:
- argv приходит уже готовый (см. `_sandbox.build_bwrap_argv` для sandbox-
  варианта; для regular shell argv = `[/bin/bash, -c, command]`).
- Все параметры процесса (`stdin_data`, `cwd`, `env`) обязательные и
  типизированы строго, без `Optional`-инвариантов. Caller отвечает за
  то, чтобы значения были корректными: пустой stdin = `b""`, для
  sandbox-вызова в качестве `env` обычно передаётся `os.environ` (bwrap
  внутри сделает `--clearenv` и поставит свой набор через `--setenv`).
- stdout/stderr читаются параллельно через select, обрезаются по байтам
  на каждый поток отдельно (см. `_pump`).
- При таймауте: `Popen.kill()`. Для sandbox-варианта
  `bwrap --die-with-parent` гарантирует, что всё дерево потомков внутри
  песочницы умирает.
- Результат — простой dataclass, без зависимости от ToolResult/JsonResult.
  Обёртывание в `JsonResult` делает caller (`BashSandboxTool.execute` /
  `BashTool.execute`).
"""

from __future__ import annotations

import os
import select
import subprocess
import time
from collections.abc import Mapping
from dataclasses import dataclass

__all__ = ["RunResult", "ShellRunnerInvariantError", "run_subprocess"]


class ShellRunnerInvariantError(Exception):
    """Нарушение внутреннего инварианта shell-runner'а (например, отсутствие
    stdout/stderr у Popen, который запущен с `stdout=PIPE, stderr=PIPE`).

    В отличие от `assert`, переживает запуск с `python -O`.
    """


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


def run_subprocess(
    argv: list[str],
    *,
    stdin_data: bytes,
    timeout_sec: int,
    max_output_bytes: int,
    cwd: str,
    env: Mapping[str, str],
) -> RunResult:
    """Запустить `argv` дочерним процессом и собрать его stdout/stderr.

    Все параметры обязательны и валидируются на входе:
    - `argv` — non-empty list, первый элемент — абсолютный путь к бинарю;
    - `stdin_data=b""` означает «нет stdin» (пайп закрывается без записи);
    - `cwd` — non-empty абсолютный путь к существующей директории;
    - `env` — финальный dict env для дочернего процесса.

    Контракт:
    - возвращает `RunResult` даже на таймаут (`exit_code=-9`,
      `timed_out=True`);
    - не бросает `CalledProcessError`: non-zero exit — это валидный
      результат;
    - `stdout`/`stderr` декодируются как utf-8 с `errors='replace'`.
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
    """Записать `data` в stdin процесса и закрыть пайп.

    `data=b""` — корректный кейс: stdin закрывается без записи, дочерний
    процесс получает EOF на первом же read.
    """
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
    """Параллельное чтение stdout/stderr с лимитами и общим дедлайном.

    Возвращает `(stdout, stderr, trunc_out, trunc_err, timed_out)`.
    """
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
