"""Стенд песочницы для тестов: профиль сборки и зигота секции.

Запуск инструментов в тестах идёт тем же путём, что в приложении: зигота
секции плюс ZygoteToolCaller. Гасить зиготы обязан сам тест — ZygoteStand.stop().
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, ClassVar

from boba.sandbox import SandboxProfile
from boba.sandbox.guest import WarmupCall
from boba.sandbox.zygote import (
    ZygotePolicy,
    ZygoteRegistry,
    ZygoteToolCaller,
)

REPO = Path(__file__).resolve().parents[5]
SANDBOX = REPO / "build" / "src" / "sandbox"
ROOTFS = SANDBOX / "rootfs"
ROOTFS_IMAGE = SANDBOX / "rootfs.ext4"
DEPLOY_BIN = REPO / "compose" / "chainlit" / "third" / "bin"
"""Каталог бинарей развёртывания: тот же, что объявлен в конфиге приложения."""


class ProfileFields:
    """Раскладка плоских полей профиля по группам: тестам так короче.

    Группа ищется по модели, поэтому список полей нигде не дублируется, а
    неизвестное имя падает сразу.
    """

    @classmethod
    def place(cls, raw: dict[str, Any], name: str, value: Any) -> None:
        if name in SandboxProfile.GROUPS:
            group = raw.get(name)
            if isinstance(group, Mapping) and isinstance(value, Mapping):
                merged = dict(group)
                merged.update(value)
                raw[name] = merged
                return

            raw[name] = value
            return

        for group_name in SandboxProfile.GROUPS:
            model = SandboxProfile.model_fields[group_name].annotation
            fields = getattr(model, "model_fields", {})
            if name not in fields:
                continue

            group = dict(raw.get(group_name, {}))
            group[name] = value
            raw[group_name] = group
            return

        msg = f"профиль: поле {name!r} не принадлежит ни одной группе"
        raise KeyError(msg)

    @classmethod
    def merged(cls, base: Mapping[str, Any], flat: Mapping[str, Any]) -> dict[str, Any]:
        """Копия базы с наложенными плоскими полями."""
        raw: dict[str, Any] = {}
        for name, value in base.items():
            if isinstance(value, Mapping):
                raw[name] = dict(value)
                continue

            raw[name] = value

        for name, value in flat.items():
            cls.place(raw, name, value)

        return raw


class SandboxStand:
    """Профиль песочницы для тестов: корень сборки, свой код и site-packages."""

    SITE_PACKAGES: ClassVar[str] = "/usr/local/lib/python3.11/site-packages"

    SRC_PACKAGES: ClassVar[tuple[str, ...]] = (
        "core/boba-cancellation",
        "core/boba-toolkit",
        "infra/sandbox/boba-sandbox",
    )
    """Пакеты, чей код нужен зиготе стенда: их src уезжает в PYTHONPATH."""

    FUSE2FS: ClassVar[Path] = DEPLOY_BIN / "fuse2fs"
    """fuse2fs развёртывания: статический, работает и в корне образа, и на хосте."""

    @classmethod
    def fuse2fs(cls) -> str:
        """fuse2fs развёртывания: тот же путь идёт и в бинды, и в binaries профиля.

        Хостовый fuse2fs слинкован динамически с libfuse3 и в корне образа не
        запускается, а собранный в рабочей копии лежит в каталоге, открытом на
        запись группе, — TrustedBinaries такой каталог не принимает.
        """
        if not cls.FUSE2FS.exists():
            msg = f"нет {cls.FUSE2FS}: собери развёртывание — make sandbox"
            raise RuntimeError(msg)

        return str(cls.FUSE2FS)

    @classmethod
    def bin_dirs(cls) -> list[str]:
        """Каталоги бинарей: сначала развёртывание, затем PATH хоста.

        Порядок важен: bwrap и fuse2fs профиль обязан брать из развёртывания —
        их же приносят бинды, а хостовые копии внутри корня образа не работают.
        """
        dirs: list[str] = []
        if DEPLOY_BIN.is_dir():
            dirs.append(str(DEPLOY_BIN))

        for entry in os.environ.get("PATH", "").split(os.pathsep):
            if entry.startswith("/"):
                dirs.append(entry)

        return dirs

    @classmethod
    def python_path(cls, *extra: str) -> str:
        """Каталоги src пакетов внутри песочницы плюс каталоги теста."""
        parts: list[str] = list(extra)
        for name in cls.SRC_PACKAGES:
            parts.append(f"/usr/src/{name}/src")

        return os.pathsep.join(parts)

    @classmethod
    def image_ro_binds(cls) -> tuple[str, ...]:
        """Бинды кода стенда в корень-образ: точки в нём уже есть."""
        return (
            f"{SANDBOX / 'third' / 'python'}:/usr/local",
            f"{SANDBOX / 'site'}:{cls.SITE_PACKAGES}",
            f"{REPO / 'packages'}:/usr/src",
        )

    @classmethod
    def image_env(cls) -> dict[str, str]:
        """Env зиготы в корне-образе: интерпретатор и код приезжают биндами."""
        return {
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "PYTHONPATH": cls.python_path(),
            "HOME": "/tmp",  # noqa: S108
            "LANG": "C.UTF-8",
        }

    @classmethod
    def profile(cls, **overrides: Any) -> SandboxProfile:
        """Профиль стенда; плоские overrides раскладываются по группам сами.

        Тесту незачем помнить, в какой группе лежит поле: группа находится по
        модели, а неизвестное имя падает сразу.
        """
        raw: dict[str, Any] = {
            "host": {
                "binaries": {"dirs": cls.bin_dirs()},
                "mounting": {
                    "mount_wait_sec": 10.0,
                    "mount_poll_sec": 0.05,
                    "shutdown_wait_sec": 5.0,
                    "lock_wait_sec": 10.0,
                    "copy_chunk_bytes": 1 << 20,
                },
                "cgroup_base": "",
                "stderr_tail_bytes": 4096,
                "channel_limit_bytes": 67108864,
                "fail_tail_chars": 2000,
                "kill_grace_sec": 5,
            },
            "rootfs": str(ROOTFS_IMAGE),
            "mounts": {
                "ro": cls.image_ro_binds(),
                "rw": (),
                "tmp": "64M",
            },
            "isolation": {
                "network": False,
                "reap_poll_sec": 0.05,
                "env": cls.image_env(),
            },
            "limits": {
                "timeout_sec": 60,
                "process_memory_bytes": 2 * 1024 * 1024 * 1024,
                "process_cpu_sec": 60,
                "process_file_bytes": 64 * 1024 * 1024,
                "process_open_files": 1024,
                "process_oom_score_adj": 0,
            },
            "run": {"cwd": "/tmp", "shell": "/bin/bash"},  # noqa: S108
        }

        return SandboxProfile.model_validate(ProfileFields.merged(raw, overrides))

    @classmethod
    def image_profile(cls, tmp_path: Path, **overrides: Any) -> SandboxProfile:
        """Профиль с образом workspace: обвязку монтирования ставит профиль."""
        template = cls.mkfs_template(tmp_path)
        images = tmp_path / "ws"
        images.mkdir(exist_ok=True)

        raw: dict[str, Any] = {
            "workspace": {
                "template": template,
                "mount": f"{images}/{{user_id}}.ext4:/workspace",
            },
            "tmp": "64M",
            "cwd": "/workspace",
        }
        raw.update(overrides)
        return cls.profile(**raw)

    @staticmethod
    def mkfs_template(tmp_path: Path) -> str:
        """Шаблон workspace-образа: пустой ext4 на 8 МБ."""
        mkfs = shutil.which("mkfs.ext4")
        if mkfs is None:
            msg = "mkfs.ext4 недоступен"
            raise RuntimeError(msg)

        template = tmp_path / "workspace.ext4"
        subprocess.run(  # noqa: S603
            [mkfs, "-q", "-F", str(template), "8m"], check=True, capture_output=True
        )
        return str(template)


class ZygoteStand:
    """Вызывающие поверх зигот; имя секции — ключ реестра."""

    POLICY: ClassVar[ZygotePolicy] = ZygotePolicy(
        start_timeout_sec=60.0,
        max_start_attempts=1,
        restart_backoff_sec=0.05,
        healthy_after_sec=0.5,
        stop_wait_sec=5.0,
        call_poll_sec=0.05,
    )

    @classmethod
    def caller(
        cls,
        section: str,
        profile: SandboxProfile,
        modules: Sequence[str] = (),
        path_vars: Callable[[], Mapping[str, str]] = dict,
        warmup_calls: Sequence[WarmupCall] = (),
    ) -> ZygoteToolCaller:
        supervisor = ZygoteRegistry.obtain(
            section, profile, modules, cls.POLICY, warmup_calls=warmup_calls
        )
        return ZygoteToolCaller(section, supervisor, profile, path_vars)

    @classmethod
    def launchers(
        cls,
        section: str,
        profile: SandboxProfile,
        modules: Sequence[str] = (),
        path_vars: Callable[[], Mapping[str, str]] = dict,
    ) -> Callable[[str], ZygoteToolCaller]:
        """LauncherFactory секции: одна зигота на все её инструменты."""
        caller = cls.caller(section, profile, modules, path_vars)

        def factory(tool: str) -> ZygoteToolCaller:
            return caller

        return factory

    @staticmethod
    def stop() -> None:
        ZygoteRegistry.stop_all()
