"""Групповые лимиты запуска: cgroup v2, отдельный leaf на каждый вызов.
База делегируется приложению: миграция в leaf разрешена лишь под общим предком."""

from __future__ import annotations

import contextlib
import errno
import logging
import os
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import ClassVar

from boba.sandbox.profile import SandboxProfile

__all__ = ["CgroupError", "CgroupManager", "GroupLimits"]

logger = logging.getLogger(__name__)


class CgroupError(RuntimeError):
    """Групповой лимит запрошен, но не может быть применён на этой машине."""


@dataclass(frozen=True)
class GroupLimits:
    """Лимиты на весь запуск суммарно; None — параметр не контролируется."""

    memory_bytes: int | None = None
    cpu_percent: int | None = None
    cpu_weight: int | None = None
    pids_max: int | None = None
    swap_max_bytes: int | None = None
    oom_kill_all: bool | None = None

    CPU_PERIOD_USEC: ClassVar[int] = 100_000

    @classmethod
    def of_profile(cls, profile: SandboxProfile) -> GroupLimits:
        return cls(
            memory_bytes=profile.cgroup_memory_bytes,
            cpu_percent=profile.cgroup_cpu_percent,
            cpu_weight=profile.cgroup_cpu_weight,
            pids_max=profile.cgroup_pids_max,
            swap_max_bytes=profile.cgroup_swap_max_bytes,
            oom_kill_all=profile.cgroup_oom_kill_all,
        )

    @property
    def requested(self) -> bool:
        fields = (
            self.memory_bytes,
            self.cpu_percent,
            self.cpu_weight,
            self.pids_max,
            self.swap_max_bytes,
            self.oom_kill_all,
        )
        return any(value is not None for value in fields)

    @property
    def controllers(self) -> tuple[str, ...]:
        needed: list[str] = []
        if self.cpu_percent is not None or self.cpu_weight is not None:
            needed.append("cpu")
        memory_fields = (self.memory_bytes, self.swap_max_bytes, self.oom_kill_all)
        if any(value is not None for value in memory_fields):
            needed.append("memory")
        if self.pids_max is not None:
            needed.append("pids")
        return tuple(needed)

    @property
    def cpu_max(self) -> str:
        """Значение cpu.max: квота в мкс на период 100000 мкс."""
        if self.cpu_percent is None:
            msg = "cpu_max is undefined: cpu_percent is not set"
            raise ValueError(msg)
        quota = self.cpu_percent * self.CPU_PERIOD_USEC // 100
        return f"{quota} {self.CPU_PERIOD_USEC}"

    def describe(self) -> str:
        labels = (
            ("memory", self.memory_bytes, "B"),
            ("cpu_max", self.cpu_percent, "%"),
            ("cpu_weight", self.cpu_weight, ""),
            ("pids", self.pids_max, ""),
            ("swap_max", self.swap_max_bytes, "B"),
            ("oom_kill_all", self.oom_kill_all, ""),
        )
        parts: list[str] = []
        for name, value, unit in labels:
            if value is not None:
                parts.append(f"{name}={value}{unit}")
        return " ".join(parts) if parts else "none"


class CgroupManager:
    """Готовит базу однажды и выдаёт per-run leaf'ы с лимитами."""

    MAIN_LEAF: ClassVar[str] = "main"
    """Сюда переезжают процессы родителя базы: правило no-internal-process."""

    RELEASE_WAIT_SEC: ClassVar[float] = 5.0
    RELEASE_POLL_SEC: ClassVar[float] = 0.05

    FIELDS_BY_CONTROLLER: ClassVar[dict[str, str]] = {
        "cpu": "cgroup_cpu_percent/cgroup_cpu_weight",
        "memory": "cgroup_memory_bytes/cgroup_swap_max_bytes/cgroup_oom_kill_all",
        "pids": "cgroup_pids_max",
    }

    _prepared: ClassVar[dict[str, set[str]]] = {}
    """База -> контроллеры, уже включённые в её subtree_control."""

    _lock: ClassVar[threading.Lock] = threading.Lock()

    def __init__(self, base: str) -> None:
        self._base = base

    def acquire(self, limits: GroupLimits) -> str:
        """Создаёт leaf под один запуск, пишет лимиты; возвращает путь."""
        self._prepare(limits.controllers)
        leaf = os.path.join(
            self._base, f"run-{os.getpid()}-{uuid.uuid4().hex[:8]}"
        )
        try:
            os.mkdir(leaf)
            self._write_limits(leaf, limits)
        except CgroupError:
            self._remove_quietly(leaf)
            raise
        except OSError as e:
            self._remove_quietly(leaf)
            msg = f"cgroup: cannot create leaf {leaf}: {e}"
            raise CgroupError(msg) from e
        return leaf

    def release(self, leaf: str) -> None:
        """Добивает выживших через cgroup.kill и удаляет leaf."""
        try:
            self._write(os.path.join(leaf, "cgroup.kill"), "1")
        except OSError as e:
            if e.errno != errno.ENOENT:
                logger.warning("cgroup: kill of %s failed: %s", leaf, e)
        deadline = time.monotonic() + self.RELEASE_WAIT_SEC
        while time.monotonic() < deadline:
            try:
                os.rmdir(leaf)
                return
            except OSError as e:
                if e.errno == errno.ENOENT:
                    return
                if e.errno != errno.EBUSY:
                    logger.warning("cgroup: cannot remove %s: %s", leaf, e)
                    return
            time.sleep(self.RELEASE_POLL_SEC)
        logger.warning("cgroup: leaf %s is still busy and was not removed", leaf)

    @classmethod
    def probe_profiles(cls, profiles: Mapping[str, SandboxProfile]) -> None:
        """Startup-проверка групповых лимитов; отказ называет профиль и параметр."""
        for name, profile in profiles.items():
            limits = GroupLimits.of_profile(profile)
            if not limits.requested:
                continue
            manager = cls(profile.cgroup_base)
            try:
                leaf = manager.acquire(limits)
                try:
                    manager.probe_migration(leaf)
                finally:
                    manager.release(leaf)
            except CgroupError as e:
                msg = f"sandbox profile {name!r}: {e}"
                raise CgroupError(msg) from e
            logger.info(
                "sandbox cgroup: profile %r ok in %s (%s)",
                name,
                profile.cgroup_base,
                limits.describe(),
            )

    def probe_migration(self, leaf: str) -> None:
        """Пробный ребёнок входит в leaf: ловит правило общего предка сразу."""
        procs = os.path.join(leaf, "cgroup.procs")

        def enter() -> None:
            fd = os.open(procs, os.O_WRONLY)
            os.write(fd, b"0")
            os.close(fd)

        try:
            subprocess.run(
                [sys.executable, "-c", ""],
                check=True,
                capture_output=True,
                preexec_fn=enter,
            )
        except (subprocess.SubprocessError, OSError) as e:
            msg = (
                f"cgroup: cannot move a child into {leaf}: {e}; the "
                f"application itself must run inside the delegated subtree "
                f"(host: systemd-run --user --scope; docker: private cgroup "
                f"namespace), otherwise remove the cgroup_* limits"
            )
            raise CgroupError(msg) from e

    def _prepare(self, controllers: tuple[str, ...]) -> None:
        with self._lock:
            enabled = self._prepared.setdefault(self._base, set())
            missing = [c for c in controllers if c not in enabled]
            if not missing:
                return
            self._prepare_base(missing)
            enabled.update(missing)

    def _prepare_base(self, controllers: list[str]) -> None:
        try:
            os.makedirs(self._base, exist_ok=True)
        except OSError as e:
            msg = (
                f"cgroup: cannot create base {self._base}: {e}; the directory "
                f"must live under a cgroup v2 subtree delegated to this user"
            )
            raise CgroupError(msg) from e
        available = self._read(os.path.join(self._base, "cgroup.controllers"))
        absent = [c for c in controllers if c not in available.split()]
        if absent:
            self._enable_in_parent(absent)
        self._enable(self._base, controllers)

    def _enable_in_parent(self, controllers: list[str]) -> None:
        """Контроллеров нет в базе — включает их в subtree_control родителя."""
        parent = os.path.dirname(self._base)
        try:
            self._enable(parent, controllers)
            return
        except OSError as e:
            if e.errno != errno.EBUSY:
                fields = ", ".join(
                    self.FIELDS_BY_CONTROLLER[c] for c in controllers
                )
                msg = (
                    f"cgroup: controllers {controllers} unavailable in "
                    f"{parent}: {e}; fix the delegation or set {fields} to 0"
                )
                raise CgroupError(msg) from e
        # EBUSY = no-internal-process: процессы родителя переезжают в main
        self._evacuate(parent)
        try:
            self._enable(parent, controllers)
        except OSError as e:
            msg = f"cgroup: cannot enable {controllers} in {parent}: {e}"
            raise CgroupError(msg) from e

    def _evacuate(self, parent: str) -> None:
        main = os.path.join(parent, self.MAIN_LEAF)
        os.makedirs(main, exist_ok=True)
        procs_path = os.path.join(parent, "cgroup.procs")
        for _ in range(10):
            pids = self._read(procs_path).split()
            if not pids:
                return
            logger.info(
                "cgroup: moving %d process(es) from %s to %s",
                len(pids),
                parent,
                main,
            )
            for pid in pids:
                try:
                    self._write(os.path.join(main, "cgroup.procs"), pid)
                except OSError as e:
                    if e.errno != errno.ESRCH:
                        msg = (
                            f"cgroup: cannot move pid {pid} from {parent} "
                            f"to {main}: {e}"
                        )
                        raise CgroupError(msg) from e
        msg = f"cgroup: {parent} keeps spawning processes, cannot evacuate"
        raise CgroupError(msg)

    def _write_limits(self, leaf: str, limits: GroupLimits) -> None:
        if limits.memory_bytes is not None:
            self._write_limit(
                leaf, "memory.max", str(limits.memory_bytes), "cgroup_memory_bytes"
            )
        if limits.swap_max_bytes is not None:
            self._write_limit(
                leaf,
                "memory.swap.max",
                str(limits.swap_max_bytes),
                "cgroup_swap_max_bytes",
            )
        if limits.oom_kill_all is not None:
            self._write_limit(
                leaf,
                "memory.oom.group",
                "1" if limits.oom_kill_all else "0",
                "cgroup_oom_kill_all",
            )
        if limits.cpu_percent is not None:
            self._write_limit(leaf, "cpu.max", limits.cpu_max, "cgroup_cpu_percent")
        if limits.cpu_weight is not None:
            self._write_limit(
                leaf, "cpu.weight", str(limits.cpu_weight), "cgroup_cpu_weight"
            )
        if limits.pids_max is not None:
            self._write_limit(
                leaf, "pids.max", str(limits.pids_max), "cgroup_pids_max"
            )

    def _write_limit(self, leaf: str, knob: str, value: str, field: str) -> None:
        try:
            self._write(os.path.join(leaf, knob), value)
        except OSError as e:
            msg = (
                f"cgroup: {knob} is not supported here ({e}); "
                f"set {field} = 0 in the profile or fix the delegation"
            )
            raise CgroupError(msg) from e

    def _enable(self, cgroup_dir: str, controllers: list[str]) -> None:
        value = " ".join(f"+{c}" for c in controllers)
        self._write(os.path.join(cgroup_dir, "cgroup.subtree_control"), value)

    @staticmethod
    def _read(path: str) -> str:
        with open(path) as f:
            return f.read()

    @staticmethod
    def _write(path: str, value: str) -> None:
        with open(path, "w") as f:
            f.write(value)

    @staticmethod
    def _remove_quietly(leaf: str) -> None:
        with contextlib.suppress(OSError):
            os.rmdir(leaf)
