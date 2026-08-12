"""Превышение лимита должно объясняться словами, а не кодом ядра."""

# ruff: noqa: S108

from __future__ import annotations

import os
import shutil
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pydantic
import pytest
from journal_stand import JournalStand
from pydantic import BaseModel, JsonValue

from boba.sandbox.diagnostics import FailureFacts, SandboxDiagnostics
from boba.sandbox.profile import SandboxProfile
from boba.sandbox.workflow import StageDef, StageRegistry, WorkflowRunner
from boba.toolkit.workflow import (
    EmptyTrailer,
    StageContract,
    WorkflowError,
    WorkflowSpec,
)

REPO = Path(__file__).resolve().parents[5]
TOOLKIT_SRC = REPO / "packages" / "core" / "boba-toolkit" / "src"
SITE_PACKAGES = Path(pydantic.__file__).resolve().parents[1]

HOST_RO_BINDS: tuple[str, ...] = ("/usr", "/bin", "/sbin", "/lib", "/lib64")

PAYLOAD_ENTRY: tuple[str, ...] = ("python3.11", "/opt/payload/main.py")

needs_sandbox = pytest.mark.skipif(
    shutil.which("bwrap") is None, reason="bwrap не установлен"
)
needs_userns = pytest.mark.skipif(
    os.geteuid() == 0, reason="под root userns ведёт себя иначе"
)

def _bin_dirs() -> list[str]:
    """В тестах каталоги берутся из PATH; в проде их задаёт конфиг."""
    dirs: list[str] = []

    for entry in os.environ.get("PATH", "").split(os.pathsep):
        if not entry.startswith("/"):
            continue

        dirs.append(entry)

    return dirs


_PROFILE_BASE: dict[str, Any] = {
    "rootfs": "",
    "ro_binds": (),
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
    "env_set": {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "PYTHONPATH": "/opt/src:/opt/site",
        "HOME": "/tmp",
        "LANG": "C.UTF-8",
    },
    "timeout_sec": 30,
    "max_memory_bytes": 512 * 1024 * 1024,
    "max_cpu_sec": 30,
    "max_file_size_bytes": 64 * 1024 * 1024,
    "max_open_files": 1024,
    "max_processes": 256,
    "max_output_bytes": 4 * 1024 * 1024,
    "cgroup_base": "",
    "oom_score_adj": 0,
    "cwd": "/tmp",
}


def _profile(**kw: object) -> SandboxProfile:
    return SandboxProfile.model_validate({**_PROFILE_BASE, **kw})


def _facts(**kw: object) -> FailureFacts:
    fields: dict[str, object] = {
        "exit_code": 1,
        "timed_out": False,
        "stderr_tail": "",
    }
    fields.update(kw)
    return FailureFacts.model_validate(fields)


def _explain(facts: FailureFacts, profile: SandboxProfile) -> str:
    return SandboxDiagnostics.explain(facts, profile)


class TestDiagnosticText:
    """Текст обязан назвать лимит, его значение и что делать дальше."""

    def test_timeout_names_limit(self) -> None:
        text = _explain(_facts(timed_out=True), _profile(timeout_sec=7))
        assert "timeout_sec=7" in text
        assert "timeout" in text.lower()

    def test_cpu_limit_named(self) -> None:
        text = _explain(_facts(exit_code=152), _profile(max_cpu_sec=5))
        assert "max_cpu_sec=5" in text
        assert "SIGXCPU" in text

    def test_file_size_limit_named(self) -> None:
        facts = _facts(stderr_tail="bash: line 1: File size limit exceeded")
        text = _explain(facts, _profile(max_file_size_bytes=1024))
        assert "max_file_size_bytes=1024" in text

    def test_open_files_limit_named(self) -> None:
        facts = _facts(stderr_tail="OSError: [Errno 24] Too many open files: 'x'")
        text = _explain(facts, _profile(max_open_files=10))
        assert "max_open_files=10" in text

    def test_process_limit_named(self) -> None:
        facts = _facts(
            stderr_tail="bash: fork: retry: Resource temporarily unavailable"
        )
        text = _explain(facts, _profile(max_processes=10))
        assert "max_processes=10" in text

    def test_memory_limit_named(self) -> None:
        facts = _facts(stderr_tail="MemoryError")
        text = _explain(facts, _profile(max_memory_bytes=64 * 1024 * 1024))
        assert "max_memory_bytes=67108864" in text

    def test_full_image_explained(self) -> None:
        facts = _facts(stderr_tail="dd: writing 'big': No space left on device")
        text = _explain(facts, _profile())
        assert "workspace image" in text
        assert "No space left" in text

    def test_network_disabled_explained_with_alternatives(self) -> None:
        facts = _facts(
            stderr_tail=(
                "socket.gaierror: [Errno -3] Temporary failure in name resolution"
            )
        )
        text = _explain(facts, _profile(network=False))
        assert "network=false" in text
        assert "not at fault" in text

    def test_network_error_ignored_when_network_enabled(self) -> None:
        facts = _facts(stderr_tail="Temporary failure in name resolution")
        assert _explain(facts, _profile(network=True)) == ""

    def test_plain_failure_has_no_diagnostic(self) -> None:
        facts = _facts(stderr_tail="cat: f.txt: No such file or directory")
        assert _explain(facts, _profile()) == ""

    def test_success_has_no_diagnostic(self) -> None:
        assert _explain(_facts(exit_code=0), _profile()) == ""


_PAYLOAD_PROLOGUE = """
from pydantic import BaseModel

from boba.toolkit.payload import PayloadChannels, PayloadLogging


class Request(BaseModel):
    pass


class Trailer(BaseModel):
    pass


PayloadLogging.setup()
channels = PayloadChannels.open()
channels.args(Request)
"""

_PAYLOAD_EPILOGUE = """
channels.write_result(Trailer())
raise SystemExit(int(channels.exit_code()))
"""


_OPEN_FILES_BODY = """
held = []
for i in range(200):
    held.append(open('/tmp/probe-%d' % i, 'w'))
"""

_PROCESSES_BODY = """
import subprocess

procs = []
for _ in range(50):
    procs.append(subprocess.Popen(["sleep", "5"]))
for proc in procs:
    proc.wait()
"""

_FILE_SIZE_BODY = """
from pathlib import Path

Path("/tmp/big").write_bytes(b"x" * (8 * 1024 * 1024))
"""

_MEMORY_BODY = """
hog = bytearray(400 * 1024 * 1024)
"""

_SLEEP_BODY = """
import time

time.sleep(30)
"""

_NETWORK_BODY = """
import socket

socket.getaddrinfo("example.com", 443)
"""


def _identity_args(args: Mapping[str, JsonValue], /) -> Mapping[str, JsonValue]:
    return dict(args)


def _allow_all(tool: str, /) -> bool:
    return True


def _write_payload(root: Path, body: str) -> Path:
    payload_dir = root / "payloads" / "probe"
    payload_dir.mkdir(parents=True, exist_ok=True)
    script = _PAYLOAD_PROLOGUE + body + _PAYLOAD_EPILOGUE
    (payload_dir / "main.py").write_text(script, encoding="utf-8")

    return payload_dir


class _NoArgs(BaseModel):
    """Запрос пробного узла: полей нет."""


def _stage_profile(payload_dir: Path, **kw: Any) -> SandboxProfile:
    fields = dict(_PROFILE_BASE)

    binds = list(HOST_RO_BINDS)
    binds.append(f"{TOOLKIT_SRC}:/opt/src")
    binds.append(f"{SITE_PACKAGES}:/opt/site")
    binds.append(f"{payload_dir}:/opt/payload")
    fields["ro_binds"] = tuple(binds)

    fields.update(kw)

    return SandboxProfile.model_validate(fields)


def _run_probe(root: Path, body: str, **profile_kw: Any) -> None:
    payload_dir = _write_payload(root, body)

    definition = StageDef(
        contract=StageContract(out=None, result=EmptyTrailer),
        profile=_stage_profile(payload_dir, **profile_kw),
        entry=PAYLOAD_ENTRY,
        request=_NoArgs,
        enrich=_identity_args,
    )
    runner = WorkflowRunner(
        StageRegistry({"probe": definition}),
        _allow_all,
        dict,
        JournalStand.journal(),
    )

    spec = WorkflowSpec.model_validate(
        {"nodes": [{"id": "probe", "tool": "probe", "args": {}}]}
    )
    runner.run(spec, JournalStand.context())


@needs_sandbox
@needs_userns
class TestDiagnosticAppearsLive:
    """Лимит реально превышается — объяснение обязано попасть в текст ошибки."""

    def test_open_files(self, tmp_path: Path) -> None:
        with pytest.raises(WorkflowError, match="max_open_files=10"):
            _run_probe(tmp_path, _OPEN_FILES_BODY, max_open_files=10)

    def test_processes(self, tmp_path: Path) -> None:
        with pytest.raises(WorkflowError, match="max_processes=10"):
            _run_probe(
                tmp_path, _PROCESSES_BODY, max_processes=10, timeout_sec=20
            )

    def test_file_size(self, tmp_path: Path) -> None:
        with pytest.raises(WorkflowError, match="max_file_size_bytes=1048576"):
            _run_probe(tmp_path, _FILE_SIZE_BODY, max_file_size_bytes=1024 * 1024)

    def test_memory(self, tmp_path: Path) -> None:
        with pytest.raises(WorkflowError, match="max_memory_bytes=134217728"):
            _run_probe(tmp_path, _MEMORY_BODY, max_memory_bytes=128 * 1024 * 1024)

    def test_timeout(self, tmp_path: Path) -> None:
        with pytest.raises(WorkflowError, match="timed out") as failure:
            _run_probe(tmp_path, _SLEEP_BODY, timeout_sec=1)

        assert "timeout_sec=1" in str(failure.value)

    def test_network_disabled_explained(self, tmp_path: Path) -> None:
        with pytest.raises(WorkflowError, match="network=false") as failure:
            _run_probe(tmp_path, _NETWORK_BODY, network=False)

        assert "not at fault" in str(failure.value)
