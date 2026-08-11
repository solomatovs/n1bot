"""Превышение лимита должно объясняться словами, а не кодом ядра."""

# ruff: noqa: S108

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from boba.sandbox.caller import SandboxCaller
from boba.sandbox.diagnostics import SandboxDiagnostics
from boba.sandbox.process_runner import RunResult
from boba.sandbox.profile import SandboxProfile
from boba.tool.shell.tools import build_bash_tool
from boba.workspace.launcher import FUSE_DEVICE

HOST_RO_BINDS = ("/usr", "/bin", "/sbin", "/lib", "/lib64", "/etc/alternatives")

needs_sandbox = pytest.mark.skipif(
    shutil.which("bwrap") is None
    or shutil.which("fuse2fs") is None
    or shutil.which("mkfs.ext4") is None
    or not os.path.exists(FUSE_DEVICE),
    reason="нужны bwrap, fuse2fs, mkfs.ext4 и /dev/fuse",
)


def _bin_dirs() -> list[str]:
    """В тестах каталоги берутся из PATH; в проде их задаёт конфиг."""
    dirs: list[str] = []

    for entry in os.environ.get("PATH", "").split(os.pathsep):
        if not entry.startswith("/"):
            continue

        dirs.append(entry)

    return dirs


_PROFILE_BASE: dict[str, object] = {
    "rootfs": "",
    "ro_binds": HOST_RO_BINDS,
    "rw_binds": (),
    "rw_images": (),
    "image_template": "",
    "launcher": {
        "mount_wait_sec": 10.0,
        "mount_poll_sec": 0.05,
        "shutdown_wait_sec": 5.0,
        "lock_wait_sec": 10.0,
        "copy_chunk_bytes": 1 << 20,
    },
    "binaries": {"dirs": _bin_dirs()},
    "tmpfs": ("/tmp:64M",),
    "network": False,
    "env_set": {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": "/tmp"},
    "timeout_sec": 30,
    "max_memory_bytes": 512 * 1024 * 1024,
    "max_cpu_sec": 30,
    "max_file_size_bytes": 64 * 1024 * 1024,
    "max_open_files": 1024,
    "max_processes": 256,
    "cgroup_base": "",
    "oom_score_adj": 0,
    "cwd": "/tmp",
}


@pytest.fixture(autouse=True)
def chainlit_context() -> None:
    pass


@pytest.fixture
def template(tmp_path: Path) -> Path:
    path = tmp_path / "template.ext4"
    with path.open("wb") as f:
        f.truncate(16 * 1024 * 1024)
    mkfs = shutil.which("mkfs.ext4")
    assert mkfs is not None
    subprocess.run(  # noqa: S603
        [mkfs, "-F", "-q", "-O", "^has_journal", "-m", "0", str(path)],
        check=True,
    )
    return path


def _profile(**kw: object) -> SandboxProfile:
    return SandboxProfile.model_validate({**_PROFILE_BASE, **kw})


def _result(**kw: object) -> RunResult:
    fields: dict[str, object] = {
        "exit_code": 1,
        "stdout": "",
        "stderr": "",
        "duration_ms": 10,
        "timed_out": False,
    }
    fields.update(kw)
    return RunResult(**fields)  # type: ignore[arg-type]


def _explain(result: RunResult, profile: SandboxProfile) -> str:
    return SandboxDiagnostics.explain(result, profile)


class TestDiagnosticText:
    """Текст обязан назвать лимит, его значение и что делать дальше."""

    def test_timeout_names_limit(self) -> None:
        text = _explain(_result(timed_out=True), _profile(timeout_sec=7))
        assert "timeout_sec=7" in text
        assert "timeout" in text.lower()

    def test_cpu_limit_named(self) -> None:
        text = _explain(_result(exit_code=152), _profile(max_cpu_sec=5))
        assert "max_cpu_sec=5" in text
        assert "SIGXCPU" in text

    def test_file_size_limit_named(self) -> None:
        result = _result(stderr="bash: line 1: File size limit exceeded")
        text = _explain(result, _profile(max_file_size_bytes=1024))
        assert "max_file_size_bytes=1024" in text

    def test_open_files_limit_named(self) -> None:
        result = _result(stderr="OSError: [Errno 24] Too many open files: 'x'")
        text = _explain(result, _profile(max_open_files=10))
        assert "max_open_files=10" in text

    def test_process_limit_named(self) -> None:
        result = _result(stderr="bash: fork: retry: Resource temporarily unavailable")
        text = _explain(result, _profile(max_processes=10))
        assert "max_processes=10" in text

    def test_memory_limit_named(self) -> None:
        result = _result(stderr="MemoryError")
        text = _explain(result, _profile(max_memory_bytes=64 * 1024 * 1024))
        assert "max_memory_bytes=67108864" in text

    def test_full_image_explained(self) -> None:
        result = _result(stderr="dd: writing 'big': No space left on device")
        text = _explain(result, _profile())
        assert "workspace image" in text
        assert "No space left" in text

    def test_network_disabled_explained_with_alternatives(self) -> None:
        result = _result(
            stderr="socket.gaierror: [Errno -3] Temporary failure in name resolution"
        )
        text = _explain(result, _profile(network=False))
        assert "network=false" in text
        assert "not at fault" in text

    def test_network_error_ignored_when_network_enabled(self) -> None:
        result = _result(stderr="Temporary failure in name resolution")
        assert _explain(result, _profile(network=True)) == ""

    def test_plain_failure_has_no_diagnostic(self) -> None:
        result = _result(stderr="cat: f.txt: No such file or directory")
        assert _explain(result, _profile()) == ""

    def test_success_has_no_diagnostic(self) -> None:
        assert _explain(_result(exit_code=0), _profile()) == ""


def _launchers(profile: SandboxProfile):
    def launcher(tool: str):
        return SandboxCaller(tool, profile, lambda: {"user_id": "7", "thread_id": "t1"})

    return launcher


def _tool(profile: SandboxProfile):
    return build_bash_tool(_launchers(profile))


def _invoke(tool, command: str, stdin: str = "") -> dict:
    msg = tool.invoke(
        {
            "args": {"command": command, "stdin": stdin},
            "id": "call-bash",
            "name": "bash",
            "type": "tool_call",
        }
    )
    return msg.artifact.payload


@needs_sandbox
class TestDiagnosticAppearsLive:
    """Лимит реально превышается — сообщение обязано быть в payload."""

    def test_open_files(self) -> None:
        code = (
            "held = []\n"
            "for i in range(200):\n"
            "    held.append(open('/tmp/probe-%d' % i, 'w'))\n"
        )
        tool = _tool(_profile(max_open_files=10))
        payload = _invoke(tool, "python3 -", stdin=code)
        assert payload["exit_code"] != 0
        assert "max_open_files=10" in payload["diagnostic"]

    def test_processes(self) -> None:
        command = "for i in $(seq 1 50); do sleep 5 & done; wait"
        tool = _tool(_profile(max_processes=10, timeout_sec=20))
        payload = _invoke(tool, command)
        assert "max_processes=10" in payload["diagnostic"]

    def test_file_size(self) -> None:
        tool = _tool(_profile(max_file_size_bytes=1024 * 1024, tmpfs=("/tmp:64M",)))
        payload = _invoke(tool, "dd if=/dev/zero of=/tmp/big bs=64k count=64")
        assert payload["exit_code"] != 0
        assert "max_file_size_bytes=1048576" in payload["diagnostic"]

    def test_memory(self) -> None:
        code = "x = bytearray(400 * 1024 * 1024)\n"
        tool = _tool(_profile(max_memory_bytes=64 * 1024 * 1024))
        payload = _invoke(tool, "python3 -", stdin=code)
        assert payload["exit_code"] != 0
        assert "max_memory_bytes=67108864" in payload["diagnostic"]

    def test_timeout(self) -> None:
        tool = _tool(_profile(timeout_sec=1))
        payload = _invoke(tool, "sleep 10")
        assert payload["timed_out"] is True
        assert "timeout_sec=1" in payload["diagnostic"]

    def test_network_disabled_explained(self) -> None:
        code = "import socket\nsocket.getaddrinfo('example.com', 443)\n"
        tool = _tool(_profile(network=False))
        payload = _invoke(tool, "python3 -", stdin=code)
        assert payload["exit_code"] != 0
        assert "network=false" in payload["diagnostic"]
        assert "not at fault" in payload["diagnostic"]

    def test_successful_command_has_empty_diagnostic(self) -> None:
        payload = _invoke(_tool(_profile()), "echo ok")
        assert payload["exit_code"] == 0
        assert payload["diagnostic"] == ""
