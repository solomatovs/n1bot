"""Integration-тесты BashTool: реально запускают bwrap.

Skipif если bubblewrap не установлен. Установка на Debian-like:
    sudo apt install bubblewrap
"""

from __future__ import annotations

import shutil
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from boba.plugin.prompt import PromptOverlay
from boba.tool.shell._profile import SandboxProfile
from boba.tool.shell.bash import BashArgs, BashTool, BashToolConfig
from boba.tools.domain import JsonResult, ToolContext, ToolSourceId

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        shutil.which("bwrap") is None,
        reason="требуется bubblewrap (`apt install bubblewrap`)",
    ),
]

_SID = ToolSourceId("plugin.shell")


def _make_tool(
    workspace_root: Path,
    profile: SandboxProfile | None = None,
) -> BashTool:
    profiles = {"default": profile or SandboxProfile()}
    return BashTool(
        BashToolConfig(prompt=PromptOverlay()),
        MagicMock(),
        _SID,
        workspace_root=str(workspace_root),
        profiles=profiles,
        default_profile="default",
    )


def _exec(tool: BashTool, **kwargs) -> dict:
    args = BashArgs(**kwargs)
    result = tool.execute(ToolContext(), args)
    assert isinstance(result, JsonResult)
    return result.payload


def test_echo_inside_sandbox(tmp_path: Path):
    tool = _make_tool(tmp_path)
    payload = _exec(tool, command="echo hello")
    assert payload["exit_code"] == 0
    assert payload["stdout"].rstrip() == "hello"
    assert not payload["timed_out"]


def test_cwd_is_workspace_root(tmp_path: Path):
    tool = _make_tool(tmp_path)
    payload = _exec(tool, command="pwd")
    assert payload["stdout"].rstrip() == str(tmp_path)


def test_workspace_writes_persist_on_host(tmp_path: Path):
    tool = _make_tool(tmp_path)
    payload = _exec(tool, command="echo content > out.txt")
    assert payload["exit_code"] == 0
    assert (tmp_path / "out.txt").read_text() == "content\n"


def test_outside_workspace_write_denied(tmp_path: Path):
    tool = _make_tool(tmp_path)
    # /etc недоступен RW (ro-bind), запись должна свалиться с non-zero.
    payload = _exec(tool, command="echo x > /etc/from-sandbox 2>&1")
    assert payload["exit_code"] != 0


def test_network_disabled_by_default(tmp_path: Path):
    tool = _make_tool(tmp_path)
    # getent hosts требует резолва; в network-namespace без сети — фейл.
    payload = _exec(
        tool,
        command="getent hosts example.com 2>&1; echo done-$?",
    )
    assert "done-2" in payload["stdout"] or "done-1" in payload["stdout"]


def test_timeout_marks_timed_out(tmp_path: Path):
    tool = _make_tool(
        tmp_path,
        profile=SandboxProfile(timeout_sec=1),
    )
    payload = _exec(tool, command="sleep 10")
    assert payload["timed_out"]
    assert payload["duration_ms"] < 5000


def test_output_truncation(tmp_path: Path):
    tool = _make_tool(
        tmp_path,
        profile=SandboxProfile(max_output_bytes=1024),
    )
    payload = _exec(tool, command="yes x | head -c 10240")
    assert payload["truncated_stdout"]
    assert len(payload["stdout"].encode("utf-8")) == 1024


def test_unknown_profile_returns_error_payload(tmp_path: Path):
    tool = _make_tool(tmp_path)
    payload = _exec(tool, command="echo x", profile="no-such")
    assert payload["error_kind"] == "unknown_profile"
    assert payload["exit_code"] == -1


def test_pid_namespace_isolation(tmp_path: Path):
    tool = _make_tool(tmp_path)
    payload = _exec(tool, command="ps -e --no-headers | wc -l")
    # внутри PID-ns хост-процессы не видны; только bash + ps.
    count = int(payload["stdout"].strip())
    assert count < 10
