"""Тесты subprocess-обёртки `_runner.run_sandboxed`.

Запускают обычный `/bin/bash` без bwrap — проверяют именно
плюшки runner'а (timeout, size-cap, stdin, отдельные потоки).
"""

from __future__ import annotations

import shutil

import pytest

from boba.tool.shell._runner import run_sandboxed

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None,
    reason="bash отсутствует на хосте",
)

_BASH = "/bin/bash"


def _bash(command: str) -> list[str]:
    return [_BASH, "-c", command]


def test_zero_exit_with_stdout():
    res = run_sandboxed(
        _bash("echo hello"),
        stdin=None, timeout_sec=5, max_output_bytes=1024,
    )
    assert res.exit_code == 0
    assert res.stdout.rstrip() == "hello"
    assert res.stderr == ""
    assert not res.timed_out
    assert not res.truncated_stdout


def test_nonzero_exit_is_returned_not_raised():
    res = run_sandboxed(
        _bash("exit 42"),
        stdin=None, timeout_sec=5, max_output_bytes=1024,
    )
    assert res.exit_code == 42
    assert not res.timed_out


def test_stderr_collected_separately():
    res = run_sandboxed(
        _bash("echo out; echo err >&2"),
        stdin=None, timeout_sec=5, max_output_bytes=1024,
    )
    assert res.stdout.rstrip() == "out"
    assert res.stderr.rstrip() == "err"


def test_stdin_passed_through():
    res = run_sandboxed(
        _bash("cat"),
        stdin="ping\n",
        timeout_sec=5,
        max_output_bytes=1024,
    )
    assert res.exit_code == 0
    assert res.stdout == "ping\n"


def test_timeout_kills_process():
    res = run_sandboxed(
        _bash("sleep 10"),
        stdin=None, timeout_sec=1, max_output_bytes=1024,
    )
    assert res.timed_out
    assert res.duration_ms < 5000


def test_stdout_truncation_marks_flag():
    # 10 KiB вывода, лимит 256 байт → обрезка
    res = run_sandboxed(
        _bash("yes x | head -c 10240"),
        stdin=None, timeout_sec=5, max_output_bytes=256,
    )
    assert res.truncated_stdout
    assert len(res.stdout.encode("utf-8")) == 256


def test_truncated_process_still_completes():
    # Регрессия: даже после truncation runner должен дочитывать поток,
    # иначе процесс блокируется на write() и таймаутится.
    res = run_sandboxed(
        _bash("yes x | head -c 102400; echo END"),
        stdin=None, timeout_sec=5, max_output_bytes=512,
    )
    assert not res.timed_out
    assert res.exit_code == 0
    assert res.truncated_stdout
