"""Диагностика лимитов песочницы: по коду возврата умершего вызова."""

from __future__ import annotations

import os
import shutil
from typing import Any

import pytest

from boba.sandbox.diagnostics import KilledBy, SandboxDiagnostics
from boba.sandbox.profile import SandboxProfile
from boba.sandbox.zygote import ZygoteToolCaller
from boba.stand.shell import ShellRun
from boba.stand.zygote import SandboxStand, ZygoteStand
from boba.tool.shell.tools import BashToolConfig
from boba.toolkit.launcher import LauncherError, RunResult
from boba.toolkit.result import ShellResult
from boba.workspace.launcher import FUSE_DEVICE

OUTPUT_LIMITS = BashToolConfig(max_output_bytes=4 * 1024 * 1024, timeout_sec=60.0)

needs_sandbox = pytest.mark.skipif(
    shutil.which("bwrap") is None
    or shutil.which("fuse2fs") is None
    or shutil.which("mkfs.ext4") is None
    or not os.path.exists(FUSE_DEVICE),
    reason="нужны bwrap, fuse2fs, mkfs.ext4 и /dev/fuse",
)


@pytest.fixture(autouse=True)
def chainlit_context() -> None:
    pass


def _profile(**kw: Any) -> SandboxProfile:
    return SandboxStand.profile(**kw)


def _result(exit_code: int = 1, *, timed_out: bool = False) -> RunResult:
    return RunResult(
        exit_code=exit_code,
        stdout="",
        stderr="",
        duration_ms=10,
        timed_out=timed_out,
    )


def _explain(result: RunResult, profile: SandboxProfile) -> str:
    return SandboxDiagnostics.explain(result, profile)


class TestDiagnosticText:
    """Текст обязан назвать лимит, его значение и что делать дальше."""

    def test_timeout_names_limit(self) -> None:
        message = _explain(_result(timed_out=True), _profile(timeout_sec=7))
        if "timeout_sec=7" not in message:
            raise AssertionError('"timeout_sec=7" in message')
        if "smaller steps" not in message:
            raise AssertionError('"smaller steps" in message')

    def test_cpu_limit_named(self) -> None:
        message = _explain(_result(KilledBy.SIGXCPU), _profile(process_cpu_sec=3))
        if "process_cpu_sec=3" not in message:
            raise AssertionError('"process_cpu_sec=3" in message')

    def test_file_size_limit_named(self) -> None:
        message = _explain(
            _result(KilledBy.SIGXFSZ),
            _profile(process_file_bytes=1024),
        )
        if "process_file_bytes=1024" not in message:
            raise AssertionError('"process_file_bytes=1024" in message')

    def test_memory_limit_named(self) -> None:
        message = _explain(
            _result(KilledBy.SIGKILL),
            _profile(process_memory_bytes=1 << 20),
        )
        if "process_memory_bytes=1048576" not in message:
            raise AssertionError('"process_memory_bytes=1048576" in message')

    def test_plain_failure_has_no_diagnostic(self) -> None:
        """Обычный ненулевой код — не лимит: команда сама объяснила себя в stderr."""
        if _explain(_result(exit_code=1), _profile()) != "":
            raise AssertionError('_explain(_result(exit_code=1), _profile()) == ""')

    def test_success_has_no_diagnostic(self) -> None:
        if _explain(_result(exit_code=0), _profile()) != "":
            raise AssertionError('_explain(_result(exit_code=0), _profile()) == ""')


def _caller(section: str, profile: SandboxProfile) -> ZygoteToolCaller:
    """Зигота теста: у теста свои лимиты — своя секция."""
    return ZygoteStand.caller(
        section, profile, path_vars=lambda: {"user_id": "7", "thread_id": "t1"}
    )


def _invoke(
    caller: ZygoteToolCaller, command: str, cfg: BashToolConfig = OUTPUT_LIMITS
) -> ShellResult:
    return ShellRun.call_text(caller, command, cfg=cfg)


def _python(code: str) -> str:
    """Команда python со скриптом в heredoc: stdin у bash-тула закрыт."""
    return f"python3 - <<'PY'\n{code}PY\n"


@needs_sandbox
class TestDiagnosticAppearsLive:
    """Лимит реально превышается: сигнал ядра доходит до обвязки запуска и
    объясняется профилем; ошибка команды остаётся её кодом и stderr."""

    def teardown_method(self) -> None:
        ZygoteStand.stop()

    def test_file_size_kills_the_call_with_the_limit_named(self) -> None:
        """SIGXFSZ убивает команду, тело умирает тем же сигналом — вызов без
        конверта, обвязка называет лимит по коду возврата."""
        caller = _caller("dg-fsize", _profile(process_file_bytes=1024 * 1024))

        with pytest.raises(LauncherError, match="process_file_bytes=1048576"):
            _invoke(caller, "dd if=/dev/zero of=/tmp/big bs=64k count=64")

    def test_open_files_is_a_plain_failure(self) -> None:
        """EMFILE — ошибка команды, не сигнал: код и stderr как есть."""
        code = (
            "held = []\n"
            "for i in range(200):\n"
            "    held.append(open('/tmp/probe-%d' % i, 'w'))\n"
        )
        caller = _caller("dg-files", _profile(process_open_files=40))
        payload = _invoke(caller, _python(code))
        if payload.exit_code == 0:
            raise AssertionError("payload.exit_code != 0")
        if "Too many open files" not in payload.stderr:
            raise AssertionError('"Too many open files" in payload.stderr')

    def test_address_space_is_a_plain_failure(self) -> None:
        """RLIMIT_AS даёт MemoryError, а не сигнал: команда отчитывается сама."""
        code = "x = bytearray(400 * 1024 * 1024)\n"
        caller = _caller("dg-mem", _profile(process_memory_bytes=64 * 1024 * 1024))
        payload = _invoke(caller, _python(code))
        if payload.exit_code == 0:
            raise AssertionError("payload.exit_code != 0")
        if "MemoryError" not in payload.stderr:
            raise AssertionError('"MemoryError" in payload.stderr')

    def test_profile_timeout_kills_the_call(self) -> None:
        caller = _caller("dg-timeout", _profile(timeout_sec=1))

        with pytest.raises(LauncherError, match="timeout_sec=1"):
            _invoke(caller, "sleep 10")

    def test_command_timeout_is_reported_by_the_tool(self) -> None:
        """Таймаут самой команды из [tool.bash]: результат с timed_out."""
        caller = _caller("dg-cmd-timeout", _profile(timeout_sec=30))
        limits = BashToolConfig(max_output_bytes=4096, timeout_sec=1.0)
        payload = _invoke(caller, "echo before; sleep 10", cfg=limits)
        if payload.timed_out is not True:
            raise AssertionError("payload.timed_out is True")
        if payload.ok:
            raise AssertionError("not payload.ok")
        if "before" not in payload.stdout:
            raise AssertionError('"before" in payload.stdout')

    def test_network_disabled_is_a_plain_failure(self) -> None:
        code = "import socket\nsocket.getaddrinfo('example.com', 443)\n"
        caller = _caller("dg-net", _profile(network=False))
        payload = _invoke(caller, _python(code))
        if payload.exit_code == 0:
            raise AssertionError("payload.exit_code != 0")

    def test_successful_command(self) -> None:
        payload = _invoke(_caller("dg-ok", _profile()), "echo ok")
        if payload.exit_code != 0:
            raise AssertionError("payload.exit_code == 0")
        if payload.stdout.strip() != "ok":
            raise AssertionError('payload.stdout.strip() == "ok"')
