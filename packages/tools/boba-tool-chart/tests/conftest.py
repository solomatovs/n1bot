"""Профиль песочницы для тестов: зависимости из собранного site, код из src.

Код инструментов монтируется из src, иначе тест проверял бы прошлую сборку.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

import pytest

from boba.sandbox import SandboxProfile

REPO = Path(__file__).resolve().parents[4]
SANDBOX = REPO / "build" / "src" / "sandbox"
ROOTFS = SANDBOX / "rootfs"
ROOTFS_IMAGE = SANDBOX / "rootfs.ext4"

"""Код пакетов монтируется одним каталогом: точку /usr/src несёт rootfs."""

ADDRESS_SPACE = 16 * 1024 * 1024 * 1024
"""RLIMIT_AS профиля парсера: pdfium резервирует ~2.3G независимо от документа."""

needs_sandbox = pytest.mark.skipif(
    shutil.which("bwrap") is None or not (ROOTFS / "bin" / "sh").exists(),
    reason="нет bwrap или артефактов песочницы (собрать: make deps)",
)
needs_userns = pytest.mark.skipif(
    os.geteuid() == 0, reason="под root user namespace ведёт себя иначе"
)


def _bin_dirs() -> list[str]:
    """В тестах каталоги берутся из PATH; в проде их задаёт конфиг."""
    dirs: list[str] = []

    for entry in os.environ.get("PATH", "").split(os.pathsep):
        if not entry.startswith("/"):
            continue

        dirs.append(entry)

    return dirs


SRC_PACKAGES = (
    "core/boba-cancellation",
    "core/boba-toolkit",
    "tools/boba-tool-chart",
)
"""Пакеты, чей код нужен телу инструмента внутри песочницы."""


class SandboxLayout:
    """Монтирования и env: код пакетов кладётся поверх собранного site."""

    @staticmethod
    def ro_binds(docs_dir: Path | None) -> list[str]:
        site_packages = "/usr/local/lib/python3.11/site-packages"
        binds = [
            f"{SANDBOX / 'third' / 'python'}:/usr/local",
            f"{SANDBOX / 'site'}:{site_packages}",
            f"{SANDBOX / 'third' / 'fastembed'}:/var/cache/fastembed",
            f"{SANDBOX / 'third' / 'tessdata'}:/usr/share/tessdata",
        ]
        binds.append(f"{REPO / 'packages'}:/usr/src")
        if docs_dir is not None:
            binds.append(f"{docs_dir}:/workspace")
        return binds

    @staticmethod
    def python_path() -> str:
        """Каталоги src пакетов внутри песочницы: их же перечисляет .pth."""
        parts: list[str] = []
        for name in SRC_PACKAGES:
            parts.append(f"/usr/src/{name}/src")

        return ":".join(parts)


def _place(raw: dict[str, Any], name: str, value: Any) -> None:
    """Плоское поле профиля в свою группу: группа находится по модели."""
    if name in SandboxProfile.GROUPS:
        group = dict(raw.get(name, {}))
        if isinstance(value, dict):
            group.update(value)
            raw[name] = group
            return

        raw[name] = value
        return

    for group_name in SandboxProfile.GROUPS:
        model = SandboxProfile.model_fields[group_name].annotation
        if name not in getattr(model, "model_fields", {}):
            continue

        group = dict(raw.get(group_name, {}))
        group[name] = value
        raw[group_name] = group
        return

    msg = f"профиль: поле {name!r} не принадлежит ни одной группе"
    raise KeyError(msg)


def _merged(base: dict[str, Any], flat: dict[str, Any]) -> dict[str, Any]:
    """Копия базы профиля с наложенными плоскими полями."""
    raw: dict[str, Any] = {}
    for name, value in base.items():
        if isinstance(value, dict):
            raw[name] = dict(value)
            continue

        raw[name] = value

    for name, value in flat.items():
        _place(raw, name, value)

    return raw


def sandbox_profile(docs_dir: Path | None = None, **kw: Any) -> dict[str, Any]:
    """Профиль запуска payload'а — тот же по смыслу, что в конфиге приложения."""
    profile: dict[str, Any] = {
        "host": {
            "mounting": {
                "mount_wait_sec": 10.0,
                "mount_poll_sec": 0.05,
                "shutdown_wait_sec": 5.0,
                "lock_wait_sec": 10.0,
                "copy_chunk_bytes": 1 << 20,
            },
            "binaries": {"dirs": _bin_dirs()},
            "stderr_tail_bytes": 4096,
            "channel_limit_bytes": 67108864,
            "fail_tail_chars": 2000,
            "kill_grace_sec": 5,
            "cgroup_base": "",
        },
        "rootfs": str(ROOTFS_IMAGE),
        "mounts": {
            "ro": tuple(SandboxLayout.ro_binds(docs_dir)),
            "rw": (),
            "tmp": "256M",
        },
        "isolation": {
            "network": False,
            "env": {
                "PATH": "/usr/local/bin:/usr/bin:/bin",
                "PYTHONPATH": SandboxLayout.python_path(),
                "HOME": "/tmp",  # noqa: S108
                "LANG": "C.UTF-8",
            },
            "reap_poll_sec": 0.05,
        },
        "limits": {
            "timeout_sec": 120,
            "process_memory_bytes": ADDRESS_SPACE,
            "process_cpu_sec": 120,
            "process_file_bytes": 64 * 1024 * 1024,
            "process_open_files": 1024,
            "process_oom_score_adj": 0,
        },
        "run": {
            "shell": "/bin/bash",
            "cwd": "/tmp",  # noqa: S108
        },
    }
    return _merged(profile, kw)


@pytest.fixture(autouse=True)
def chainlit_context() -> None:
    pass
