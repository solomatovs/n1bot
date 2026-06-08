"""Тесты subprocess-обёртки _runner.run_subprocess.

Запускают обычный /bin/bash без bwrap — проверяют именно
плюшки runner'а (timeout, size-cap, stdin, отдельные потоки,
обязательность параметров).
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from boba.tool.shell._runner import run_subprocess

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None,
    reason="bash отсутствует на хосте",
)

_BASH = "/bin/bash"


def _bash(command: str) -> list[str]:
    return [_BASH, "-c", command]


def _run(
    command: str,
    *,
    stdin_data: bytes = b"",
    timeout_sec: int = 5,
    max_output_bytes: int = 1024,
    cwd: str | None = None,
):
    return run_subprocess(
        _bash(command),
        stdin_data=stdin_data,
        timeout_sec=timeout_sec,
        max_output_bytes=max_output_bytes,
        cwd=cwd if cwd is not None else os.getcwd(),
        env=os.environ,
    )


def test_zero_exit_with_stdout():
    res = _run("echo hello")
    assert res.exit_code == 0
    assert res.stdout.rstrip() == "hello"
    assert res.stderr == ""
    assert not res.timed_out
    assert not res.truncated_stdout


def test_nonzero_exit_is_returned_not_raised():
    res = _run("exit 42")
    assert res.exit_code == 42
    assert not res.timed_out


def test_stderr_collected_separately():
    res = _run("echo out; echo err >&2")
    assert res.stdout.rstrip() == "out"
    assert res.stderr.rstrip() == "err"


def test_stdin_passed_through():
    res = _run("cat", stdin_data=b"ping\n")
    assert res.exit_code == 0
    assert res.stdout == "ping\n"


def test_empty_stdin_means_no_input():
    # cat должен сразу получить EOF и завершиться с пустым stdout.
    res = _run("cat", stdin_data=b"")
    assert res.exit_code == 0
    assert res.stdout == ""


def test_timeout_kills_process():
    res = _run("sleep 10", timeout_sec=1)
    assert res.timed_out
    assert res.duration_ms < 5000


def test_stdout_truncation_marks_flag():
    # 10 KiB вывода, лимит 256 байт -> обрезка
    res = _run("yes x | head -c 10240", max_output_bytes=256)
    assert res.truncated_stdout
    assert len(res.stdout.encode("utf-8")) == 256


def test_truncated_process_still_completes():
    # Регрессия: даже после truncation runner должен дочитывать поток,
    # иначе процесс блокируется на write() и таймаутится.
    res = _run("yes x | head -c 102400; echo END", max_output_bytes=512)
    assert not res.timed_out
    assert res.exit_code == 0
    assert res.truncated_stdout


def test_cwd_changes_working_directory(tmp_path: Path):
    res = _run("pwd", cwd=str(tmp_path))
    assert res.stdout.rstrip() == str(tmp_path)


def test_env_passed_through(tmp_path: Path):
    res = run_subprocess(
        _bash('printf "%s\\n" "$MY_VAR"'),
        stdin_data=b"",
        timeout_sec=5,
        max_output_bytes=1024,
        cwd=str(tmp_path),
        env={"PATH": "/usr/bin:/bin", "MY_VAR": "hello"},
    )
    assert res.exit_code == 0
    assert res.stdout.rstrip() == "hello"


def test_empty_cwd_rejected():
    with pytest.raises(ValueError, match="cwd"):
        run_subprocess(
            _bash("true"),
            stdin_data=b"",
            timeout_sec=5,
            max_output_bytes=1024,
            cwd="",
            env=os.environ,
        )


def test_empty_argv_rejected():
    with pytest.raises(ValueError, match="argv"):
        run_subprocess(
            [],
            stdin_data=b"",
            timeout_sec=5,
            max_output_bytes=1024,
            cwd=os.getcwd(),
            env=os.environ,
        )
