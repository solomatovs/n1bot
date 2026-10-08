"""Стенд песочницы для тестов: профиль сборки и зигота секции.

Запуск инструментов в тестах идёт тем же путём, что в приложении: зигота
секции плюс ZygoteToolCaller. Гасить зиготы обязан сам тест — stop() объекта
стенда ZygoteStand.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Callable, Mapping, Sequence
from enum import StrEnum
from pathlib import Path
from typing import Any, ClassVar

import pytest

from boba.runtime.config import EnvOverride
from boba.sandbox import BindSpec, SandboxProfile
from boba.sandbox.guest import WarmupCall
from boba.sandbox.zygote import (
    ZygotePolicy,
    ZygoteRegistry,
    ZygoteToolCaller,
)
from boba.stand.shell import ShellRun
from boba.toolkit.chain import CallAmbient

REPO = Path(__file__).resolve().parents[6]
SANDBOX = REPO / "build" / "src" / "sandbox"
ROOTFS_IMAGE = SANDBOX / "plugins" / "boba-tool-shell" / "rootfs.ext4"
DEPLOY_BIN = SANDBOX / "third" / "bin"
"""Бинарные артефакты сборки: bwrap и fuse2fs из make sandbox."""


class CgroupFs(StrEnum):
    """Адреса cgroup v2 на хосте, по которым стенд проверяет делегирование."""

    ROOT = "/sys/fs/cgroup"
    PROCS = "cgroup.procs"
    OWN = "/proc/self/cgroup"
    UNIFIED = "0::"
    """Префикс строки cgroup v2 в /proc/self/cgroup: за ним путь от корня."""


class SandboxCgroup:
    """Делегированное поддерево cgroup v2, в котором тесты и стенды заводят
    leaf'ы песочницы.

    Базу называет окружение прогона переменной BOBA_CGROUP_BASE — той же,
    которой приложение переопределяет [env].cgroup_base. На хосте разработки
    это каталог sandbox user-юнита boba-sandbox@debug.service, а сам прогон
    стартует внутри слайса юнита (.vscode/python-debug-slice.sh): ядро
    переносит процесс в leaf только под общим предком, доступным на запись.
    Создаётся тестом или стендом приложения; путей по умолчанию нет — без
    переменной тесты с групповыми лимитами пропускаются.
    """

    def __init__(self) -> None:
        self._var = EnvOverride.CGROUP_BASE.var
        self._base = os.environ.get(self._var, "")

    @property
    def base(self) -> str:
        """Каталог поддерева; пустая строка — окружение его не назвало."""
        return self._base

    def child(self, name: str) -> str:
        """База отдельного теста внутри поддерева: leaf'ы тестов не смешиваются."""
        return str(Path(self._base) / name)

    def missing(self) -> str:
        """Чего не хватает для переноса процесса в поддерево; пустая строка —
        всё на месте."""
        if not self._base:
            return (
                f"{self._var} is not set: expected the sandbox directory of a "
                "delegated user unit (boba-sandbox@debug.service)"
            )

        procs = Path(self._base) / CgroupFs.PROCS
        if not os.access(procs, os.W_OK):
            return (
                f"cgroup {self._base} is not delegated to the user: "
                f"{procs} is not writable"
            )

        own = self._own()
        if not own:
            return f"{CgroupFs.OWN.value} has no cgroup v2 entry of the test process"

        gate = Path(os.path.commonpath([own, self._base])) / CgroupFs.PROCS
        if not os.access(gate, os.W_OK):
            return (
                f"the test process runs in {own}, outside the delegated subtree "
                f"of {self._base}: {gate} is not writable; start the run through "
                ".vscode/python-debug-slice.sh"
            )

        return ""

    def required(self) -> pytest.MarkDecorator:
        """Метка пропуска тестов, которым нужен перенос процесса в поддерево."""
        reason = self.missing()

        return pytest.mark.skipif(bool(reason), reason=reason)

    def _own(self) -> str:
        """Cgroup процесса прогона; пустая строка — cgroup v2 у процесса нет."""
        for line in Path(CgroupFs.OWN).read_text().splitlines():
            if not line.startswith(CgroupFs.UNIFIED):
                continue

            relative = line.removeprefix(CgroupFs.UNIFIED).lstrip("/")

            return str(Path(CgroupFs.ROOT) / relative)

        return ""


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

        groups = list(SandboxProfile.GROUPS)
        msg = (
            f"sandbox profile: flat field {name!r} belongs to none of the "
            f"profile groups {groups}"
        )
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

    SHELL_SRC: ClassVar[str] = "tools/boba-tool-shell"
    """Пакет bash-тула: тело shell-команд стенда; в профили чужих плагинов
    его src доезжает биндом (with_shell)."""

    SRC_PACKAGES: ClassVar[tuple[str, ...]] = (
        "core/boba-cancellation",
        "core/boba-toolkit",
        "infra/sandbox/boba-sandbox",
        SHELL_SRC,
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
            msg = (
                f"sandbox stand: fuse2fs binary {cls.FUSE2FS} does not exist, "
                f"build the deployment with `make sandbox`"
            )
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

    SHELL_GUEST: ClassVar[str] = "/srv"
    """Куда бинд bash-тула садится в образ чужого плагина: точка обязана
    существовать в read-only корне, /srv в образах пуст."""

    @classmethod
    def with_shell(cls, profile: SandboxProfile) -> SandboxProfile:
        """Профиль, в котором гостю виден модуль bash.

        Профили плагинов боевого конфига несут только свой пакет; стенд
        подкладывает исходники bash-тула биндом и ставит их в PYTHONPATH.
        Профили самого стенда уже держат их в /usr/src.
        """
        env = dict(profile.isolation.env)
        entries = env.get("PYTHONPATH", "").split(os.pathsep)
        if f"/usr/src/{cls.SHELL_SRC}/src" in entries:
            return profile

        guest = cls.SHELL_GUEST
        kept: list[str] = [guest]
        for entry in entries:
            if entry:
                kept.append(entry)

        env["PYTHONPATH"] = os.pathsep.join(kept)
        host = REPO / "packages" / cls.SHELL_SRC / "src"
        bind = BindSpec(host=str(host), target=guest)
        mounts = profile.mounts.model_copy(update={"ro": (*profile.mounts.ro, bind)})
        isolation = profile.isolation.model_copy(update={"env": env})
        return profile.model_copy(update={"mounts": mounts, "isolation": isolation})

    @classmethod
    def image_ro_binds(cls) -> tuple[str, ...]:
        """Бинды кода стенда в корень-образ: python и site уже внутри образа."""
        return (f"{REPO / 'packages'}:/usr/src",)

    @classmethod
    def image_env(cls) -> dict[str, str]:
        """Env зиготы в корне-образе: интерпретатор и код приезжают биндами."""
        return {
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "PYTHONPATH": cls.python_path(),
            "HOME": "/tmp",  # noqa: S108  # nosec B108
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
            "run": {"cwd": "/tmp"},  # noqa: S108  # nosec B108
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
        mkfs = shutil.which("mkfs.ext4")  # noqa: TID251 — стенд ищет по PATH сознательно
        if mkfs is None:
            msg = (
                "sandbox stand: mkfs.ext4 is not found on PATH for the workspace image"
            )
            raise RuntimeError(msg)

        template = tmp_path / "workspace.ext4"
        subprocess.run(  # noqa: S603
            [mkfs, "-q", "-F", str(template), "8m"], check=True, capture_output=True
        )
        return str(template)


class ZygoteStand:
    """Вызывающие поверх зигот; имя секции — ключ реестра.

    Объект один на прогон тестов (фикстура zygote_stand), как способ
    запуска в приложении один на процесс: зиготы секций живут в его реестре.
    Гасит зиготы сам тест — stop(); остаток гасит конец прогона.
    """

    POLICY: ClassVar[ZygotePolicy] = ZygotePolicy(
        start_timeout_sec=60.0,
        max_start_attempts=1,
        restart_backoff_sec=0.05,
        healthy_after_sec=0.5,
        stop_wait_sec=5.0,
        call_poll_sec=0.05,
    )

    def __init__(self) -> None:
        self._zygotes = ZygoteRegistry()

    def registry(self) -> ZygoteRegistry:
        """Реестр зигот стенда для тестов, которым нужен сам супервизор."""
        return self._zygotes

    def caller(
        self,
        section: str,
        profile: SandboxProfile,
        modules: Sequence[str] = (),
        path_vars: Callable[[], Mapping[str, str]] = dict,
        warmup_calls: Sequence[WarmupCall] = (),
    ) -> ZygoteToolCaller:
        """Вызывающий без журнала вызова: обстановка у него своя и пустая."""
        return self.journaled(
            section, profile, CallAmbient(), modules, path_vars, warmup_calls
        )

    def journaled(  # noqa: PLR0913 — вызывающий собирается всеми частями стенда
        self,
        section: str,
        profile: SandboxProfile,
        ambient: CallAmbient,
        modules: Sequence[str] = (),
        path_vars: Callable[[], Mapping[str, str]] = dict,
        warmup_calls: Sequence[WarmupCall] = (),
    ) -> ZygoteToolCaller:
        """Вызывающий с обстановкой теста: тест ставит в неё приёмники журнала."""
        # модуль bash грузится всегда: shell-команды стенда идут через него
        loaded = [
            ShellRun.MODULE,
            *(name for name in modules if name != ShellRun.MODULE),
        ]
        provisioned = SandboxStand.with_shell(profile)
        supervisor = self._zygotes.obtain(
            section, provisioned, loaded, self.POLICY, warmup_calls=warmup_calls
        )
        return ZygoteToolCaller(section, supervisor, provisioned, ambient, path_vars)

    def launchers(
        self,
        section: str,
        profile: SandboxProfile,
        modules: Sequence[str] = (),
        path_vars: Callable[[], Mapping[str, str]] = dict,
    ) -> Callable[[str], ZygoteToolCaller]:
        """LauncherFactory секции: одна зигота на все её инструменты."""
        caller = self.caller(section, profile, modules, path_vars)

        def factory(tool: str) -> ZygoteToolCaller:
            return caller

        return factory

    def stop(self) -> None:
        self._zygotes.stop_all()
