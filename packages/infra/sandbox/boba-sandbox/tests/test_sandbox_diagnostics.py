"""Превышение лимита должно объясняться словами, а не кодом ядра."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
from zygote_stand import SandboxStand, ZygoteStand

from boba.sandbox.diagnostics import SandboxDiagnostics
from boba.sandbox.profile import SandboxProfile
from boba.tool.shell.tools import BashToolConfig, build_bash_tool
from boba.toolkit.launcher import RunResult
from boba.toolkit.result import ShellResult
from boba.workspace.launcher import FUSE_DEVICE

HOST_RO_BINDS = ("/usr", "/bin", "/sbin", "/lib", "/lib64", "/etc/alternatives")

OUTPUT_LIMITS = BashToolConfig(max_output_bytes=4 * 1024 * 1024)

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


@pytest.fixture(autouse=True)
def chainlit_context() -> None:
    pass


@pytest.fixture
def template(tmp_path: Path) -> Path:
    path = tmp_path / "template.ext4"
    with path.open("wb") as f:
        f.truncate(16 * 1024 * 1024)
    mkfs = shutil.which("mkfs.ext4")
    if mkfs is None:
        raise AssertionError("mkfs is not None")
    subprocess.run(  # noqa: S603
        [mkfs, "-F", "-q", "-O", "^has_journal", "-m", "0", str(path)],
        check=True,
    )
    return path


def _profile(**kw: Any) -> SandboxProfile:
    return SandboxStand.profile(**kw)


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
        if "timeout_sec=7" not in text:
            raise AssertionError('"timeout_sec=7" in text')
        if "timeout" not in text.lower():
            raise AssertionError('"timeout" in text.lower()')

    def test_cpu_limit_named(self) -> None:
        text = _explain(_result(exit_code=152), _profile(process_cpu_sec=5))
        if "process_cpu_sec=5" not in text:
            raise AssertionError('"process_cpu_sec=5" in text')
        if "SIGXCPU" not in text:
            raise AssertionError('"SIGXCPU" in text')

    def test_file_size_limit_named(self) -> None:
        result = _result(stderr="bash: line 1: File size limit exceeded")
        text = _explain(result, _profile(process_file_bytes=1024))
        if "process_file_bytes=1024" not in text:
            raise AssertionError('"process_file_bytes=1024" in text')

    def test_open_files_limit_named(self) -> None:
        result = _result(stderr="OSError: [Errno 24] Too many open files: 'x'")
        text = _explain(result, _profile(process_open_files=10))
        if "process_open_files=10" not in text:
            raise AssertionError('"process_open_files=10" in text')

    def test_process_limit_named(self) -> None:
        result = _result(stderr="bash: fork: retry: Resource temporarily unavailable")
        text = _explain(result, _profile(max_processes=10))
        if "max_processes=10" not in text:
            raise AssertionError('"max_processes=10" in text')

    def test_memory_limit_named(self) -> None:
        result = _result(stderr="MemoryError")
        text = _explain(result, _profile(process_memory_bytes=64 * 1024 * 1024))
        if "process_memory_bytes=67108864" not in text:
            raise AssertionError('"process_memory_bytes=67108864" in text')

    def test_thread_local_failure_is_a_memory_limit(self) -> None:
        """glibc падает до main и пишет своё сообщение в нижнем регистре."""
        result = _result(
            exit_code=127,
            stderr="cannot allocate memory for thread-local data: ABORT",
        )

        text = _explain(result, _profile(process_memory_bytes=64 * 1024 * 1024))

        if "process_memory_bytes=67108864" not in text:
            raise AssertionError(f"падение TLS не объяснено лимитом: {text!r}")
        if "RLIMIT_AS" not in text:
            raise AssertionError(f"в объяснении нет RLIMIT_AS: {text!r}")

    def test_full_image_explained(self) -> None:
        result = _result(stderr="dd: writing 'big': No space left on device")
        text = _explain(result, _profile())
        if "workspace image" not in text:
            raise AssertionError('"workspace image" in text')
        if "No space left" not in text:
            raise AssertionError('"No space left" in text')

    def test_network_disabled_explained_with_alternatives(self) -> None:
        result = _result(
            stderr="socket.gaierror: [Errno -3] Temporary failure in name resolution"
        )
        text = _explain(result, _profile(network=False))
        if "network=false" not in text:
            raise AssertionError('"network=false" in text')
        if "not at fault" not in text:
            raise AssertionError('"not at fault" in text')

    def test_network_error_ignored_when_network_enabled(self) -> None:
        result = _result(stderr="Temporary failure in name resolution")
        if _explain(result, _profile(network=True)) != "":
            raise AssertionError('_explain(result, _profile(network=True)) == ""')

    def test_plain_failure_has_no_diagnostic(self) -> None:
        result = _result(stderr="cat: f.txt: No such file or directory")
        if _explain(result, _profile()) != "":
            raise AssertionError('_explain(result, _profile()) == ""')

    def test_success_has_no_diagnostic(self) -> None:
        if _explain(_result(exit_code=0), _profile()) != "":
            raise AssertionError('_explain(_result(exit_code=0), _profile()) == ""')


def _tool(section: str, profile: SandboxProfile):
    """Bash-инструмент на своей зиготе: у теста свои лимиты — своя секция."""
    launchers = ZygoteStand.launchers(
        section, profile, path_vars=lambda: {"user_id": "7", "thread_id": "t1"}
    )
    return build_bash_tool(OUTPUT_LIMITS, launchers)


def _invoke(tool, command: str, stdin: str = "") -> ShellResult:
    body = tool.func
    if body is None:
        raise AssertionError("bash tool has no sync body")

    _content, artifact = body(command=command, stdin=stdin)
    if not isinstance(artifact, ShellResult):
        raise AssertionError("isinstance(artifact, ShellResult)")

    return artifact


@needs_sandbox
class TestDiagnosticAppearsLive:
    """Лимит реально превышается — сообщение обязано быть в результате."""

    def teardown_method(self) -> None:
        ZygoteStand.stop()

    def test_open_files(self) -> None:
        code = (
            "held = []\n"
            "for i in range(200):\n"
            "    held.append(open('/tmp/probe-%d' % i, 'w'))\n"
        )
        tool = _tool("dg-files", _profile(process_open_files=10))
        payload = _invoke(tool, "python3 -", stdin=code)
        if payload.exit_code == 0:
            raise AssertionError("payload.exit_code != 0")
        if "process_open_files=10" not in payload.diagnostic:
            raise AssertionError('"process_open_files=10" in payload.diagnostic')

    def test_processes(self) -> None:
        command = "for i in $(seq 1 300); do sleep 5 & done; wait"
        tool = _tool("dg-procs", _profile(max_processes=64, timeout_sec=20))
        payload = _invoke(tool, command)
        if "max_processes=64" not in payload.diagnostic:
            raise AssertionError(f"нет упоминания лимита: {payload.diagnostic!r}")

    def test_file_size(self) -> None:
        tool = _tool("dg-fsize", _profile(process_file_bytes=1024 * 1024))
        payload = _invoke(tool, "dd if=/dev/zero of=/tmp/big bs=64k count=64")
        if payload.exit_code == 0:
            raise AssertionError("payload.exit_code != 0")
        if "process_file_bytes=1048576" not in payload.diagnostic:
            raise AssertionError('"process_file_bytes=1048576" in payload.diagnostic')

    def test_memory(self) -> None:
        code = "x = bytearray(400 * 1024 * 1024)\n"
        tool = _tool("dg-mem", _profile(process_memory_bytes=64 * 1024 * 1024))
        payload = _invoke(tool, "python3 -", stdin=code)
        if payload.exit_code == 0:
            raise AssertionError("payload.exit_code != 0")
        if "process_memory_bytes=67108864" not in payload.diagnostic:
            raise AssertionError(
                '"process_memory_bytes=67108864" in payload.diagnostic'
            )

    def test_timeout(self) -> None:
        tool = _tool("dg-timeout", _profile(timeout_sec=1))
        payload = _invoke(tool, "sleep 10")
        if payload.timed_out is not True:
            raise AssertionError("payload.timed_out is True")
        if "timeout_sec=1" not in payload.diagnostic:
            raise AssertionError('"timeout_sec=1" in payload.diagnostic')

    def test_network_disabled_explained(self) -> None:
        code = "import socket\nsocket.getaddrinfo('example.com', 443)\n"
        tool = _tool("dg-net", _profile(network=False))
        payload = _invoke(tool, "python3 -", stdin=code)
        if payload.exit_code == 0:
            raise AssertionError("payload.exit_code != 0")
        if "network=false" not in payload.diagnostic:
            raise AssertionError('"network=false" in payload.diagnostic')
        if "not at fault" not in payload.diagnostic:
            raise AssertionError('"not at fault" in payload.diagnostic')

    def test_successful_command_has_empty_diagnostic(self) -> None:
        payload = _invoke(_tool("dg-ok", _profile()), "echo ok")
        if payload.exit_code != 0:
            raise AssertionError("payload.exit_code == 0")
        if payload.diagnostic != "":
            raise AssertionError('payload.diagnostic == ""')
