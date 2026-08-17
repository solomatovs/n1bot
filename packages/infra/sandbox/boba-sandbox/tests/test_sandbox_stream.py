"""Тап живого вывода: окно наполняется по ходу работы процесса в песочнице."""

from __future__ import annotations

import os
import shutil
from typing import Any

import pytest

from boba.sandbox import SandboxCaller, SandboxProfile
from boba.toolkit.stream import ToolStreamBuffer, ToolStreamTap


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
    "ro_binds": ("/usr", "/bin", "/sbin", "/lib", "/lib64"),
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
    "tmpfs": ("/tmp:64M",),  # noqa: S108
    "network": False,
    "env_set": {"PATH": "/usr/bin:/bin", "HOME": "/tmp"},  # noqa: S108
    "timeout_sec": 30,
    "max_memory_bytes": 512 * 1024 * 1024,
    "max_cpu_sec": 30,
    "max_file_size_bytes": 64 * 1024 * 1024,
    "max_open_files": 1024,
    "max_processes": 256,
    "cgroup_base": "",
    "oom_score_adj": 0,
    "cwd": "/tmp",  # noqa: S108
}


@pytest.fixture(autouse=True)
def clean_tap() -> Any:
    yield
    ToolStreamTap.set(None)


def _window(window_bytes: int = 64 * 1024) -> ToolStreamBuffer:
    def wake() -> None:
        pass

    return ToolStreamBuffer(window_bytes, wake)


@pytest.mark.skipif(shutil.which("bwrap") is None, reason="bwrap не установлен")
@pytest.mark.skipif(os.geteuid() == 0, reason="под root userns ведёт себя иначе")
class TestCallTextTap:
    """Текстовый запуск: окно видит сырые stdout и stderr, результат полон."""

    @staticmethod
    def _caller(**profile_kw: Any) -> SandboxCaller:
        profile = SandboxProfile.model_validate({**_PROFILE_BASE, **profile_kw})
        return SandboxCaller("bash", profile, dict)

    def test_window_gets_both_streams_and_result_is_kept(self) -> None:
        buffer = _window()
        ToolStreamTap.set(buffer)

        outcome = self._caller().call_text("echo привет; echo беда >&2", stdin="")

        window = buffer.snapshot()
        if "привет" not in window.text:
            raise AssertionError('"привет" in window.text')
        if "беда" not in window.text:
            raise AssertionError('"беда" in window.text')
        if "привет" not in outcome.result.stdout:
            raise AssertionError('"привет" in outcome.result.stdout')
        if "беда" not in outcome.result.stderr:
            raise AssertionError('"беда" in outcome.result.stderr')

    def test_without_tap_nothing_changes(self) -> None:
        ToolStreamTap.set(None)

        outcome = self._caller().call_text("echo одинокий", stdin="")

        if "одинокий" not in outcome.result.stdout:
            raise AssertionError('"одинокий" in outcome.result.stdout')

    def test_window_stays_bounded_on_huge_output(self) -> None:
        """Мегабайты вывода не оседают в окне: оно держит только хвост.

        Результат отдаётся целиком, а окно продолжает ехать до конца
        процесса — в нём последние строки.
        """
        window_bytes = 64 * 1024
        buffer = _window(window_bytes)
        ToolStreamTap.set(buffer)

        # ~1.6 МБ: 200000 строк по 8 байт
        outcome = self._caller().call_text("seq -w 1 200000", stdin="")

        window = buffer.snapshot()
        if len(window.text.encode()) > window_bytes:
            raise AssertionError("len(window.text.encode()) <= window_bytes")
        if window.dropped_bytes <= 1_000_000:
            raise AssertionError("window.dropped_bytes > 1_000_000")
        if "200000" not in window.text:
            raise AssertionError('"200000" in window.text')
        if "\n000002\n" in window.text:
            raise AssertionError('"\\n000002\\n" not in window.text')
        if not (outcome.result.stdout.startswith("000001\n")):
            raise AssertionError('outcome.result.stdout.startswith("000001\\n")')

    def test_window_fills_while_the_process_runs(self) -> None:
        """Пробуждения приходят по ходу процесса, а не одним махом в конце."""
        sizes: list[int] = []
        holder: list[ToolStreamBuffer] = []

        def wake() -> None:
            sizes.append(len(holder[0].snapshot().text))

        buffer = ToolStreamBuffer(64 * 1024, wake)
        holder.append(buffer)
        ToolStreamTap.set(buffer)

        self._caller().call_text("echo старт; sleep 0.3; echo финиш", stdin="")

        if len(sizes) < 2:
            raise AssertionError("len(sizes) >= 2")
        if sizes != sorted(sizes):
            raise AssertionError("sizes == sorted(sizes)")
        if sizes[0] >= sizes[-1]:
            raise AssertionError("sizes[0] < sizes[-1]")
