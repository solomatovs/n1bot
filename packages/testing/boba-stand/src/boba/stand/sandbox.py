"""Профиль песочницы для тестов инструментов: артефакты сборки плюс код репо.

Зависимости — из собранного site, код инструментов — из src: иначе тест проверял бы
прошлую сборку, а не то, что сейчас в репозитории.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any, ClassVar

import pytest
from omegaconf import DictConfig, OmegaConf

from boba.runtime.launchers import ZygoteLaunchers
from boba.runtime.plugins import EntryPointPlugins
from boba.sandbox import SandboxProfile
from boba.toolkit.manifest import LaunchSpec

REPO = Path(__file__).resolve().parents[6]
SANDBOX = REPO / "build" / "chainlit" / "src" / "sandbox"
PLUGIN_IMAGES = SANDBOX / "plugins"
ROOTFS_IMAGE = PLUGIN_IMAGES / "boba-tool-shell" / "rootfs.ext4"
"""Базовый корень стендов: образ shell-плагина, bash и python без payload'ов."""


def plugin_rootfs(package: str) -> Path:
    """Образ корня пакета: make plugin-rootfs PLUGIN=<пакет>."""
    return PLUGIN_IMAGES / package / "rootfs.ext4"

"""Код пакетов монтируется одним каталогом: точку /usr/src несёт rootfs."""

ADDRESS_SPACE = 16 * 1024 * 1024 * 1024
"""RLIMIT_AS профиля парсера: pdfium резервирует ~2.3G независимо от документа."""

needs_sandbox = pytest.mark.skipif(
    shutil.which("bwrap") is None  # noqa: TID251 — стенд ищет по PATH сознательно
    or not ROOTFS_IMAGE.exists(),
    reason=(
        "нет bwrap или образов плагинов "
        "(собрать: make -C build/chainlit fetch plugin-rootfs-all)"
    ),
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
    """Монтирования и env: код пакетов кладётся поверх собранного site."""

    DATA_BINDS: ClassVar[dict[str, tuple[tuple[str, str], ...]]] = {
        "boba-tool-knowledge": (
            ("fastembed", "/var/cache/fastembed"),
            ("tessdata", "/usr/share/tessdata"),
        ),
        "boba-tool-doc": (("tessdata", "/usr/share/tessdata"),),
    }
    """Данные моделей по пакетам: рантайм монтирует их биндами ([sandbox].binds
    секций), образ несёт только пустые точки — и только у пакетов, в замыкании
    которых объявлены data-пути; бинд в чужой rootfs уронил бы bwrap."""

    @staticmethod
    def ro_binds(docs_dir: Path | None, package: str = "") -> list[str]:
        """Код репозитория поверх кода образа плюс данные моделей пакета."""
        binds = [f"{REPO / 'packages'}:/usr/src"]

        models = SandboxLayout.models_dir()
        for name, guest in SandboxLayout.DATA_BINDS.get(package, ()):
            binds.append(f"{models / name}:{guest}")

        if docs_dir is not None:
            binds.append(f"{docs_dir}:/workspace")

        return binds

    @staticmethod
    def models_dir() -> Path:
        base = os.environ.get("BOBA_BASE")
        if base is None:
            msg = "BOBA_BASE is required to locate model data for the sandbox"
            raise RuntimeError(msg)

        return Path(base) / "models"

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


def sandbox_profile(
    package: str, docs_dir: Path | None = None, **kw: Any
) -> dict[str, Any]:
    """Профиль запуска payload'а пакета — тот же по смыслу, что в приложении."""
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
        "rootfs": str(plugin_rootfs(package)),
        "mounts": {
            "ro": tuple(SandboxLayout.ro_binds(docs_dir, package)),
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
            "process_oom_score_adj": 0,
        },
        "run": {
            "shell": "/bin/bash",
            "cwd": "/tmp",  # noqa: S108  # nosec B108
        },
    }
    return _merged(profile, kw)


def plugin_launch_spec(section: str) -> LaunchSpec:
    """Спека запуска секции из установленных entry points — как у приложения."""
    table = EntryPointPlugins.discover()
    plugin = table.get(section)
    if plugin is None:
        msg = f"plugin {section!r} is not installed"
        raise RuntimeError(msg)

    return LaunchSpec(
        section=plugin.section,
        modules=plugin.modules,
        package=plugin.package,
    )


def _staged(value: Any, deploy_sandbox: str, deploy_third: str) -> Any:
    """Пути развёртывания в значении профиля -> артефакты сборки."""
    if isinstance(value, str):
        replaced = value.replace(deploy_sandbox, str(SANDBOX))
        return replaced.replace(deploy_third, str(SANDBOX / "third"))

    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, entry in value.items():
            result[key] = _staged(entry, deploy_sandbox, deploy_third)
        return result

    if isinstance(value, (list, tuple)):
        items: list[Any] = []
        for entry in value:
            items.append(_staged(entry, deploy_sandbox, deploy_third))
        return items

    return value


def section_profile(raw: DictConfig, section: str) -> SandboxProfile:
    """Профиль секции той же сборкой, что у приложения: base + манифест.

    Пути развёртывания (rootfs, workspace, third/bin) подменяются артефактами
    сборки: dev-хост работает process-режимом и их не раскладывает.
    """
    profile = ZygoteLaunchers(raw).profile_of(plugin_launch_spec(section))

    deploy_sandbox = str(OmegaConf.select(raw, "env.sandbox"))
    deploy_third = str(OmegaConf.select(raw, "env.base")) + "/third"
    data = _staged(profile.model_dump(), deploy_sandbox, deploy_third)

    return SandboxProfile.model_validate(data)
