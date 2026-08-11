"""Профиль песочницы для тестов узла bash: зависимости из site, код из src.

Код пакетов монтируется из репозитория, иначе тест проверял бы прошлую сборку.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[4]
SANDBOX = REPO / "build" / "src" / "sandbox"
ROOTFS = SANDBOX / "rootfs"

SRC_PACKAGES = (
    "core/boba-cancellation",
    "core/boba-toolkit",
    "infra/sandbox/boba-sandbox",
    "tools/boba-tool-shell",
)

needs_sandbox = pytest.mark.skipif(
    shutil.which("bwrap") is None or not (ROOTFS / "bin" / "sh").exists(),
    reason="нет bwrap или артефактов песочницы (собрать: make deps)",
)
needs_userns = pytest.mark.skipif(
    os.geteuid() == 0, reason="под root user namespace ведёт себя иначе"
)


class SandboxLayout:
    """Монтирования и env: код пакетов кладётся поверх собранного site."""

    @staticmethod
    def ro_binds() -> list[str]:
        return [
            f"{SANDBOX / 'third' / 'python'}:/opt/python",
            f"{SANDBOX / 'site'}:/opt/site",
            f"{REPO / 'packages'}:/opt/src",
        ]

    @staticmethod
    def python_path() -> str:
        parts: list[str] = []
        for name in SRC_PACKAGES:
            parts.append(f"/opt/src/{name}/src")
        parts.append("/opt/site")
        return ":".join(parts)


def sandbox_profile(**kw: Any) -> dict[str, Any]:
    """Профиль запуска узла — тот же по смыслу, что в конфиге приложения."""
    profile: dict[str, Any] = {
        "rootfs": str(ROOTFS),
        "ro_binds": tuple(SandboxLayout.ro_binds()),
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
        "tmpfs": ("/tmp:256M",),  # noqa: S108
        "network": False,
        "env_set": {
            "PATH": "/opt/python/bin:/usr/local/bin:/usr/bin:/bin",
            "PYTHONHOME": "/opt/python",
            "PYTHONPATH": SandboxLayout.python_path(),
            "LD_LIBRARY_PATH": "/opt/python/lib",
            "HOME": "/tmp",  # noqa: S108
            "LANG": "C.UTF-8",
        },
        "timeout_sec": 60,
        "max_memory_bytes": 2 * 1024 * 1024 * 1024,
        "max_cpu_sec": 60,
        "max_file_size_bytes": 64 * 1024 * 1024,
        "max_open_files": 1024,
        "max_processes": 256,
        "max_output_bytes": 1 << 20,
        "cgroup_base": "",
        "oom_score_adj": 0,
        "cwd": "/tmp",  # noqa: S108
    }
    profile.update(kw)
    return profile
