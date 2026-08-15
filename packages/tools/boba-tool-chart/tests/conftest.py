"""Профиль песочницы для тестов: зависимости из собранного site, код из src.

Код инструментов монтируется из src, иначе тест проверял бы прошлую сборку.
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
    "core/boba-indexing",
    "core/boba-toolkit",
    "infra/sandbox/boba-sandbox",
    "infra/db/boba-db-postgres",
    "infra/db/boba-db-pgvector",
    "infra/krb/boba-krb",
    "tools/boba-tool-shell",
    "tools/boba-tool-chart",
    "tools/boba-tool-web",
    "tools/boba-tool-doc",
    "tools/boba-tool-postgres",
    "tools/boba-tool-knowledge",
)
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
        """Свой код: интерпретатор и site-packages стоят на штатных местах."""
        parts = []
        for name in SRC_PACKAGES:
            parts.append(f"/usr/src/{name}/src")

        return ":".join(parts)


def sandbox_profile(docs_dir: Path | None = None, **kw: Any) -> dict[str, Any]:
    """Профиль запуска payload'а — тот же по смыслу, что в конфиге приложения."""
    profile: dict[str, Any] = {
        "rootfs": str(ROOTFS),
        "ro_binds": tuple(SandboxLayout.ro_binds(docs_dir)),
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
        "tmpfs": ("/tmp:256M",),  # noqa: S108
        "network": False,
        "env_set": {
            "PYTHONPATH": SandboxLayout.python_path(),
            "HOME": "/tmp",  # noqa: S108
            "LANG": "C.UTF-8",
        },
        "timeout_sec": 120,
        "max_memory_bytes": ADDRESS_SPACE,
        "max_cpu_sec": 120,
        "max_file_size_bytes": 64 * 1024 * 1024,
        "max_open_files": 1024,
        "max_processes": 256,
        "cgroup_base": "",
        "oom_score_adj": 0,
        "cwd": "/tmp",  # noqa: S108
    }
    profile.update(kw)
    return profile


@pytest.fixture(autouse=True)
def chainlit_context() -> None:
    pass
