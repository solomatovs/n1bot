"""Нагрузка и аварийные завершения песочницы: ресурсы обязаны освобождаться.

Проверяется внешнее состояние хоста после вызовов: точки монтирования,
fuse2fs-демоны, дескрипторы, flock образов, cgroup-leaf'ы и недокопированные
образы. Инвариант один — после любого исхода вызова состояние возвращается
к тому, что было до него.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Iterator, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from enum import StrEnum
from pathlib import Path
from typing import ClassVar
from uuid import uuid4

import pytest
from pydantic import BaseModel, ConfigDict
from zygote_stand import ROOTFS_IMAGE, ProfileFields, SandboxStand

from boba.cancellation import ToolStopped, TurnCancellation, turn_cancellation
from boba.chainlit.data.storage import ImageStorageClient, StorageFactory
from boba.chainlit.infra.config import LocalStorageConfig
from boba.sandbox import SandboxProfile
from boba.sandbox.cgroup import CgroupManager
from boba.sandbox.profile import SandboxMount
from boba.sandbox.zygote import (
    ZygotePolicy,
    ZygoteRegistry,
    ZygoteSpawner,
    ZygoteToolCaller,
)
from boba.toolkit.images import PartialCopy
from boba.toolkit.launcher import LauncherError, LaunchOutcome
from boba.toolkit.stream import Chunk, JournalChannel, StreamSink, ToolChannelsTap
from boba.workspace.launcher import (
    FUSE_DEVICE,
    ReadWindow,
)

pytestmark = pytest.mark.load

_IMAGES_MOUNT = SandboxMount.SETUP_IMAGES.value
"""Каталог образов внутри песочницы: его несёт в cmdline fuse2fs вызова."""


needs_fuse = pytest.mark.skipif(
    shutil.which("bwrap") is None
    or shutil.which("fuse2fs") is None
    or shutil.which("mkfs.ext4") is None
    or not os.path.exists(FUSE_DEVICE),
    reason="нужны bwrap, fuse2fs, mkfs.ext4 и /dev/fuse",
)


class BrokenSink(StreamSink):
    """Приёмник журнала, который падает на первом же куске вывода."""

    def feed(self, data: Chunk) -> None:
        msg = "consumer is broken"
        raise RuntimeError(msg)

    def feed_text(self, text: str) -> None:
        msg = "consumer is broken"
        raise RuntimeError(msg)


class BrokenSinks:
    """Журнал вызова, у которого сломан любой канал."""

    def sink_of(self, channel: JournalChannel) -> StreamSink:
        return BrokenSink()


class CgroupZone:
    """Делегированная cgroup v2 зона стенда: первая доступная на запись."""

    _UID: ClassVar[int] = os.getuid()

    CANDIDATES: ClassVar[tuple[str, ...]] = (
        f"/sys/fs/cgroup/boba.slice/user-{_UID}.slice/user@{_UID}.service",
        "/sys/fs/cgroup/boba.slice/boba-sandbox",
    )
    ENV: ClassVar[str] = "BOBA_CGROUP_BASE"

    @classmethod
    def find(cls) -> str:
        """Пустая строка — делегированной зоны на машине нет."""
        for path in cls._paths():
            if not os.path.isdir(path):
                continue

            if not os.access(path, os.W_OK):
                continue

            return path

        return ""

    @classmethod
    def _paths(cls) -> tuple[str, ...]:
        configured = os.environ.get(cls.ENV, "")
        if not configured:
            return cls.CANDIDATES

        return (configured, *cls.CANDIDATES)


needs_delegation = pytest.mark.skipif(
    not CgroupZone.find(),
    reason="нет делегированной cgroup v2 зоны, куда можно мигрировать процесс",
)


class LoadScale:
    """Размер нагрузки: столько пользователей и вызовов на пользователя."""

    USERS: ClassVar[int] = 8
    CALLS: ClassVar[int] = 5
    THREADS: ClassVar[int] = 8
    REPEATS: ClassVar[int] = 25
    """Последовательные вызовы для проверки утечки дескрипторов."""


class Waiting:
    """Тайминги ожиданий стенда: у ресурсов есть право освобождаться не мгновенно."""

    SETTLE_SEC: ClassVar[float] = 20.0
    POLL_SEC: ClassVar[float] = 0.1
    APPEAR_SEC: ClassVar[float] = 30.0


class ProcName(StrEnum):
    """Процессы, которые стенд ищет в /proc по исполняемому файлу."""

    FUSE2FS = "fuse2fs"
    BWRAP = "bwrap"


class ProcTable:
    """Процессы хоста: поиск по cmdline и прямые потомки процесса."""

    PROC: ClassVar[str] = "/proc"

    @classmethod
    def matching(cls, name: ProcName, needle: str) -> frozenset[int]:
        """Pid'ы процесса name, в чьём cmdline есть путь стенда.

        Имя ищется в argv[0], а не по всему cmdline: bwrap несёт путь
        fuse2fs аргументом бинда и иначе считался бы fuse-демоном.
        """
        found: set[int] = set()

        for entry in os.listdir(cls.PROC):
            if not entry.isdigit():
                continue

            pid = int(entry)
            cmdline = cls.cmdline(pid)
            if needle not in cmdline:
                continue

            argv0 = cmdline.split(" ")[0]
            if os.path.basename(argv0) != name.value:
                continue

            found.add(pid)

        return frozenset(found)

    @classmethod
    def cmdline(cls, pid: int) -> str:
        path = os.path.realpath(os.path.join(cls.PROC, str(pid), "cmdline"))
        try:
            with open(path, "rb") as f:
                raw = f.read()
        except OSError:
            return ""

        return raw.replace(b"\0", b" ").decode("utf-8", errors="replace")

    @classmethod
    def children_of(cls, pid: int) -> frozenset[int]:
        """Прямые потомки: осиротевшая обвязка вызова видна здесь."""
        kids: set[int] = set()
        task_dir = os.path.join(cls.PROC, str(pid), "task")

        try:
            tids = os.listdir(task_dir)
        except OSError:
            return frozenset()

        for tid in tids:
            path = os.path.realpath(os.path.join(task_dir, tid, "children"))
            try:
                with open(path) as f:
                    raw = f.read()
            except OSError:
                continue

            for token in raw.split():
                kids.add(int(token))

        return frozenset(kids)

    @classmethod
    def alive(cls, pid: int) -> bool:
        return os.path.exists(os.path.join(cls.PROC, str(pid)))

    @classmethod
    def wait_gone(cls, pids: Sequence[int], timeout_sec: float) -> tuple[int, ...]:
        """Ждёт исчезновения процессов; возвращает выживших."""
        deadline = time.monotonic() + timeout_sec

        while time.monotonic() < deadline:
            survivors = tuple(pid for pid in pids if cls.alive(pid))
            if not survivors:
                return ()

            time.sleep(Waiting.POLL_SEC)

        return tuple(pid for pid in pids if cls.alive(pid))


class ResourceLeak(BaseModel):
    """Что осталось сверх исходного состояния; пусто — ресурсы освобождены."""

    model_config = ConfigDict(frozen=True)

    host_mounts: frozenset[str]
    fuse_daemons: frozenset[int]
    children: frozenset[int]
    stale_mounts: frozenset[str]
    partial_copies: frozenset[str]
    held_locks: frozenset[str]
    cgroup_leaves: frozenset[str]
    extra_fds: int

    @property
    def empty(self) -> bool:
        groups = (
            self.host_mounts,
            self.fuse_daemons,
            self.children,
            self.stale_mounts,
            self.partial_copies,
            self.held_locks,
            self.cgroup_leaves,
        )
        for group in groups:
            if group:
                return False

        return self.extra_fds <= 0

    def describe(self) -> str:
        parts: list[str] = []

        if self.host_mounts:
            parts.append(f"host mounts left: {sorted(self.host_mounts)}")

        if self.fuse_daemons:
            parts.append(f"fuse2fs alive: {sorted(self.fuse_daemons)}")

        if self.children:
            parts.append(f"child processes alive: {sorted(self.children)}")

        if self.stale_mounts:
            parts.append(f"stale mountpoints: {sorted(self.stale_mounts)}")

        if self.partial_copies:
            parts.append(f"partial image copies: {sorted(self.partial_copies)}")

        if self.held_locks:
            parts.append(f"locks still held: {sorted(self.held_locks)}")

        if self.cgroup_leaves:
            parts.append(f"cgroup leaves left: {sorted(self.cgroup_leaves)}")

        if self.extra_fds > 0:
            parts.append(f"open descriptors grown by {self.extra_fds}")

        if not parts:
            return "nothing leaked"

        return "; ".join(parts)


class ResourceCensus(BaseModel):
    """Снимок внешних ресурсов вызова: сравнимое состояние до и после."""

    model_config = ConfigDict(frozen=True)

    host_mounts: frozenset[str]
    fuse_daemons: frozenset[int]
    children: frozenset[int]
    stale_mounts: frozenset[str]
    partial_copies: frozenset[str]
    held_locks: frozenset[str]
    cgroup_leaves: frozenset[str]
    open_fds: int

    MOUNTINFO: ClassVar[str] = "/proc/self/mountinfo"
    MOUNT_FIELD: ClassVar[int] = 4
    FD_DIR: ClassVar[str] = "/proc/self/fd"
    MNT_SUFFIX: ClassVar[str] = ".mnt"
    LOCK_SUFFIX: ClassVar[str] = ".lock"
    PARTIAL_GLOB: ClassVar[str] = "**/*.tmp.*"
    LEAF_PREFIX: ClassVar[str] = "run-"

    @classmethod
    def _call_children(cls) -> frozenset[int]:
        """Потомки процесса тестов без резидентных зигот: считаем ресурсы вызова.

        Зигота секции живёт между вызовами и поднимается лениво — в снимок
        «до» она не попадает и иначе выглядела бы утечкой.
        """
        kids: set[int] = set()

        for pid in ProcTable.children_of(os.getpid()):
            if ZygoteSpawner.MODULE in ProcTable.cmdline(pid):
                continue

            kids.add(pid)

        return frozenset(kids)

    @classmethod
    def capture(cls, root: Path, cgroup_base: str = "") -> ResourceCensus:
        return cls(
            host_mounts=cls._host_mounts(root),
            fuse_daemons=ProcTable.matching(ProcName.FUSE2FS, _IMAGES_MOUNT),
            children=cls._call_children(),
            stale_mounts=cls._stale_mounts(root),
            partial_copies=cls._partial_copies(root),
            held_locks=cls._held_locks(root),
            cgroup_leaves=cls._cgroup_leaves(cgroup_base),
            open_fds=cls._open_fds(),
        )

    def leaked_over(self, before: ResourceCensus) -> ResourceLeak:
        return ResourceLeak(
            host_mounts=self.host_mounts - before.host_mounts,
            fuse_daemons=self.fuse_daemons - before.fuse_daemons,
            children=self.children - before.children,
            stale_mounts=self.stale_mounts - before.stale_mounts,
            partial_copies=self.partial_copies - before.partial_copies,
            held_locks=self.held_locks - before.held_locks,
            cgroup_leaves=self.cgroup_leaves - before.cgroup_leaves,
            extra_fds=self.open_fds - before.open_fds,
        )

    @classmethod
    def settle(
        cls,
        before: ResourceCensus,
        root: Path,
        cgroup_base: str = "",
        timeout_sec: float = Waiting.SETTLE_SEC,
    ) -> ResourceLeak:
        """Ждёт возврата к исходному состоянию; отдаёт последнюю разницу."""
        deadline = time.monotonic() + timeout_sec
        leak = cls.capture(root, cgroup_base).leaked_over(before)

        while time.monotonic() < deadline:
            if leak.empty:
                return leak

            time.sleep(Waiting.POLL_SEC)
            leak = cls.capture(root, cgroup_base).leaked_over(before)

        return leak

    @classmethod
    def _host_mounts(cls, root: Path) -> frozenset[str]:
        """Точки монтирования стенда, видимые в namespace хоста."""
        mounts: set[str] = set()

        with open(cls.MOUNTINFO) as f:
            for line in f:
                fields = line.split()
                if len(fields) <= cls.MOUNT_FIELD:
                    continue

                target = fields[cls.MOUNT_FIELD]
                if not target.startswith(str(root)):
                    continue

                mounts.add(target)

        return frozenset(mounts)

    @classmethod
    def _stale_mounts(cls, root: Path) -> frozenset[str]:
        """Каталоги *.mnt, оставшиеся смонтированными или отвечающие ошибкой."""
        stale: set[str] = set()

        for path in root.rglob(f"*{cls.MNT_SUFFIX}"):
            if cls._is_stale(path):
                stale.add(str(path))

        return frozenset(stale)

    @classmethod
    def _is_stale(cls, path: Path) -> bool:
        try:
            os.stat(path)
        except FileNotFoundError:
            return False
        except OSError:
            return True

        return str(path) in cls._host_mounts(path.parent)

    @classmethod
    def _partial_copies(cls, root: Path) -> frozenset[str]:
        partials: set[str] = set()

        for path in root.glob(cls.PARTIAL_GLOB):
            partials.add(str(path))

        return frozenset(partials)

    @classmethod
    def _held_locks(cls, root: Path) -> frozenset[str]:
        """Локи образов, которые сейчас кем-то удерживаются."""
        held: set[str] = set()

        for path in root.rglob(f"*{cls.LOCK_SUFFIX}"):
            if cls._is_locked(path):
                held.add(str(path))

        return frozenset(held)

    @classmethod
    def _is_locked(cls, path: Path) -> bool:
        try:
            fd = os.open(path, os.O_WRONLY)
        except OSError:
            return False

        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        else:
            fcntl.flock(fd, fcntl.LOCK_UN)
            return False
        finally:
            os.close(fd)

    @classmethod
    def _cgroup_leaves(cls, cgroup_base: str) -> frozenset[str]:
        if not cgroup_base:
            return frozenset()

        try:
            entries = os.listdir(cgroup_base)
        except OSError:
            return frozenset()

        leaves: set[str] = set()
        for entry in entries:
            if not entry.startswith(cls.LEAF_PREFIX):
                continue

            leaves.add(entry)

        return frozenset(leaves)

    @classmethod
    def _open_fds(cls) -> int:
        return len(os.listdir(cls.FD_DIR))


class LoadStand:
    """Стенд нагрузки: образ на пользователя, профиль и caller'ы к нему."""

    WORKSPACE: ClassVar[str] = "/workspace"
    SIGNALS_MOUNT: ClassVar[str] = "/srv"
    """rw-bind под метки старта: /mnt отцепляется вместе с обвязкой образа."""

    TEMPLATE_BYTES: ClassVar[int] = 16 * 1024 * 1024

    def __init__(self, root: Path, template: Path, **profile_kw: object) -> None:
        self._root = root
        self._template = template
        self._profile_kw = profile_kw

    @property
    def root(self) -> Path:
        return self._root

    @property
    def images_dir(self) -> Path:
        return self._root / "ws"

    @property
    def signals_dir(self) -> Path:
        return self._root / "signals"

    def signal_path(self, marker: str) -> Path:
        return self.signals_dir / marker

    def started_command(self, marker: str, command: str) -> str:
        """Команда, которая перед работой отмечается в rw-bind."""
        return f"touch {self.SIGNALS_MOUNT}/{marker}; {command}"

    def wait_for_signal(
        self, marker: str, timeout_sec: float = Waiting.APPEAR_SEC
    ) -> None:
        """Ждёт метку команды: до неё образ ещё монтируется."""
        deadline = time.monotonic() + timeout_sec
        path = self.signal_path(marker)

        while time.monotonic() < deadline:
            if path.exists():
                return

            time.sleep(Waiting.POLL_SEC)

        msg = f"command {marker!r} did not start in {timeout_sec}s"
        raise AssertionError(msg)

    def image_of(self, user_id: str) -> Path:
        return self.images_dir / f"{user_id}.ext4"

    def profile(self, **overrides: object) -> SandboxProfile:
        fields: dict[str, object] = {
            "host": {
                "mounting": {
                    "mount_wait_sec": 30.0,
                    "mount_poll_sec": 0.05,
                    "shutdown_wait_sec": 5.0,
                    "lock_wait_sec": 60.0,
                    "copy_chunk_bytes": 1 << 20,
                },
                "binaries": {"dirs": self._bin_dirs()},
                "stderr_tail_bytes": 4096,
                "channel_limit_bytes": 67108864,
                "fail_tail_chars": 2000,
                "kill_grace_sec": 5,
                "cgroup_base": "",
            },
            "rootfs": str(ROOTFS_IMAGE),
            "mounts": {
                "ro": SandboxStand.image_ro_binds(),
                "rw": (f"{self.signals_dir}:{self.SIGNALS_MOUNT}",),
                "workspace": {
                    "template": str(self._template),
                    "mount": f"{self.images_dir}/{{user_id}}.ext4:{self.WORKSPACE}",
                },
                "tmp": "64M",
            },
            "isolation": {
                "network": False,
                "env": SandboxStand.image_env(),
                "reap_poll_sec": 0.05,
            },
            "limits": {
                "timeout_sec": 120,
                "process_memory_bytes": 512 * 1024 * 1024,
                "process_cpu_sec": 120,
                "process_file_bytes": 64 * 1024 * 1024,
                "process_open_files": 256,
                "process_oom_score_adj": 0,
            },
            "run": {
                "shell": "/bin/bash",
                "cwd": self.WORKSPACE,
            },
        }
        merged = ProfileFields.merged(fields, self._profile_kw)

        return SandboxProfile.model_validate(ProfileFields.merged(merged, overrides))

    ZYGOTE: ClassVar[ZygotePolicy] = ZygotePolicy(
        start_timeout_sec=60.0,
        max_start_attempts=2,
        restart_backoff_sec=0.1,
        healthy_after_sec=1.0,
        stop_wait_sec=5.0,
        call_poll_sec=0.05,
    )

    def caller(
        self,
        user_id: str,
        thread_id: str = "t1",
        **overrides: object,
    ) -> ZygoteToolCaller:
        """Зигота под набор лимитов: имя секции — ключ реестра зигот."""
        profile = self.profile(**overrides)

        def path_vars() -> dict[str, str]:
            return {"user_id": user_id, "thread_id": thread_id}

        section = f"bash-{sorted(overrides.items())}"
        supervisor = ZygoteRegistry.obtain(section, profile, (), self.ZYGOTE)
        return ZygoteToolCaller(section, supervisor, profile, path_vars)

    def storage(self) -> ImageStorageClient:
        cfg = LocalStorageConfig.model_validate(
            {
                "kind": "image",
                "mount_dir": "/tmp",  # noqa: S108
                "workspace": {
                    "template": str(self._template),
                    "mount": f"{self.images_dir}/{{user_id}}.ext4:{self.WORKSPACE}",
                },
                "op_timeout_sec": 120,
                "mounting": {
                    "mount_wait_sec": 30.0,
                    "mount_poll_sec": 0.05,
                    "shutdown_wait_sec": 5.0,
                    "lock_wait_sec": 60.0,
                    "copy_chunk_bytes": 1 << 20,
                },
                "binaries": {"dirs": self._bin_dirs()},
            }
        )
        client = StorageFactory.create(cfg)
        if not (isinstance(client, ImageStorageClient)):
            raise AssertionError("isinstance(client, ImageStorageClient)")
        return client

    def warm(self, **overrides: object) -> None:
        """Поднять зиготу секции до снимка ресурсов: она живёт между вызовами."""
        self.caller("warm", "t1", **overrides)

    def census(self, cgroup_base: str = "") -> ResourceCensus:
        return ResourceCensus.capture(self._root, cgroup_base)

    def settle(self, before: ResourceCensus, cgroup_base: str = "") -> ResourceLeak:
        return ResourceCensus.settle(before, self._root, cgroup_base)

    def wait_for_daemon(self, timeout_sec: float = Waiting.APPEAR_SEC) -> int:
        """Ждёт fuse2fs стенда: до него убивать нечего."""
        deadline = time.monotonic() + timeout_sec

        while time.monotonic() < deadline:
            daemons = ProcTable.matching(ProcName.FUSE2FS, _IMAGES_MOUNT)
            if daemons:
                return next(iter(daemons))

            time.sleep(Waiting.POLL_SEC)

        msg = f"fuse2fs for {self._root} did not appear in {timeout_sec}s"
        raise AssertionError(msg)

    def wait_for_bwrap(self, timeout_sec: float = Waiting.APPEAR_SEC) -> int:
        deadline = time.monotonic() + timeout_sec

        while time.monotonic() < deadline:
            found = ProcTable.matching(ProcName.BWRAP, str(self._root))
            if found:
                return min(found)

            time.sleep(Waiting.POLL_SEC)

        msg = f"bwrap for {self._root} did not appear in {timeout_sec}s"
        raise AssertionError(msg)

    @staticmethod
    def _bin_dirs() -> list[str]:
        """В тестах каталоги даёт стенд; в проде их задаёт конфиг."""
        return SandboxStand.bin_dirs()


@pytest.fixture(autouse=True)
def chainlit_context() -> None:
    """Нагрузка не зависит от сессии chainlit."""


@pytest.fixture
def template(tmp_path: Path) -> Path:
    """Шаблонный ext4-образ; без журнала — fuse2fs пишет только так."""
    path = tmp_path / "template.ext4"
    with path.open("wb") as f:
        f.truncate(LoadStand.TEMPLATE_BYTES)

    mkfs = shutil.which("mkfs.ext4")
    if mkfs is None:
        raise AssertionError("mkfs is not None")
    subprocess.run(  # noqa: S603
        [mkfs, "-F", "-q", "-O", "^has_journal", "-m", "0", str(path)],
        check=True,
    )
    return path


@pytest.fixture
def stand(tmp_path: Path, template: Path) -> Iterator[LoadStand]:
    """Зиготы гасятся вместе со стендом: реестр общий на процесс тестов."""
    stand = LoadStand(tmp_path / "stand", template)
    stand.images_dir.mkdir(parents=True, exist_ok=True)
    stand.signals_dir.mkdir(parents=True, exist_ok=True)

    try:
        yield stand
    finally:
        ZygoteRegistry.stop_all()


@needs_fuse
class TestParallelLoad:
    """Много пользователей и потоков: результат верный, ресурсы освобождены."""

    @staticmethod
    def _write_and_read(stand: LoadStand, user_id: str, index: int) -> LaunchOutcome:
        name = f"u{user_id}-{index}.txt"
        command = f"echo {user_id}-{index} > {name}; cat {name}"
        return stand.caller(user_id).call_text(command, stdin="")

    def test_many_users_release_everything(self, stand: LoadStand) -> None:
        stand.warm()
        before = stand.census()
        jobs: list[tuple[str, int]] = []

        for user in range(LoadScale.USERS):
            for index in range(LoadScale.CALLS):
                jobs.append((str(user), index))

        with ThreadPoolExecutor(max_workers=LoadScale.THREADS) as pool:
            futures = [
                pool.submit(self._write_and_read, stand, user, index)
                for user, index in jobs
            ]
            outcomes = [future.result() for future in futures]

        for (user, index), outcome in zip(jobs, outcomes, strict=True):
            if outcome.result.exit_code != 0:
                raise AssertionError(outcome.result.stderr)
            if f"{user}-{index}" not in outcome.result.stdout:
                raise AssertionError('f"{user}-{index}" in outcome.result.stdout')

        leak = stand.settle(before)
        if not (leak.empty):
            raise AssertionError(leak.describe())

    def test_every_user_gets_own_image(self, stand: LoadStand) -> None:
        with ThreadPoolExecutor(max_workers=LoadScale.THREADS) as pool:
            futures = [
                pool.submit(self._write_and_read, stand, str(user), 0)
                for user in range(LoadScale.USERS)
            ]
            for future in futures:
                if future.result().result.exit_code != 0:
                    raise AssertionError("future.result().result.exit_code == 0")

        for user in range(LoadScale.USERS):
            if not (stand.image_of(str(user)).exists()):
                raise AssertionError("stand.image_of(str(user)).exists()")

        listing = stand.caller("0").call_text(f"ls {LoadStand.WORKSPACE}", stdin="")
        if "u0-0.txt" not in listing.result.stdout:
            raise AssertionError('"u0-0.txt" in listing.result.stdout')
        if "u1-0.txt" in listing.result.stdout:
            raise AssertionError('"u1-0.txt" not in listing.result.stdout')

    def test_one_image_shared_by_threads_keeps_all_writes(
        self, stand: LoadStand
    ) -> None:
        """Один пользователь, много потоков: вызовы сериализуются локом."""
        stand.warm()
        before = stand.census()

        with ThreadPoolExecutor(max_workers=LoadScale.THREADS) as pool:
            futures = [
                pool.submit(self._write_and_read, stand, "shared", index)
                for index in range(LoadScale.THREADS)
            ]
            for future in futures:
                if future.result().result.exit_code != 0:
                    raise AssertionError("future.result().result.exit_code == 0")

        listing = stand.caller("shared").call_text(
            f"ls {LoadStand.WORKSPACE}", stdin=""
        )
        for index in range(LoadScale.THREADS):
            if f"ushared-{index}.txt" not in listing.result.stdout:
                raise AssertionError('f"ushared-{index}.txt" in listing.result.stdout')

        leak = stand.settle(before)
        if not (leak.empty):
            raise AssertionError(leak.describe())

    MIX_USER: ClassVar[str] = "mix"
    MIX_CALLS: ClassVar[int] = 4

    @classmethod
    def _attachment_key(cls, index: int) -> str:
        """Ключ вложения: первый сегмент — пользователь, дальше путь в образе."""
        return f"{cls.MIX_USER}/t1/upload/file-{index}.txt"

    def test_storage_and_sandbox_share_image_under_load(self, stand: LoadStand) -> None:
        """Вложения и bash работают с одним образом: flock их разводит."""
        stand.warm()
        before = stand.census()
        storage = stand.storage()

        def upload(index: int) -> None:
            payload = f"attachment-{index}".encode()
            asyncio.run(storage.upload_file(self._attachment_key(index), payload))

        def shell(index: int) -> LaunchOutcome:
            return self._write_and_read(stand, self.MIX_USER, index)

        with ThreadPoolExecutor(max_workers=LoadScale.THREADS) as pool:
            uploads = [pool.submit(upload, index) for index in range(self.MIX_CALLS)]
            shells = [pool.submit(shell, index) for index in range(self.MIX_CALLS)]
            for future in uploads:
                future.result()
            for future in shells:
                if future.result().result.exit_code != 0:
                    raise AssertionError("future.result().result.exit_code == 0")

        for index in range(self.MIX_CALLS):
            body = asyncio.run(self._read_all(storage, self._attachment_key(index)))
            if body != f"attachment-{index}".encode():
                raise AssertionError('body == f"attachment-{index}".encode()')

        leak = stand.settle(before)
        if not (leak.empty):
            raise AssertionError(leak.describe())

    @staticmethod
    async def _read_all(storage: ImageStorageClient, object_key: str) -> bytes:
        collected = bytearray()

        async with await storage.open_stream(object_key, ReadWindow.entire()) as body:
            async for chunk in body.chunks:
                collected.extend(chunk)

        return bytes(collected)

    def test_repeated_calls_do_not_leak_descriptors(self, stand: LoadStand) -> None:
        """Дескрипторы и локи не накапливаются на серии вызовов."""
        stand.caller("fd").call_text("true", stdin="")
        before = stand.census()

        for index in range(LoadScale.REPEATS):
            outcome = stand.caller("fd").call_text(f"echo {index}", stdin="")
            if outcome.result.exit_code != 0:
                raise AssertionError("outcome.result.exit_code == 0")

        leak = stand.settle(before)
        if not (leak.empty):
            raise AssertionError(leak.describe())


class CallReport(BaseModel):
    """Исход вызова под убийствами: код возврата либо текст сбоя раннера."""

    model_config = ConfigDict(frozen=True)

    exit_code: int | None
    stdout: str
    failure: str

    MOUNT_FAILURE: ClassVar[str] = "image not mounted"

    @classmethod
    def of(cls, outcome: LaunchOutcome) -> CallReport:
        return cls(
            exit_code=outcome.result.exit_code,
            stdout=outcome.result.stdout,
            failure="",
        )

    @classmethod
    def failed(cls, exc: BaseException) -> CallReport:
        return cls(exit_code=None, stdout="", failure=str(exc))

    @property
    def mount_lost(self) -> bool:
        """Образ не смонтировался: демона убили до конца монтирования."""
        return self.MOUNT_FAILURE in self.failure


@needs_fuse
class TestAbnormalTermination:
    """Аварийные исходы вызова: таймаут, отмена, убийство процессов."""

    LONG_COMMAND: ClassVar[str] = "touch busy.txt; sleep 300"

    @staticmethod
    def _report(caller: ZygoteToolCaller, command: str) -> CallReport:
        """Смерть цепочки — тоже исход вызова, а не поломка стенда."""
        try:
            return CallReport.of(caller.call_text(command, stdin=""))
        except LauncherError as exc:
            return CallReport.failed(exc)

    def test_timeout_kills_command_and_frees_image(self, stand: LoadStand) -> None:
        stand.warm()
        stand.warm(timeout_sec=1)
        before = stand.census()

        outcome = stand.caller("timeout", timeout_sec=1).call_text(
            self.LONG_COMMAND, stdin=""
        )

        if outcome.result.timed_out is not True:
            raise AssertionError("outcome.result.timed_out is True")
        leak = stand.settle(before)
        if not (leak.empty):
            raise AssertionError(leak.describe())

        again = stand.caller("timeout").call_text("echo alive", stdin="")
        if again.result.exit_code != 0:
            raise AssertionError("again.result.exit_code == 0")
        if "alive" not in again.result.stdout:
            raise AssertionError('"alive" in again.result.stdout')

    def test_cancelled_turn_frees_image(self, stand: LoadStand) -> None:
        """Остановка хода посреди работы команды: образ и демон отпущены."""
        stand.warm()
        before = stand.census()
        marker = f"cancel-load-{uuid4().hex[:8]}"
        command = stand.started_command(marker, self.LONG_COMMAND)

        with turn_cancellation() as cancellation:
            stopper = _Stopper(stand, cancellation, marker)
            stopper.start()
            with pytest.raises(ToolStopped):
                stand.caller("cancel").call_text(command, stdin="")
            stopper.join()

        leak = stand.settle(before)
        if not (leak.empty):
            raise AssertionError(leak.describe())

        again = stand.caller("cancel").call_text("echo alive", stdin="")
        if again.result.exit_code != 0:
            raise AssertionError("again.result.exit_code == 0")

    def test_failing_output_consumer_frees_image(self, stand: LoadStand) -> None:
        """Потребитель потока падает: процесс добивается, образ отпускается."""
        stand.warm()
        before = stand.census()

        caller = stand.caller("sink")
        ToolChannelsTap.set(BrokenSinks())
        try:
            with pytest.raises(RuntimeError, match="consumer is broken"):
                caller.call_text("echo noise; sleep 300", stdin="")
        finally:
            ToolChannelsTap.set(None)

        leak = stand.settle(before)
        if not (leak.empty):
            raise AssertionError(leak.describe())

        again = stand.caller("sink").call_text("echo alive", stdin="")
        if again.result.exit_code != 0:
            raise AssertionError("again.result.exit_code == 0")

    def test_killed_bwrap_frees_image(self, stand: LoadStand) -> None:
        """SIGKILL bwrap зиготы: её вызовы гаснут, секция поднимается заново."""
        stand.warm()
        before = stand.census()
        marker = f"bwrap-load-{uuid4().hex[:8]}"
        command = stand.started_command(marker, self.LONG_COMMAND)

        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self._report, stand.caller("bwrap"), command)
            stand.wait_for_signal(marker)
            pid = stand.wait_for_bwrap()
            daemons = ProcTable.matching(ProcName.FUSE2FS, _IMAGES_MOUNT)
            os.kill(pid, signal.SIGKILL)
            report = future.result(timeout=Waiting.APPEAR_SEC)

        if report.exit_code == 0:
            raise AssertionError("report.exit_code != 0")

        survivors = ProcTable.wait_gone(tuple(daemons), Waiting.SETTLE_SEC)
        if survivors != ():
            raise AssertionError(f"fuse2fs survived its bwrap: {survivors}")

        leak = stand.settle(before)
        if not (leak.empty):
            raise AssertionError(leak.describe())

        again = stand.caller("bwrap").call_text("echo alive", stdin="")
        if again.result.exit_code != 0:
            raise AssertionError("again.result.exit_code == 0")

    def test_killed_fuse_daemon_does_not_hang_next_call(self, stand: LoadStand) -> None:
        """SIGKILL смонтированному fuse2fs: команда теряет точку, вызов не висит."""
        stand.warm()
        before = stand.census()
        marker = f"fuse-load-{uuid4().hex[:8]}"
        watch = (
            f"while ls {LoadStand.WORKSPACE} > /dev/null 2>&1; do sleep 0.2; done; "
            f"echo lost-mount"
        )
        command = stand.started_command(marker, watch)

        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self._report, stand.caller("fuse"), command)
            stand.wait_for_signal(marker)
            pid = stand.wait_for_daemon()
            os.kill(pid, signal.SIGKILL)
            report = future.result(timeout=Waiting.APPEAR_SEC)

        if "lost-mount" not in report.stdout:
            raise AssertionError('"lost-mount" in report.stdout')
        leak = stand.settle(before)
        if not (leak.empty):
            raise AssertionError(leak.describe())

        again = stand.caller("fuse").call_text("echo alive", stdin="")
        if again.result.exit_code != 0:
            raise AssertionError("again.result.exit_code == 0")
        if "alive" not in again.result.stdout:
            raise AssertionError('"alive" in again.result.stdout')

    def test_killed_host_process_leaves_nothing_behind(
        self, stand: LoadStand, tmp_path: Path
    ) -> None:
        """SIGKILL всему приложению: образ отпущен, демоны и точки прибраны."""
        stand.warm()
        before = stand.census()
        child = _ChildCall(stand, tmp_path)
        marker = f"host-load-{uuid4().hex[:8]}"

        proc = child.start(stand.started_command(marker, self.LONG_COMMAND))
        try:
            stand.wait_for_signal(marker)
            daemons = ProcTable.matching(ProcName.FUSE2FS, _IMAGES_MOUNT)
            if not (daemons):
                raise AssertionError("fuse2fs of the child call was not found")
            proc.kill()
            proc.wait(timeout=Waiting.SETTLE_SEC)
        finally:
            child.cleanup(proc)

        survivors = ProcTable.wait_gone(tuple(daemons), Waiting.SETTLE_SEC)
        if survivors != ():
            raise AssertionError(f"fuse2fs outlived the killed host: {survivors}")

        leak = stand.settle(before)
        if not (leak.empty):
            raise AssertionError(leak.describe())

        again = stand.caller(_ChildCall.USER).call_text("echo alive", stdin="")
        if again.result.exit_code != 0:
            raise AssertionError("again.result.exit_code == 0")
        if "alive" not in again.result.stdout:
            raise AssertionError('"alive" in again.result.stdout')

    def test_killed_host_process_keeps_image_usable(
        self, stand: LoadStand, tmp_path: Path
    ) -> None:
        """Убийство посреди записи не рвёт образ: следующий вызов его читает."""
        child = _ChildCall(stand, tmp_path)
        marker = f"usable-load-{uuid4().hex[:8]}"
        write = "echo written > survivor.txt; sync; sleep 300"

        proc = child.start(stand.started_command(marker, write))
        try:
            stand.wait_for_signal(marker)
            proc.kill()
            proc.wait(timeout=Waiting.SETTLE_SEC)
        finally:
            child.cleanup(proc)

        listing = stand.caller(_ChildCall.USER).call_text(
            f"ls {LoadStand.WORKSPACE}", stdin=""
        )
        if listing.result.exit_code != 0:
            raise AssertionError("listing.result.exit_code == 0")

        written = stand.caller(_ChildCall.USER).call_text("echo ok", stdin="")
        if written.result.exit_code != 0:
            raise AssertionError("written.result.exit_code == 0")

    def test_parallel_load_survives_random_kills(self, stand: LoadStand) -> None:
        """Нагрузка вперемешку с убийствами: уцелевшие вызовы честны, мусора нет."""
        stand.warm()
        before = stand.census()

        def call(index: int) -> CallReport:
            name = f"kill-{index}.txt"
            command = f"echo {index} > {name}; sleep 2; cat {name}"
            return self._report(stand.caller(str(index % 4)), command)

        with ThreadPoolExecutor(max_workers=LoadScale.THREADS) as pool:
            futures = [pool.submit(call, index) for index in range(LoadScale.THREADS)]
            stand.wait_for_daemon()
            self._kill_some_daemons(stand)
            reports = [future.result(timeout=Waiting.APPEAR_SEC) for future in futures]

        for index, report in enumerate(reports):
            if report.mount_lost:
                continue

            if report.failure != "":
                raise AssertionError(report.failure)
            if report.exit_code != 0:
                continue

            if report.stdout.strip() != str(index):
                raise AssertionError("report.stdout.strip() == str(index)")

        leak = stand.settle(before)
        if not (leak.empty):
            raise AssertionError(leak.describe())

    @staticmethod
    def _kill_some_daemons(stand: LoadStand) -> None:
        """Половина живых демонов уходит по SIGKILL, остальные работают."""
        daemons = sorted(ProcTable.matching(ProcName.FUSE2FS, _IMAGES_MOUNT))

        for pid in daemons[::2]:
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)


class _Stopper:
    """Останавливает ход, как только команда отметилась в rw-bind."""

    def __init__(
        self, stand: LoadStand, cancellation: TurnCancellation, marker: str
    ) -> None:
        self._stand = stand
        self._cancellation = cancellation
        self._marker = marker
        self._pool = ThreadPoolExecutor(max_workers=1)
        self._future: Future[None] | None = None

    def start(self) -> None:
        self._future = self._pool.submit(self._run)

    def join(self) -> None:
        if self._future is not None:
            self._future.result(timeout=Waiting.APPEAR_SEC)

        self._pool.shutdown(wait=True)

    def _run(self) -> None:
        self._stand.wait_for_signal(self._marker)
        self._cancellation.cancel()


class _ChildCall:
    """Вызов песочницы в отдельном процессе: его можно убить целиком."""

    USER: ClassVar[str] = "host"
    SCRIPT_NAME: ClassVar[str] = "child_call.py"
    READY_SEC: ClassVar[float] = 30.0

    SOURCE: ClassVar[str] = '''
"""Один вызов песочницы; профиль и команда приходят аргументами."""

import json
import sys

from boba.sandbox import SandboxProfile
from boba.sandbox.zygote import (
    ZygotePolicy,
    ZygoteRegistry,
    ZygoteSpawner,
    ZygoteToolCaller,
)

profile = SandboxProfile.model_validate_json(sys.argv[1])
user_id = sys.argv[2]
command = sys.argv[3]


def path_vars():
    return {"user_id": user_id, "thread_id": "t1"}


print("ready", flush=True)
policy = ZygotePolicy(
    start_timeout_sec=60.0,
    max_start_attempts=2,
    restart_backoff_sec=0.1,
    healthy_after_sec=1.0,
    stop_wait_sec=5.0,
    call_poll_sec=0.05,
)
supervisor = ZygoteRegistry.obtain("bash", profile, (), policy)
caller = ZygoteToolCaller("bash", supervisor, profile, path_vars)
outcome = caller.call_text(command, stdin="")
print(json.dumps({"rc": outcome.result.exit_code}), flush=True)
'''

    def __init__(self, stand: LoadStand, tmp_path: Path) -> None:
        self._stand = stand
        self._script = tmp_path / f"{uuid4().hex[:8]}-{self.SCRIPT_NAME}"
        self._script.write_text(self.SOURCE, encoding="utf-8")

    def start(self, command: str) -> subprocess.Popen[bytes]:
        profile_json = self._stand.profile().model_dump_json()
        env = dict(os.environ)
        env["PYTHONPATH"] = self._pythonpath()

        proc = subprocess.Popen(  # noqa: S603
            [
                sys.executable,
                str(self._script),
                profile_json,
                self.USER,
                command,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )
        self._wait_ready(proc)
        return proc

    def cleanup(self, proc: subprocess.Popen[bytes]) -> None:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=Waiting.SETTLE_SEC)

        if proc.stdout is not None:
            proc.stdout.close()

        if proc.stderr is not None:
            proc.stderr.close()

    def _wait_ready(self, proc: subprocess.Popen[bytes]) -> None:
        """Первая строка stdout — готовность: до неё вызов ещё не начался."""
        if proc.stdout is None:
            msg = "child call started without stdout pipe"
            raise AssertionError(msg)

        line = proc.stdout.readline()
        if line.strip() == b"ready":
            return

        stderr = b""
        if proc.stderr is not None:
            stderr = proc.stderr.read()

        msg = f"child call did not start: {line!r} {stderr!r}"
        raise AssertionError(msg)

    @staticmethod
    def _pythonpath() -> str:
        entries: list[str] = []

        for entry in sys.path:
            if not entry:
                continue

            entries.append(entry)

        return os.pathsep.join(entries)


@needs_fuse
@needs_delegation
class TestGroupLimitsUnderLoad:
    """cgroup-leaf'ы живут ровно один вызов и не накапливаются под нагрузкой."""

    @pytest.fixture
    def cgroup_base(self) -> Iterator[str]:
        path = os.path.join(CgroupZone.find(), f"boba-load-{uuid4().hex[:8]}")
        yield path

        CgroupManager._prepared.pop(path, None)
        with contextlib.suppress(OSError):
            os.rmdir(path)

    def test_leaves_removed_after_parallel_calls(
        self, stand: LoadStand, cgroup_base: str
    ) -> None:
        stand.warm(
            cgroup_base=cgroup_base,
            group_memory_bytes=512 * 1024 * 1024,
            group_pids_max=64,
            group_swap_bytes=0,
            group_oom_kill_all=True,
        )
        before = stand.census(cgroup_base)

        def call(index: int) -> LaunchOutcome:
            caller = stand.caller(
                str(index),
                cgroup_base=cgroup_base,
                group_memory_bytes=512 * 1024 * 1024,
                group_pids_max=64,
                group_swap_bytes=0,
                group_oom_kill_all=True,
            )
            return caller.call_text(f"echo {index}", stdin="")

        with ThreadPoolExecutor(max_workers=LoadScale.THREADS) as pool:
            futures = [pool.submit(call, index) for index in range(LoadScale.THREADS)]
            for future in futures:
                if future.result().result.exit_code != 0:
                    raise AssertionError("future.result().result.exit_code == 0")

        leak = stand.settle(before, cgroup_base)
        if not (leak.empty):
            raise AssertionError(leak.describe())

    def test_group_oom_releases_leaf(self, stand: LoadStand, cgroup_base: str) -> None:
        """Группа упирается в memory.max: её убивают, leaf исчезает."""
        stand.warm(
            cgroup_base=cgroup_base,
            group_memory_bytes=64 * 1024 * 1024,
            group_swap_bytes=0,
            group_oom_kill_all=True,
            process_memory_bytes=1024 * 1024 * 1024,
        )
        before = stand.census(cgroup_base)
        caller = stand.caller(
            "oom",
            cgroup_base=cgroup_base,
            group_memory_bytes=64 * 1024 * 1024,
            group_swap_bytes=0,
            group_oom_kill_all=True,
            process_memory_bytes=1024 * 1024 * 1024,
        )

        # tail держит окно в памяти целиком: 256 МБ против лимита в 64 МБ
        outcome = caller.call_text(
            "head -c 256M /dev/zero | tail -c 256M > /dev/null", stdin=""
        )

        if outcome.result.exit_code == 0:
            raise AssertionError("outcome.result.exit_code != 0")
        leak = stand.settle(before, cgroup_base)
        if not (leak.empty):
            raise AssertionError(leak.describe())

    def test_pids_limit_holds_under_fork_pressure(
        self, stand: LoadStand, cgroup_base: str
    ) -> None:
        """pids.max держит форк-бомбу и не оставляет leaf после вызова."""
        stand.warm(
            cgroup_base=cgroup_base,
            group_pids_max=32,
            timeout_sec=30,
        )
        before = stand.census(cgroup_base)
        caller = stand.caller(
            "pids",
            cgroup_base=cgroup_base,
            group_pids_max=32,
            timeout_sec=30,
        )

        outcome = caller.call_text(
            "bomb() { bomb | bomb & }; bomb; sleep 5; echo survived", stdin=""
        )

        if not ("survived" in outcome.result.stdout or outcome.result.exit_code != 0):
            raise AssertionError('"survived" in outcome.result.stdout or outcome.resu…')
        leak = stand.settle(before, cgroup_base)
        if not (leak.empty):
            raise AssertionError(leak.describe())


@needs_fuse
class TestCrashDebris:
    """Мусор, оставшийся от прежней аварии, убирается при взятии лока."""

    DEAD_PID: ClassVar[int] = 999_999
    """Pid, которого нет: так выглядит копия, брошенная умершим процессом."""

    OWN_PID: ClassVar[int] = 1
    """Pid исполнителя внутри его pid namespace: у каждого вызова он равен 1."""

    def test_partial_copy_from_a_crash_is_removed(self, stand: LoadStand) -> None:
        """Копию без живого владельца никто не докопирует — она удаляется."""
        image = stand.image_of("debris")
        partial = Path(PartialCopy.render(str(image), self.DEAD_PID))
        partial.write_bytes(b"broken copy")

        outcome = stand.caller("debris").call_text("echo alive", stdin="")

        if outcome.result.exit_code != 0:
            raise AssertionError("outcome.result.exit_code == 0")
        if not (image.exists()):
            raise AssertionError("image.exists()")
        if partial.exists():
            raise AssertionError("брошенная частичная копия должна быть убрана")

    def test_partial_copy_with_a_live_pid_is_removed(self, stand: LoadStand) -> None:
        """Pid копии ничего не значит: под эксклюзивным локом владельца нет."""
        image = stand.image_of("live-debris")
        partial = Path(PartialCopy.render(str(image), self.OWN_PID))
        partial.parent.mkdir(parents=True, exist_ok=True)
        partial.write_bytes(b"copy in progress")

        outcome = stand.caller("live-debris").call_text("echo alive", stdin="")

        if outcome.result.exit_code != 0:
            raise AssertionError("outcome.result.exit_code == 0")
        if partial.exists():
            raise AssertionError("копия под чужим pid всё равно должна быть убрана")

    def test_abandoned_lock_file_does_not_block_calls(self, stand: LoadStand) -> None:
        """Файл лока переживает вызовы: значение имеет только сам flock."""
        first = stand.caller("lock").call_text("echo first", stdin="")
        lock = stand.image_of("lock").with_suffix(".ext4.lock")

        if first.result.exit_code != 0:
            raise AssertionError("first.result.exit_code == 0")
        if not (lock.exists()):
            raise AssertionError("lock.exists()")

        second = stand.caller("lock").call_text("echo second", stdin="")

        if second.result.exit_code != 0:
            raise AssertionError("second.result.exit_code == 0")
        if "second" not in second.result.stdout:
            raise AssertionError('"second" in second.result.stdout')


@needs_fuse
class TestChildCallHarness:
    """Проверка самой обвязки: дочерний процесс действительно делает вызов."""

    def test_child_script_reports_return_code(
        self, stand: LoadStand, tmp_path: Path
    ) -> None:
        child = _ChildCall(stand, tmp_path)
        proc = child.start("echo done")

        try:
            stdout, _ = proc.communicate(timeout=Waiting.APPEAR_SEC)
        finally:
            child.cleanup(proc)

        lines = stdout.decode().strip().splitlines()
        if json.loads(lines[-1])["rc"] != 0:
            raise AssertionError('json.loads(lines[-1])["rc"] == 0')
