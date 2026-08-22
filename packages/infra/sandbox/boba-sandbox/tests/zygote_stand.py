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
from boba.sandbox.zygote import (
    ZygotePolicy,
    ZygoteRegistry,
    ZygoteToolCaller,
)
from boba.toolkit.zygote import WarmupCall

REPO = Path(__file__).resolve().parents[5]
SANDBOX = REPO / "build" / "src" / "sandbox"
ROOTFS = SANDBOX / "rootfs"
ROOTFS_IMAGE = SANDBOX / "rootfs.ext4"
DEPLOY_BIN = REPO / "compose" / "third" / "bin"
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
    )
    """Пакеты, чей код нужен зиготе стенда: их src уезжает в PYTHONPATH."""

    FUSE2FS: ClassVar[Path] = DEPLOY_BIN / "fuse2fs"
    """fuse2fs развёртывания: статический, работает и в корне образа, и на хосте."""

    IMAGE_MOUNTS: ClassVar[Mapping[str, str]] = {
        "template": "/mnt/workspace.ext4",
        "fuse2fs": "/mnt/fuse2fs",
        "images": "/mnt/images",
    }
    """Точки образной обвязки, как их объявляет конфиг стенда."""

    @classmethod
    def fuse2fs(cls) -> str:
        """fuse2fs развёртывания: тот же путь идёт и в бинды, и в binaries профиля.

        Хостовый fuse2fs слинкован динамически с libfuse3 и в корне образа не
        запускается, а собранный в рабочей копии лежит в каталоге, открытом на
        запись группе, — TrustedBinaries такой каталог не принимает.
        """
        if not cls.FUSE2FS.exists():
            msg = f"нет {cls.FUSE2FS}: собери развёртывание — make sandbox-image"
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
                "fail_tail_chars": 2000,
                "kill_grace_sec": 5,
            },
            "rootfs": {"dir": str(ROOTFS), "image": "", "mount": ""},
            "mounts": {
                "ro": (
                    f"{SANDBOX / 'third' / 'python'}:/usr/local",
                    f"{SANDBOX / 'site'}:{cls.SITE_PACKAGES}",
                    f"{REPO / 'packages'}:/usr/src",
                ),
                "rw": (),
                "setup_ro": (),
                "setup_rw": (),
                "tmpfs": ("/tmp:64M",),  # noqa: S108
                "call_tmpfs": "/tmp",  # noqa: S108
                "proc": "/proc",
                "dev": "/dev",
                "images": (),
                "image_template": "",
            },
            "isolation": {
                "network": False,
                "max_processes": 256,
                "reap_poll_sec": 0.05,
                "env": {
                    "PATH": "/usr/local/bin:/usr/bin:/bin",
                    "PYTHONPATH": cls.python_path(),
                    "HOME": "/tmp",  # noqa: S108
                    "LANG": "C.UTF-8",
                },
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
        """Профиль с образом workspace: обвязка монтирования объявлена явно.

        Точки под tmpfs /mnt: на read-only корне bwrap точку не создаст.
        """
        template = cls.mkfs_template(tmp_path)
        images = tmp_path / "ws"
        images.mkdir(exist_ok=True)

        fuse2fs = cls.fuse2fs()

        raw: dict[str, Any] = {
            "setup_ro": (
                f"{template}:{cls.IMAGE_MOUNTS['template']}",
                f"{fuse2fs}:{cls.IMAGE_MOUNTS['fuse2fs']}",
            ),
            "setup_rw": (f"{images}:{cls.IMAGE_MOUNTS['images']}",),
            "workspace": {
                "template": template,
                "images": str(images),
                "mount": "/workspace",
            },
            "tmpfs": ("/tmp:64M", "/mnt:1M"),  # noqa: S108
            "cwd": "/workspace",
        }
        raw.update(overrides)
        return cls.profile(**raw)

    VENV: ClassVar[Path] = REPO / ".venv"

    @classmethod
    def host_python_binds(cls) -> tuple[str, ...]:
        """Бинды для профиля с хостовым корнем: venv-интерпретатор и свой код.

        Зигота стартует `python3 -m boba.toolkit.zygote`, поэтому ей нужен
        интерпретатор с установленными пакетами — на хостовом корне это venv
        стенда, смонтированный своим же путём (в нём абсолютные пути .pth).
        """
        return (
            f"{cls.VENV}:{cls.VENV}",
            f"{REPO / 'packages'}:{REPO / 'packages'}",
        )

    @classmethod
    def host_python_path(cls) -> str:
        return f"{cls.VENV}/bin:/usr/local/bin:/usr/bin:/bin"

    @classmethod
    def image_binds(cls, template: str, images_dir: str) -> tuple[tuple[str, ...], ...]:
        """Бинды образной обвязки хостовыми путями (host==target).

        Годится профилям, чей корень собран из хостовых каталогов: там точку
        монтирования bwrap создаёт сам. Возвращает (ro_binds, rw_binds).
        """
        fuse2fs = cls.fuse2fs()

        ro = (f"{template}:{template}", f"{fuse2fs}:{fuse2fs}")
        rw = (f"{images_dir}:{images_dir}",)
        return ro, rw

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
