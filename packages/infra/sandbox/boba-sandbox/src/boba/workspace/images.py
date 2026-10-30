"""Образы ext4 песочницы: развёртка шаблона gzip под flock и монтирование fuse2fs.

Исполняется внутри песочницы — лаунчером цепочки и ребёнком зиготы, поэтому
живёт в toolkit: пакет песочницы в образ не попадает. Ход монтирования
трассируется в stderr кадрами `sandbox-mount:`, которые релей хоста
поднимает в журнал приложения.

Ошибки:
MountError — сбой подготовки или монтирования образа.
OSError — копирование или лок образа отказали на уровне файловой системы.
"""

from __future__ import annotations

import contextlib
import ctypes
import fcntl
import gzip
import os
import signal
import subprocess
import sys
import time
import zlib
from collections.abc import Iterator
from dataclasses import dataclass
from enum import StrEnum
from typing import BinaryIO, ClassVar

from boba.workspace.binaries import SandboxBinary, TrustedBinaries

__all__ = [
    "FuseMounter",
    "ImageStore",
    "LauncherMarker",
    "LauncherOptions",
    "MountError",
    "PartialCopy",
    "SparseCopier",
    "trace",
]


class LauncherMarker(StrEnum):
    """Маркеры строк stderr: хост отличает трассировку и сбой от чужого шума."""

    LOG = "sandbox-mount: "
    ERROR = "sandbox-mount-error: "


def trace(message: str) -> None:
    """Ход монтирования — только в stderr: stdout занят данными операции."""
    print(f"{LauncherMarker.LOG}{message}", file=sys.stderr, flush=True)  # noqa: T201


class MountError(RuntimeError):
    """Сбой подготовки или монтирования образа."""

    EXIT_CODE: ClassVar[int] = 2
    """Код выхода процесса, который не смог смонтировать образ."""


class SparseCopier:
    """Разворачивает шаблон образа, сжатый gzip, в разреженный файл.

    Нулевые блоки шаблона остаются дырами: пустой ext4 на гигабайт занимает
    на диске только свои метаданные. Шаблоны workspace и прочих rw-образов
    лежат сжатыми; ImageStore зовёт развёртку один раз на образ.
    """

    BLOCK: ClassVar[int] = 4096
    """Гранулярность дыр: блок ext4 шаблона."""

    def __init__(self, chunk_bytes: int) -> None:
        self._chunk = chunk_bytes
        self._zero = b"\0" * max(chunk_bytes, self.BLOCK)

    def copy(self, src: str, dst: str) -> None:
        try:
            with gzip.open(src, "rb") as fin, open(dst, "wb") as fout:
                size = self._unpack(fin, fout)
                fout.truncate(size)
        except (gzip.BadGzipFile, EOFError, zlib.error) as exc:
            msg = (
                f"unpacking template {src!r} into {dst!r}: expected a gzip "
                f"stream, got: {exc}"
            )
            raise MountError(msg) from exc

    def _unpack(self, fin: gzip.GzipFile, fout: BinaryIO) -> int:
        offset = 0
        while chunk := fin.read(self._chunk):
            self._write_data(fout, offset, chunk)
            offset += len(chunk)

        return offset

    def _write_data(self, fout: BinaryIO, offset: int, chunk: bytes) -> None:
        """Ненулевые блоки куска chunk со смещения offset; нулевые — дыры."""
        if chunk == self._zero[: len(chunk)]:
            return

        view = memoryview(chunk)
        for start in range(0, len(chunk), self.BLOCK):
            block = view[start : start + self.BLOCK]
            if block == self._zero[: len(block)]:
                continue

            fout.seek(offset + start)
            fout.write(block)


class PartialCopy:
    """Имя недокопированного образа: `<image>.tmp.<pid>` владельца копии."""

    SUFFIX: ClassVar[str] = ".tmp."

    @classmethod
    def render(cls, image: str, pid: int) -> str:
        return f"{image}{cls.SUFFIX}{pid}"

    @classmethod
    def owner_of(cls, image: str, path: str) -> int | None:
        """Pid из имени; None — имя не похоже на частичную копию образа."""
        prefix = f"{image}{cls.SUFFIX}"
        if not path.startswith(prefix):
            return None

        tail = path[len(prefix) :]
        if not tail.isdigit():
            return None

        return int(tail)

    @classmethod
    def abandoned(cls, image: str) -> Iterator[str]:
        """Копии образа, найденные под его эксклюзивным локом.

        Копирование идёт только под этим локом, поэтому живого владельца у
        найденной копии быть не может. Pid в имени остаётся следом для
        разбора: у исполнителя вызова он свой на namespace и равен 1.
        """
        directory = os.path.dirname(image)
        try:
            names = os.listdir(directory)
        except OSError:
            return

        for name in names:
            path = os.path.join(directory, name)
            owner = cls.owner_of(image, path)
            if owner is None:
                continue

            yield path


class ImageStore:
    """Готовит образы: flock сериализует доступ, шаблон копируется однажды."""

    LOCK_SUFFIX: ClassVar[str] = ".lock"
    LOCK_POLL_SEC: ClassVar[float] = 0.05

    def __init__(
        self, template: str, copier: SparseCopier, lock_wait_sec: float
    ) -> None:
        self._template = template
        self._copier = copier
        self._lock_wait_sec = lock_wait_sec
        self._locks: dict[str, int] = {}

    @property
    def lock_fds(self) -> tuple[int, ...]:
        return tuple(self._locks.values())

    def acquire(self, image: str) -> None:
        started = time.monotonic()
        self._lock(image, fcntl.LOCK_EX)
        waited_ms = int((time.monotonic() - started) * 1000)
        trace(f"lock on {image} acquired in {waited_ms}ms")

        self._drop_abandoned(image)

        if os.path.exists(image):
            trace(f"image {image} already exists ({os.path.getsize(image)} bytes)")
            return

        try:
            self._materialize(image)
        except BaseException:
            self.release(image)
            raise

    def acquire_shared(self, image: str) -> bool:
        """Разделяемый лок для чтения; False — образа ещё нет, читать нечего."""
        started = time.monotonic()
        self._lock(image, fcntl.LOCK_SH)
        waited_ms = int((time.monotonic() - started) * 1000)
        trace(f"shared lock on {image} acquired in {waited_ms}ms")

        return os.path.exists(image)

    def release(self, image: str) -> None:
        fd = self._locks.pop(image, None)
        if fd is None:
            return

        os.close(fd)

    def release_all(self) -> None:
        for fd in self._locks.values():
            os.close(fd)
        self._locks.clear()

    def _lock(self, image: str, operation: int) -> None:
        if image in self._locks:
            return

        fd = os.open(image + self.LOCK_SUFFIX, os.O_WRONLY | os.O_CREAT, 0o600)

        try:
            self._flock_wait(fd, operation, image)
        except BaseException:
            os.close(fd)
            raise

        os.set_inheritable(fd, True)
        self._locks[image] = fd

    def _flock_wait(self, fd: int, operation: int, image: str) -> None:
        """flock с таймаутом: вечное ожидание чужого лока — это зависание."""
        deadline = time.monotonic() + self._lock_wait_sec

        while True:
            try:
                fcntl.flock(fd, operation | fcntl.LOCK_NB)
                return
            except BlockingIOError as exc:
                if time.monotonic() >= deadline:
                    msg = (
                        f"lock {image + self.LOCK_SUFFIX} is held by another "
                        f"process: not acquired within {self._lock_wait_sec}s"
                    )
                    raise MountError(msg) from exc

            time.sleep(self.LOCK_POLL_SEC)

    def _drop_abandoned(self, image: str) -> None:
        """Под своим локом чужая частичная копия — только от умершего процесса."""
        for path in PartialCopy.abandoned(image):
            try:
                os.remove(path)
            except OSError as exc:
                trace(f"cannot remove abandoned copy {path}: {exc}")
                continue

            trace(f"abandoned partial copy removed: {path}")

    def _materialize(self, image: str) -> None:
        if not os.path.exists(self._template):
            msg = (
                f"cannot create image {image!r}: template image "
                f"{self._template!r} not found"
            )
            raise MountError(msg)

        tmp = PartialCopy.render(image, os.getpid())
        started = time.monotonic()
        try:
            self._copier.copy(self._template, tmp)
            os.rename(tmp, image)
            elapsed_ms = int((time.monotonic() - started) * 1000)
            trace(f"image {image} created from {self._template} in {elapsed_ms}ms")
        except BaseException:
            with contextlib.suppress(FileNotFoundError):
                os.remove(tmp)
            raise


class FuseMounter:
    """fuse2fs-демоны: запуск с pdeathsig, ожидание монтирования, гашение."""

    MOUNTINFO: ClassVar[str] = "/proc/self/mountinfo"
    _PR_SET_PDEATHSIG: ClassVar[int] = 1

    def __init__(
        self,
        options: LauncherOptions,
        binaries: TrustedBinaries,
        pass_fds: tuple[int, ...] = (),
    ) -> None:
        self._options = options
        self._binaries = binaries
        self._pass_fds = pass_fds
        self._daemons: list[subprocess.Popen[bytes]] = []

    READONLY_OPTIONS: ClassVar[str] = "ro,norecovery"
    """Чтение под разделяемым локом: replay журнала писал бы в образ."""

    FAKEROOT_OPTIONS: ClassVar[str] = "fakeroot"
    """Запись без userns: права внутри образа проверяются как у root."""

    NEUTRAL_OPTIONS: ClassVar[str] = (
        "no_default_opts,fsname=rootfs,subtype=ext4,kernel_cache,attr_timeout=0"
    )
    """Источник монтирования в /proc/mounts и mountinfo нейтрален.

    По умолчанию fuse2fs ставит fsname = путь образа и выдаёт телу раскладку
    хоста (/mnt/images/…, путь rootfs.ext4). no_default_opts отключает этот
    набор, а остальное повторяет его дефолты (kernel_cache, attr_timeout);
    use_ino/big_writes в libfuse3 не нужны."""

    def mount(
        self, image: str, mnt: str, *, readonly: bool, fakeroot: bool = False
    ) -> None:
        # пути приезжают из argv лаунчера: относительный или начинающийся с
        # дефиса fuse2fs разобрал бы как опцию, а не как файл
        if not os.path.isabs(image):
            msg = f"fuse2fs mount: image path must be absolute, got {image!r}"
            raise MountError(msg)

        if not os.path.isabs(mnt):
            msg = f"fuse2fs mount: mount point must be absolute, got {mnt!r}"
            raise MountError(msg)

        fuse2fs = self._binaries.resolve(SandboxBinary.FUSE2FS)
        os.makedirs(mnt, exist_ok=True)

        options = self.NEUTRAL_OPTIONS
        if readonly:
            options = f"{self.READONLY_OPTIONS},{options}"
        if fakeroot:
            options = f"{self.FAKEROOT_OPTIONS},{options}"

        argv = [fuse2fs, "-f", "-o", options, image, mnt]

        # stdout fuse2fs — информационный шум («Mounting read-only.»); в stderr
        # он смешивался бы с выводом тела. Ошибки fuse2fs идут его stderr'ом.
        daemon = subprocess.Popen(  # noqa: S603
            argv,
            shell=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            pass_fds=self._pass_fds,
            preexec_fn=self.set_pdeathsig,  # noqa: PLW1509
        )
        self._daemons.append(daemon)
        started = time.monotonic()
        self._wait_mounted(mnt, daemon)
        elapsed_ms = int((time.monotonic() - started) * 1000)
        trace(f"{image} mounted at {mnt} in {elapsed_ms}ms (fuse2fs pid {daemon.pid})")

    def shutdown(self) -> None:
        for daemon in self._daemons:
            daemon.terminate()
        for daemon in self._daemons:
            try:
                daemon.wait(timeout=self._options.shutdown_wait_sec)
                trace(f"fuse2fs pid {daemon.pid} exited normally, caches flushed")
            except subprocess.TimeoutExpired:
                daemon.kill()
                daemon.wait()
                trace(
                    f"fuse2fs pid {daemon.pid} ignored SIGTERM for "
                    f"{self._options.shutdown_wait_sec}s and was killed: "
                    f"some writes may not have reached the image"
                )
        self._daemons.clear()

    @classmethod
    def set_pdeathsig(cls) -> None:
        libc = ctypes.CDLL(None, use_errno=True)
        libc.prctl(cls._PR_SET_PDEATHSIG, int(signal.SIGKILL))

    @classmethod
    def is_mounted(cls, target: str) -> bool:
        with open(cls.MOUNTINFO) as f:
            for line in f:
                if line.split()[4] == target:
                    return True
        return False

    def _wait_mounted(self, mnt: str, daemon: subprocess.Popen[bytes]) -> None:
        target = os.path.realpath(mnt)
        deadline = time.monotonic() + self._options.mount_wait_sec
        while time.monotonic() < deadline:
            if daemon.poll() is not None:
                msg = (
                    f"fuse2fs pid {daemon.pid} ({daemon.args!r}) exited with "
                    f"code {daemon.returncode} before {mnt} appeared in "
                    f"{self.MOUNTINFO}"
                )
                raise MountError(msg)
            if self.is_mounted(target):
                return
            time.sleep(self._options.mount_poll_sec)
        msg = (
            f"{mnt} was not mounted by fuse2fs pid {daemon.pid} "
            f"({daemon.args!r}) within {self._options.mount_wait_sec}s"
        )
        raise MountError(msg)


@dataclass(frozen=True)
class LauncherOptions:
    """Тайминги и размеры лаунчера; значения приходят из профиля."""

    mount_wait_sec: float
    mount_poll_sec: float
    shutdown_wait_sec: float
    lock_wait_sec: float
    copy_chunk_bytes: int
