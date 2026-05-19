"""Integration-тесты BashSandboxTool: реально запускают bwrap.

Skipif если bubblewrap не установлен. Установка на Debian-like:
    sudo apt install bubblewrap
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from boba.tool.shell._profile import SandboxProfile
from boba.tool.shell.bash_sandbox import bash_sandbox
from boba.tool.shell.config import BashSandboxConfig

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        shutil.which("bwrap") is None,
        reason="требуется bubblewrap (`apt install bubblewrap`)",
    ),
]


def _make_cfg(
    workspace_root: Path,
    profile: SandboxProfile | None = None,
) -> BashSandboxConfig:
    return BashSandboxConfig(
        enable=True,
        workspace_root=workspace_root,
        profiles={"default": profile or SandboxProfile()},
        default_profile="default",
    )


def _exec(cfg: BashSandboxConfig, **kwargs) -> dict:
    """Tool как обычный callable. Возвращает payload (dict)."""
    return bash_sandbox(cfg=cfg, **kwargs)


def test_echo_inside_sandbox(tmp_path: Path):
    payload = _exec(_make_cfg(tmp_path), command="echo hello")
    assert payload["exit_code"] == 0
    assert payload["stdout"].rstrip() == "hello"
    assert not payload["timed_out"]


def test_cwd_is_workspace_root(tmp_path: Path):
    payload = _exec(_make_cfg(tmp_path), command="pwd")
    assert payload["stdout"].rstrip() == str(tmp_path.resolve())


def test_workspace_writes_persist_on_host(tmp_path: Path):
    payload = _exec(_make_cfg(tmp_path), command="echo content > out.txt")
    assert payload["exit_code"] == 0
    assert (tmp_path / "out.txt").read_text() == "content\n"


def test_outside_workspace_write_denied(tmp_path: Path):
    # /etc недоступен RW (ro-bind), запись должна свалиться с non-zero.
    payload = _exec(
        _make_cfg(tmp_path),
        command="echo x > /etc/from-sandbox 2>&1",
    )
    assert payload["exit_code"] != 0


def test_network_disabled_by_default(tmp_path: Path):
    # getent hosts требует резолва; в network-namespace без сети — фейл.
    payload = _exec(
        _make_cfg(tmp_path),
        command="getent hosts example.com 2>&1; echo done-$?",
    )
    assert "done-2" in payload["stdout"] or "done-1" in payload["stdout"]


def test_timeout_marks_timed_out(tmp_path: Path):
    payload = _exec(
        _make_cfg(tmp_path, profile=SandboxProfile(timeout_sec=1)),
        command="sleep 10",
    )
    assert payload["timed_out"]
    assert payload["duration_ms"] < 5000


def test_output_truncation(tmp_path: Path):
    payload = _exec(
        _make_cfg(tmp_path, profile=SandboxProfile(max_output_bytes=1024)),
        command="yes x | head -c 10240",
    )
    assert payload["truncated_stdout"]
    assert len(payload["stdout"].encode("utf-8")) == 1024


def test_unknown_profile_returns_error_payload(tmp_path: Path):
    payload = _exec(_make_cfg(tmp_path), command="echo x", profile="no-such")
    assert payload["error_kind"] == "unknown_profile"
    assert payload["exit_code"] == -1


def test_pid_namespace_isolation(tmp_path: Path):
    payload = _exec(
        _make_cfg(tmp_path),
        command="ps -e --no-headers | wc -l",
    )
    # внутри PID-ns хост-процессы не видны; только bash + ps.
    count = int(payload["stdout"].strip())
    assert count < 10
