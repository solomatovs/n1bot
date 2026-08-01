"""Тесты инструментов: bash_local, bash (bwrap-песочница) и visualize (chart)."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import cast

import pytest
from langchain_core.messages import ToolMessage
from pydantic import BaseModel

from boba.chainlit2.agent.tools import (
    SandboxProfile,
    build_bash_local_tool,
    build_bash_tool,
    visualize,
)
from boba.chainlit2.agent.tools.config import BashLocalConfig, BashSandboxConfig
from boba.chainlit2.agent.tools.sandbox import build_bwrap_argv
from boba.chainlit2.rendering.tool_result import ChartResult, JsonResult


@pytest.fixture(autouse=True)
def chainlit_context() -> None:
    """Чистые юнит-тесты инструментов не требуют HTTP-контекста chainlit."""


def _tool_call(name: str, args: dict) -> dict:
    return {"args": args, "id": f"call-{name}", "name": name, "type": "tool_call"}


class TestBashLocal:
    def test_build_requires_workspace_root(self, tmp_path: Path) -> None:
        cfg = BashLocalConfig(workspace_root=tmp_path)
        tool = build_bash_local_tool(cfg)
        assert tool.name == "bash_local"
        schema = cast(type[BaseModel], tool.args_schema)
        assert set(schema.model_fields) == {"command", "stdin"}

    def test_echo(self, tmp_path: Path) -> None:
        cfg = BashLocalConfig(workspace_root=tmp_path)
        tool = build_bash_local_tool(cfg)
        msg: ToolMessage = tool.invoke(
            _tool_call("bash_local", {"command": "echo hello", "stdin": ""})
        )
        assert isinstance(msg.artifact, JsonResult)
        payload = msg.artifact.payload
        assert payload["exit_code"] == 0
        assert payload["stdout"] == "hello\n"
        assert payload["timed_out"] is False
        assert "hello" in msg.content

    def test_stdin_passed_to_command(self, tmp_path: Path) -> None:
        cfg = BashLocalConfig(workspace_root=tmp_path)
        tool = build_bash_local_tool(cfg)
        msg: ToolMessage = tool.invoke(
            _tool_call(
                "bash_local",
                {"command": "cat", "stdin": "line1\nline2\n"},
            )
        )
        assert msg.artifact.payload["stdout"] == "line1\nline2\n"

    def test_nonzero_exit_is_not_error(self, tmp_path: Path) -> None:
        cfg = BashLocalConfig(workspace_root=tmp_path)
        tool = build_bash_local_tool(cfg)
        msg: ToolMessage = tool.invoke(
            _tool_call("bash_local", {"command": "exit 3", "stdin": ""})
        )
        assert msg.artifact.payload["exit_code"] == 3
        assert msg.status == "success"

    def test_timeout_kills_process(self, tmp_path: Path) -> None:
        cfg = BashLocalConfig(workspace_root=tmp_path, timeout_sec=1)
        tool = build_bash_local_tool(cfg)
        msg: ToolMessage = tool.invoke(
            _tool_call("bash_local", {"command": "sleep 10", "stdin": ""})
        )
        assert msg.artifact.payload["timed_out"] is True
        assert msg.artifact.payload["exit_code"] == -9


class TestVisualize:
    def test_valid_spec_returns_chart(self) -> None:
        spec = '{"data":[{"type":"bar","x":[1,2],"y":[3,1]}],"layout":{"title":"T"}}'
        msg: ToolMessage = visualize.invoke(_tool_call("visualize", {"spec": spec}))
        assert isinstance(msg.artifact, ChartResult)
        assert msg.artifact.title == "T"
        layout_title = msg.artifact.spec["layout"]["title"]
        assert layout_title in ("T", {"text": "T"})
        assert msg.content == "[chart rendered: T]"

    def test_invalid_json_raises(self) -> None:
        with pytest.raises(RuntimeError, match="JSON"):
            visualize.invoke(_tool_call("visualize", {"spec": "{not json"}))

    def test_non_object_spec_raises(self) -> None:
        with pytest.raises(RuntimeError, match="JSON-объектом"):
            visualize.invoke(_tool_call("visualize", {"spec": "[1,2,3]"}))

    def test_invalid_plotly_spec_raises(self) -> None:
        spec = '{"data": 42}'
        with pytest.raises(RuntimeError, match="невалидный Plotly"):
            visualize.invoke(_tool_call("visualize", {"spec": spec}))


class TestBwrapArgv:
    """Юниты pure-builder'а argv: не требуют установленного bwrap."""

    _WS = "/srv/workspace"

    def test_starts_with_bwrap_and_unshare_flags(self) -> None:
        argv = build_bwrap_argv(
            SandboxProfile(), "echo hi", workspace_root=self._WS, env={},
        )
        assert argv[0] == "bwrap"
        assert "--die-with-parent" in argv
        assert "--unshare-user" in argv
        assert "--unshare-pid" in argv
        assert "--new-session" in argv

    def test_network_disabled_adds_unshare_net(self) -> None:
        argv = build_bwrap_argv(
            SandboxProfile(network=False), "true", workspace_root=self._WS, env={},
        )
        assert "--unshare-net" in argv

    def test_network_enabled_omits_unshare_net(self) -> None:
        argv = build_bwrap_argv(
            SandboxProfile(network=True), "true", workspace_root=self._WS, env={},
        )
        assert "--unshare-net" not in argv

    def test_workspace_rw_bind_and_chdir(self) -> None:
        argv = build_bwrap_argv(
            SandboxProfile(), "true", workspace_root=self._WS, env={},
        )
        idx = argv.index(self._WS)
        assert argv[idx - 1] == "--bind-try"
        assert argv[argv.index("--chdir") + 1] == self._WS

    def test_rootfs_mounted_as_root_before_proc_dev(self) -> None:
        # rootfs задаёт корень, поэтому монтируется ДО /proc и /dev —
        # иначе ro-bind перекрыл бы их и /dev/null стал read-only.
        argv = build_bwrap_argv(
            SandboxProfile(rootfs="/srv/rootfs", ro_binds=()),
            "true",
            workspace_root=self._WS,
            env={},
        )
        i = argv.index("--ro-bind")
        assert argv[i + 1 : i + 3] == ["/srv/rootfs", "/"]
        assert i < argv.index("--proc")
        assert i < argv.index("--dev")

    def test_rootfs_moves_workspace_to_fixed_mount(self) -> None:
        # на read-only корне bwrap не создаст точку по host-пути,
        # поэтому workspace монтируется в /workspace и туда же chdir
        argv = build_bwrap_argv(
            SandboxProfile(rootfs="/srv/rootfs", ro_binds=()),
            "true",
            workspace_root=self._WS,
            env={},
        )
        i = argv.index("--bind-try")
        assert argv[i + 1 : i + 3] == [self._WS, "/workspace"]
        assert argv[argv.index("--chdir") + 1] == "/workspace"

    def test_without_rootfs_workspace_keeps_host_path(self) -> None:
        argv = build_bwrap_argv(
            SandboxProfile(), "true", workspace_root=self._WS, env={},
        )
        assert "--ro-bind" not in argv  # корень не монтируется
        assert argv[argv.index("--chdir") + 1] == self._WS

    def test_env_cleared_and_set(self) -> None:
        argv = build_bwrap_argv(
            SandboxProfile(),
            "true",
            workspace_root=self._WS,
            env={"PATH": "/usr/bin:/bin"},
        )
        assert "--clearenv" in argv
        i = argv.index("--setenv")
        assert argv[i + 1 : i + 3] == ["PATH", "/usr/bin:/bin"]

    def test_command_goes_after_separator(self) -> None:
        argv = build_bwrap_argv(
            SandboxProfile(), "echo hi", workspace_root=self._WS, env={},
        )
        sep = argv.index("--")
        assert argv[sep + 1 :] == ["/bin/bash", "-c", "echo hi"]


@pytest.mark.skipif(
    shutil.which("bwrap") is None,
    reason="требуется bubblewrap (`apt install bubblewrap`)",
)
class TestBashTool:
    """Интеграционные: реально запускают bwrap."""

    @staticmethod
    def _make_tool(workspace_root: Path, profile: SandboxProfile | None = None):
        cfg = BashSandboxConfig(
            workspace_root=workspace_root,
            profiles={"default": profile or SandboxProfile()},
            default_profile="default",
        )
        return build_bash_tool(cfg)

    @staticmethod
    def _invoke(tool, **args) -> dict:
        args.setdefault("stdin", "")
        args.setdefault("profile", "")
        msg: ToolMessage = tool.invoke(_tool_call("bash", args))
        assert isinstance(msg.artifact, JsonResult)
        return msg.artifact.payload

    def test_echo_inside_sandbox(self, tmp_path: Path) -> None:
        payload = self._invoke(self._make_tool(tmp_path), command="echo hello")
        assert payload["exit_code"] == 0
        assert payload["stdout"].rstrip() == "hello"
        assert not payload["timed_out"]

    def test_cwd_is_workspace_root(self, tmp_path: Path) -> None:
        payload = self._invoke(self._make_tool(tmp_path), command="pwd")
        assert payload["stdout"].rstrip() == str(tmp_path.resolve())

    def test_workspace_writes_persist_on_host(self, tmp_path: Path) -> None:
        payload = self._invoke(
            self._make_tool(tmp_path), command="echo content > out.txt",
        )
        assert payload["exit_code"] == 0
        assert (tmp_path / "out.txt").read_text() == "content\n"

    def test_outside_workspace_write_does_not_reach_host(
        self, tmp_path: Path
    ) -> None:
        # /etc внутри песочницы — синтетический tmpfs bwrap (создан под
        # ro-bind'ы alternatives/resolv.conf): запись туда «удаётся», но
        # исчезает вместе с песочницей. Проверяем главное — хост не тронут.
        payload = self._invoke(
            self._make_tool(tmp_path),
            command="echo x > /etc/from-sandbox 2>&1; echo rc=$?",
        )
        assert payload["exit_code"] == 0
        assert not Path("/etc/from-sandbox").exists()

    def test_ro_bind_write_denied(self, tmp_path: Path) -> None:
        # /usr замонтирован read-only — запись обязана падать.
        payload = self._invoke(
            self._make_tool(tmp_path),
            command="echo x > /usr/from-sandbox 2>&1",
        )
        assert payload["exit_code"] != 0
        assert not Path("/usr/from-sandbox").exists()

    def test_network_disabled_by_default(self, tmp_path: Path) -> None:
        payload = self._invoke(
            self._make_tool(tmp_path),
            command="getent hosts example.com 2>&1; echo done-$?",
        )
        assert "done-2" in payload["stdout"] or "done-1" in payload["stdout"]

    def test_timeout_marks_timed_out(self, tmp_path: Path) -> None:
        payload = self._invoke(
            self._make_tool(tmp_path, SandboxProfile(timeout_sec=1)),
            command="sleep 10",
        )
        assert payload["timed_out"]

    def test_unknown_profile_returns_error_payload(self, tmp_path: Path) -> None:
        payload = self._invoke(
            self._make_tool(tmp_path), command="echo x", profile="no-such",
        )
        assert payload["error_kind"] == "unknown_profile"
        assert payload["exit_code"] == -1

    def test_pid_namespace_isolation(self, tmp_path: Path) -> None:
        payload = self._invoke(
            self._make_tool(tmp_path),
            command="ps -e --no-headers | wc -l",
        )
        # внутри PID-ns хост-процессы не видны; только bash + ps.
        assert int(payload["stdout"].strip()) < 10
