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
  на каждый поток отдельно (см. `_pump_stream`).
- При таймауте: `Popen.kill()`. Для sandbox-варианта
  `bwrap --die-with-parent` гарантирует, что всё дерево потомков внутри
  песочницы умирает.
- Результат — простой dataclass, без зависимости от ToolResult/JsonResult.
  Обёртывание в `JsonResult` делает caller (`bash_sandbox` / `BashTool.execute`).

Два API:
- `run_subprocess(...)` — синхронный, возвращает `RunResult` целиком.
  Используется bash_local и тестами `_runner`.
- `run_subprocess_stream(...)` — generator, yield-ит `(tag, line)` для
  каждой полной строки stdout/stderr live и через `return` отдаёт
  финальный `RunResult`. Используется `bash_sandbox` для streaming-
  индикации прогресса в UI.

`run_subprocess` реализован как drain `run_subprocess_stream` — единый
путь, никакой дубликат Popen/pump логики.
"""

from __future__ import annotations

import os
import select
import subprocess
import time
from collections.abc import Generator, Iterator, Mapping
from dataclasses import dataclass

__all__ = [
    "RunResult",
    "ShellRunnerInvariantError",
    "run_subprocess",
    "run_subprocess_stream",
]


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


def run_subprocess_stream(  # noqa: PLR0913 — keyword-only API, явный набор runtime-параметров
    argv: list[str],
    *,
    stdin_data: bytes,
    timeout_sec: int,
    max_output_bytes: int,
    cwd: str,
    env: Mapping[str, str],
) -> Generator[tuple[str, str], None, RunResult]:
    """Запустить `argv` как generator: yield-ит `(tag, line)` per полная строка
    stdout/stderr live; через `return` отдаёт финальный `RunResult`.

    `tag` ∈ {`"out"`, `"err"`}. Строки — без trailing newline. Декодинг
    utf-8 c `errors='replace'`. Незавершённый tail в конце потока (если
    процесс не закрыл вывод newline'ом) тоже yield-ится одной финальной
    строкой.

    Параметры — те же, что у `run_subprocess`. Truncation/timeout
    обрабатываются идентично: при превышении `max_output_bytes` дальнейшие
    байты этого потока игнорируются (но yield-и продолжаются для другого
    потока); при таймауте процесс убивается, `timed_out=True` в RunResult.
    """
    if not argv:
        msg = "run_subprocess_stream: argv не может быть пустым"
        raise ValueError(msg)
    if not cwd:
        msg = "run_subprocess_stream: cwd должен быть непустой строкой"
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
    out_bytes, err_bytes, trunc_out, trunc_err, timed_out = yield from _pump_stream(
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


def run_subprocess(  # noqa: PLR0913 — keyword-only API, явный набор runtime-параметров
    argv: list[str],
    *,
    stdin_data: bytes,
    timeout_sec: int,
    max_output_bytes: int,
    cwd: str,
    env: Mapping[str, str],
) -> RunResult:
    """Sync-обёртка над `run_subprocess_stream`: дренирует поток line-yield'ов
    и возвращает финальный `RunResult` из `StopIteration.value`.

    Контракт идентичен предыдущей не-streaming реализации:
    - возвращает `RunResult` даже на таймаут (`exit_code=-9`,
      `timed_out=True`);
    - не бросает `CalledProcessError`: non-zero exit — это валидный
      результат;
    - `stdout`/`stderr` декодируются как utf-8 с `errors='replace'`.
    """
    gen = run_subprocess_stream(
        argv,
        stdin_data=stdin_data,
        timeout_sec=timeout_sec,
        max_output_bytes=max_output_bytes,
        cwd=cwd,
        env=env,
    )
    while True:
        try:
            next(gen)
        except StopIteration as stop:
            return stop.value


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


def _pump_stream(  # noqa: C901 — select/timeout/truncation в одном месте, разносить хуже
    proc: subprocess.Popen[bytes],
    timeout_sec: int,
    max_output_bytes: int,
) -> Generator[tuple[str, str], None, tuple[bytes, bytes, bool, bool, bool]]:
    """Параллельное чтение stdout/stderr с лимитами и общим дедлайном;
    yield-ит `(tag, line)` per полная строка.

    Возвращает `(stdout_bytes, stderr_bytes, trunc_out, trunc_err, timed_out)`.
    Незавершённый tail в pending-буфере (без trailing newline) yield-ится
    одной финальной строкой по EOF/таймауту.
    """
    if proc.stdout is None or proc.stderr is None:
        raise ShellRunnerInvariantError(
            "_pump_stream: ожидались proc.stdout и proc.stderr (Popen запущен с PIPE)"
        )

    deadline = time.monotonic() + timeout_sec
    fds = {proc.stdout.fileno(): "out", proc.stderr.fileno(): "err"}
    # Полные собранные байты — для финального RunResult.stdout/stderr.
    buffers = {"out": bytearray(), "err": bytearray()}
    # Незакрытый "current line" — копит байты между newline'ами для yield.
    pending = {"out": bytearray(), "err": bytearray()}
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
            chunk = os.read(fd, 65536)
            if not chunk:
                open_fds.discard(fd)
                continue
            _accumulate(chunk, buffers[tag], truncated, tag, max_output_bytes)
            pending[tag].extend(chunk)
            yield from _drain_lines(pending[tag], tag)

    # Хвост (последняя строка без \n) — тоже yield, чтобы UI её увидел.
    for tag in ("out", "err"):
        if pending[tag]:
            yield (tag, pending[tag].decode("utf-8", errors="replace"))
            pending[tag].clear()

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


def _accumulate(
    chunk: bytes,
    buf: bytearray,
    truncated: dict[str, bool],
    tag: str,
    max_output_bytes: int,
) -> None:
    """Дописать `chunk` в `buf` с учётом cap'а. После переполнения tag
    помечается truncated; дальнейшие байты этого потока игнорируются."""
    if truncated[tag]:
        return
    room = max_output_bytes - len(buf)
    if len(chunk) <= room:
        buf.extend(chunk)
    else:
        if room > 0:
            buf.extend(chunk[:room])
        truncated[tag] = True


def _drain_lines(
    pending: bytearray, tag: str,
) -> Iterator[tuple[str, str]]:
    """Yield-ит `(tag, line)` для каждой полной строки в `pending`; хвост
    после последнего `\\n` остаётся в `pending` (накопится со следующим chunk'ом).
    """
    while True:
        idx = pending.find(b"\n")
        if idx == -1:
            return
        line_bytes = bytes(pending[:idx])
        del pending[: idx + 1]
        yield (tag, line_bytes.decode("utf-8", errors="replace"))
