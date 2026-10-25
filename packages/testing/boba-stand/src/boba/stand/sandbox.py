"""Профиль песочницы для тестов инструментов: артефакты сборки плюс код репо.

Зависимости — из собранного site, код инструментов — из src: иначе тест проверял бы
прошлую сборку, а не то, что сейчас в репозитории.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

import pytest
from omegaconf import DictConfig, OmegaConf

from boba.runtime.launchers import PluginLaunch, ZygoteLaunchers
from boba.sandbox import SandboxProfile
from boba.sandbox.program import PluginEntry
from boba.stand.zygote import ROOTFS_IMAGE, RUNTIME, SandboxCgroup

REPO = Path(__file__).resolve().parents[6]
"""Код пакетов монтируется одним каталогом: точку /usr/src несёт rootfs."""

ADDRESS_SPACE = 16 * 1024 * 1024 * 1024
"""RLIMIT_AS профиля парсера: pdfium резервирует ~2.3G независимо от документа."""

needs_sandbox = pytest.mark.failif(
    shutil.which("bwrap") is None  # noqa: TID251 — стенд ищет по PATH сознательно
    or not ROOTFS_IMAGE.exists(),
    reason=(
        "нет bwrap или образов плагинов "
        "(собрать: make -C build fetch plugin-rootfs-all)"
    ),
)
needs_userns = pytest.mark.failif(
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


def _src_packages() -> tuple[str, ...]:
    """Все пакеты репозитория с src-layout: бинд packages:/usr/src перекрывает
    запечённый код образа, поэтому sys.path обязан покрывать репо целиком."""
    names: list[str] = []
    for src in sorted((REPO / "packages").glob("*/*/src")) + sorted(
        (REPO / "packages").glob("*/*/*/src")
    ):
        names.append(str(src.relative_to(REPO / "packages").parent))

    return tuple(names)


SRC_PACKAGES = _src_packages()
"""Пакеты, чей код нужен телу инструмента внутри песочницы."""


class SandboxLayout:
    """Монтирования и env: код пакетов кладётся поверх собранного site.

    Образы весов моделей плагин объявляет манифестом (data): рантайм
    монтирует их ext4-образами данных, как в бою; корень несёт только пустые
    точки."""

    @staticmethod
    def ro_binds(docs_dir: Path | None) -> list[str]:
        """Код репозитория поверх кода образа плюс каталог документов теста."""
        binds = [f"{REPO / 'packages'}:/usr/src"]

        if docs_dir is not None:
            binds.append(f"{docs_dir}:/workspace")

        return binds

    @staticmethod
    def images(plugin_id: str) -> list[str]:
        """Образы весов моделей плагина по его объявлению: собирает make
        models-image."""
        plugin = PluginEntry().program(plugin_id).plugin

        images: list[str] = []
        for image in plugin.data:
            images.append(f"{RUNTIME.model_image(image.name)}:{image.target}")

        return images

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

    groups = list(SandboxProfile.GROUPS)
    msg = (
        f"sandbox profile: flat field {name!r} belongs to none of the "
        f"profile groups {groups}"
    )
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


def sandbox_profile(
    plugin_id: str, docs_dir: Path | None = None, **kw: Any
) -> dict[str, Any]:
    """Профиль запуска тела плагина — тот же по смыслу, что в приложении."""
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
            "cgroup_base": SandboxCgroup().base,
        },
        "rootfs": str(RUNTIME.plugin_rootfs(plugin_id)),
        "mounts": {
            "ro": tuple(SandboxLayout.ro_binds(docs_dir)),
            "images": tuple(SandboxLayout.images(plugin_id)),
            "rw": (),
            "tmp": "256M",
        },
        "isolation": {
            "network": False,
            "env": {
                "PATH": "/usr/local/bin:/usr/bin:/bin",
                "PYTHONPATH": SandboxLayout.python_path(),
                "HOME": "/tmp",  # noqa: S108  # nosec B108
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
            "group_memory_bytes": 8 * 1024 * 1024 * 1024,
            "process_oom_score_adj": 0,
        },
        "run": {
            "cwd": "/tmp",  # noqa: S108  # nosec B108
        },
    }
    return _merged(profile, kw)


def plugin_launch(raw: DictConfig, plugin_id: str) -> PluginLaunch:
    """Запуск плагина, как его видит сервис: манифест от объявления плагина
    (стенд строит его в своём процессе, как программа плагина) и секция
    [tool.<id>] конфига raw."""
    manifest = PluginEntry().program(plugin_id).manifest()
    node = OmegaConf.select(raw, f"tool.{plugin_id}")
    container = OmegaConf.to_container(node, resolve=True)

    section: dict[str, Any] = {}
    if isinstance(container, dict):
        for key, value in container.items():
            section[str(key)] = value

    return PluginLaunch(manifest=manifest, section=section)


def section_profile(raw: DictConfig, plugin_id: str) -> SandboxProfile:
    """Профиль плагина той же сборкой, что у приложения: base + манифест.

    Пути образов, workspace и third/bin называет конфиг отладки сервиса: они
    указывают на общий каталог runtime.
    """
    return ZygoteLaunchers(raw).profile_of(plugin_launch(raw, plugin_id))
