"""Запуск в workspace-образе: fuse2fs-цепочка, её конфиг и лимиты процесса;
супервизор mount-группы — N вложенных bwrap'ов над одним монтированием.

Ошибки: MountError — предпосылки, подготовка или монтирование образа,
включая сборку цепочки, рендер путей и контракт mount-группы;
NotRegularFileError — по пути в образе не обычный файл.
"""

from __future__ import annotations

import argparse
import contextlib
import ctypes
import errno
import fcntl
import os
import resource
import shlex
import shutil
import signal
import stat as stat_module
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import IntEnum, StrEnum
from types import FrameType, TracebackType
from typing import BinaryIO, ClassVar, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from boba.toolkit.channels import Channel, ShellExit, StageExit

__all__ = [
    "FUSE_DEVICE",
    "CapabilityDropper",
    "ChainChannelArgv",
    "ChannelBridge",
    "FileOperations",
    "FuseMounter",
    "ImagePath",
    "ImageStore",
    "Launcher",
    "LauncherConfig",
    "LauncherEnv",
    "LauncherExit",
    "LauncherMarker",
    "LauncherMode",
    "LauncherOptions",
    "MountError",
    "NotRegularFileError",
    "ReadHeader",
    "ReadWindow",
    "ResourceLimits",
    "SparseCopier",
    "StageEntry",
    "StageSupervisor",
    "WrapOutput",
    "WrapResultWriter",
    "build_chain_argv",
    "render_image_path",
    "require_fuse",
    "trace",
]

_SignalHandler: TypeAlias = (
    Callable[[int, FrameType | None], object] | int | signal.Handlers | None
)


class LauncherExit(IntEnum):
    """Коды возврата лаунчера."""

    OK = 0
    MOUNT_ERROR = 2
    NOT_FOUND = 3
    NO_SPACE = 4
    NOT_REGULAR = 5


class LauncherMarker(StrEnum):
    """Маркеры строк stderr: хост отличает трассировку и сбой от чужого шума."""

    LOG = "sandbox-mount: "
    ERROR = "sandbox-mount-error: "


class LauncherMode(StrEnum):
    """Операции лаунчера над смонтированным образом.

    `group` — mount-группа: аргументы режима — записи `StageEntry`, по одному
    вложенному bwrap на стадию; flock и монтирование одни на группу.
    """

    RUN = "run"
    GROUP = "group"
    WRITE = "write"
    READ = "read"
    DELETE = "delete"
    LIST = "list"
    STAT = "stat"

    def is_readonly(self) -> bool:
        """Читающий режим: образ монтируется ro под разделяемым локом."""
        readonly = (LauncherMode.READ, LauncherMode.LIST, LauncherMode.STAT)
        return self in readonly


class ReadWindow(BaseModel):
    """Окно чтения файла: length None — до конца, 0 — только размер."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    UNBOUNDED_ARG: ClassVar[str] = "-1"
    """Представление length=None в argv: между процессами едут только строки."""

    offset: int = Field(ge=0)
    length: int | None = Field(ge=0)

    @classmethod
    def entire(cls) -> ReadWindow:
        return cls(offset=0, length=None)

    def to_argv(self) -> list[str]:
        length = self.UNBOUNDED_ARG
        if self.length is not None:
            length = str(self.length)

        return [str(self.offset), length]

    @classmethod
    def from_argv(cls, offset: str, length: str) -> ReadWindow:
        if length == cls.UNBOUNDED_ARG:
            return cls(offset=int(offset), length=None)

        return cls(offset=int(offset), length=int(length))

    def resolve_length(self, size: int) -> int:
        """Сколько байт реально отдать из файла известного размера."""
        available = max(size - self.offset, 0)

        if self.length is None:
            return available

        return min(self.length, available)


class ReadHeader:
    """Первая строка stdout читающих операций: полный размер файла."""

    PREFIX: ClassVar[bytes] = b"size="

    @classmethod
    def render(cls, size: int) -> bytes:
        return cls.PREFIX + str(size).encode("ascii") + b"\n"

    @classmethod
    def parse(cls, line: bytes) -> int:
        if not line.startswith(cls.PREFIX):
            msg = f"invalid read header: {line!r}"
            raise ValueError(msg)

        return int(line[len(cls.PREFIX) :].strip())


class LauncherEnv(StrEnum):
    """Переменные окружения портативного python: без них лаунчер не стартует."""

    LD_LIBRARY_PATH = "LD_LIBRARY_PATH"
    PYTHONHOME = "PYTHONHOME"
    PYTHONPATH = "PYTHONPATH"


class WrapOutput:
    """Сообщения обвязки: маркерные строки в общем stderr либо чистые wrap-каналы.

    В канальном режиме fd 1/2 лаунчера — это wrap_stdout/wrap_stderr стадии:
    каналы принадлежат обвязке целиком, маркеры не нужны.
    """

    _plain: ClassVar[bool] = False

    @classmethod
    def configure(cls, *, plain: bool) -> None:
        cls._plain = plain

    @classmethod
    def trace(cls, message: str) -> None:
        if cls._plain:
            print(message, file=sys.stdout, flush=True)  # noqa: T201
            return

        print(f"{LauncherMarker.LOG}{message}", file=sys.stderr, flush=True)  # noqa: T201

    @classmethod
    def error(cls, message: str) -> None:
        if cls._plain:
            print(message, file=sys.stderr, flush=True)  # noqa: T201
            return

        print(f"{LauncherMarker.ERROR}{message}", file=sys.stderr, flush=True)  # noqa: T201


def trace(message: str) -> None:
    """Ход монтирования; в старом пути — в stderr: stdout занят данными операции."""
    WrapOutput.trace(message)


class MountError(RuntimeError):
    """Сбой подготовки или монтирования образа."""


class NotRegularFileError(RuntimeError):
    """По пути в образе лежит не обычный файл: каталог, канал или устройство."""


class ChannelBridge:
    """Каналы стадии из окружения лаунчера: дескрипторы для проброса вниз.

    Внешний bwrap проводит переменные `Channel.env_name` через `--setenv`;
    `pass_fds` сохраняет номера, поэтому номер, сообщённый на верхней ступени
    цепочки, действителен и во вложенном bwrap.
    """

    STDIO: ClassVar[frozenset[int]] = frozenset((0, 1, 2))
    """stdin/stdout/stderr наследуются и так: в pass_fds им не место."""

    def __init__(self, fds: Mapping[Channel, int]) -> None:
        self._fds = dict(fds)

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> ChannelBridge:
        fds: dict[Channel, int] = {}

        for channel in Channel:
            raw = env.get(channel.env_name)
            if raw is None:
                continue

            try:
                fds[channel] = int(raw)
            except ValueError as exc:
                msg = f"channel {channel.value} fd is not an integer: {raw!r}"
                raise MountError(msg) from exc

        return cls(fds)

    @property
    def active(self) -> bool:
        """Хоть один канал объявлен: лаунчер работает в канальном режиме."""
        return bool(self._fds)

    def fd_of(self, channel: Channel) -> int | None:
        """Номер дескриптора канала; None — канал в окружении не объявлен."""
        return self._fds.get(channel)

    @property
    def pass_fds(self) -> tuple[int, ...]:
        fds: list[int] = []
        for fd in sorted(self._fds.values()):
            if fd in self.STDIO:
                continue
            fds.append(fd)

        return tuple(fds)


class SparseCopier:
    """Копирует только данные: дыры и нулевые блоки остаются дырами."""

    def __init__(self, chunk_bytes: int) -> None:
        self._chunk = chunk_bytes
        self._zero = b"\0" * chunk_bytes

    def copy(self, src: str, dst: str) -> None:
        with open(src, "rb") as fin, open(dst, "wb") as fout:
            size = os.fstat(fin.fileno()).st_size
            fout.truncate(size)
            offset = 0
            while offset < size:
                offset = self._copy_next_extent(fin, fout, offset, size)

    def _copy_next_extent(
        self, fin: BinaryIO, fout: BinaryIO, offset: int, size: int
    ) -> int:
        try:
            start = os.lseek(fin.fileno(), offset, os.SEEK_DATA)
        except OSError as e:
            if e.errno != errno.ENXIO:
                raise
            return size
        end = os.lseek(fin.fileno(), start, os.SEEK_HOLE)
        fin.seek(start)
        while start < end:
            chunk = fin.read(min(end - start, self._chunk))
            if not chunk:
                msg = f"unexpected end of file {fin.name!r} at offset {start}"
                raise MountError(msg)
            if chunk != self._zero[: len(chunk)]:
                fout.seek(start)
                fout.write(chunk)
            start += len(chunk)
        return start


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

    def _materialize(self, image: str) -> None:
        if not os.path.exists(self._template):
            msg = f"template image {self._template!r} not found"
            raise MountError(msg)
        tmp = f"{image}.tmp.{os.getpid()}"
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
        self, options: LauncherOptions, pass_fds: tuple[int, ...] = ()
    ) -> None:
        self._options = options
        self._pass_fds = pass_fds
        self._daemons: list[subprocess.Popen[bytes]] = []

    READONLY_OPTIONS: ClassVar[str] = "ro,norecovery"
    """Чтение под разделяемым локом: replay журнала писал бы в образ."""

    FAKEROOT_OPTIONS: ClassVar[str] = "fakeroot"
    """Запись без userns: права внутри образа проверяются как у root."""

    def mount(
        self, image: str, mnt: str, *, readonly: bool, fakeroot: bool = False
    ) -> None:
        fuse2fs = shutil.which("fuse2fs")
        if fuse2fs is None:
            msg = "fuse2fs not found in PATH"
            raise MountError(msg)
        os.makedirs(mnt, exist_ok=True)

        argv = [fuse2fs, "-f", image, mnt]
        if readonly:
            argv = [fuse2fs, "-f", "-o", self.READONLY_OPTIONS, image, mnt]
        if fakeroot:
            argv = [fuse2fs, "-f", "-o", self.FAKEROOT_OPTIONS, image, mnt]

        # stdout лаунчера несёт данные операции: предупреждения fuse2fs — в stderr
        daemon = subprocess.Popen(  # noqa: S603
            argv,
            shell=False,
            stdin=subprocess.DEVNULL,
            stdout=sys.stderr,
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

    @classmethod
    def reclaim(cls, mnt: str) -> None:
        """Отцепляет точку, осиротевшую после гибели процесса-владельца.

        Актуально только для маунтов в namespace хоста: SIGKILL валит fuse2fs
        без размонтирования, точка остаётся в mountinfo битой — stat даёт
        ENOTCONN, mkdir даёт EEXIST. Приватные namespace лаунчера ядро
        прибирает само.
        """
        stale = False
        try:
            os.stat(mnt)
        except FileNotFoundError:
            return
        except OSError:
            stale = True

        if not stale and not cls.is_mounted(os.path.realpath(mnt)):
            return

        fusermount = shutil.which("fusermount3")
        if fusermount is None:
            fusermount = shutil.which("fusermount")
        if fusermount is None:
            trace(f"stale mount at {mnt}: fusermount not found")
            return

        subprocess.run(  # noqa: S603
            [fusermount, "-uz", mnt], check=False, capture_output=True
        )
        trace(f"stale mount reclaimed: {mnt}")

    def _wait_mounted(self, mnt: str, daemon: subprocess.Popen[bytes]) -> None:
        target = os.path.realpath(mnt)
        deadline = time.monotonic() + self._options.mount_wait_sec
        while time.monotonic() < deadline:
            if daemon.poll() is not None:
                msg = f"fuse2fs exited with code {daemon.returncode}"
                raise MountError(msg)
            if self.is_mounted(target):
                return
            time.sleep(self._options.mount_poll_sec)
        msg = f"{mnt} was not mounted within {self._options.mount_wait_sec}s"
        raise MountError(msg)


class CapabilityDropper:
    """Полный сброс capabilities процесса через capset(2)."""

    _VERSION_3: ClassVar[int] = 0x20080522

    class _Header(ctypes.Structure):
        _fields_: ClassVar = [("version", ctypes.c_uint32), ("pid", ctypes.c_int)]

    class _Data(ctypes.Structure):
        _fields_: ClassVar = [
            ("effective", ctypes.c_uint32),
            ("permitted", ctypes.c_uint32),
            ("inheritable", ctypes.c_uint32),
        ]

    def drop_all(self) -> None:
        header = self._Header(self._VERSION_3, 0)
        data = (self._Data * 2)()
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.capset(ctypes.byref(header), ctypes.byref(data)) != 0:
            raise OSError(ctypes.get_errno(), "capset")


class ImagePath:
    """Путь внутри образа, открываемый без перехода по симлинкам.

    Каждый сегмент открывается отдельно с O_NOFOLLOW, поэтому ни конечный
    файл, ни промежуточный каталог не выводят за пределы образа: иначе
    bash-тул подменил бы вложение симлинком на файл хоста. O_NONBLOCK не даёт
    open зависнуть на именованном канале, а тип проверяется уже по открытому
    описателю — между проверкой и чтением подменить файл нельзя.
    """

    DIR_FLAGS: ClassVar[int] = os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY
    READ_FLAGS: ClassVar[int] = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    WRITE_FLAGS: ClassVar[int] = (
        os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW | os.O_NONBLOCK
    )
    FILE_MODE: ClassVar[int] = 0o600
    DIR_MODE: ClassVar[int] = 0o700

    REFUSED_ERRNO: ClassVar[frozenset[int]] = frozenset(
        (errno.ELOOP, errno.ENOTDIR, errno.ENXIO, errno.EISDIR)
    )
    """Симлинк, не каталог или не файл на пути: идти дальше нельзя."""

    def __init__(self, root: str) -> None:
        self._root = root

    def open_read(self, rel: str) -> int:
        parent, name = self._parent_and_name(rel, create=False)
        try:
            return os.open(name, self.READ_FLAGS, dir_fd=parent)
        finally:
            os.close(parent)

    def open_write(self, rel: str) -> int:
        parent, name = self._parent_and_name(rel, create=True)
        try:
            return os.open(name, self.WRITE_FLAGS, self.FILE_MODE, dir_fd=parent)
        finally:
            os.close(parent)

    def open_dir(self, rel: str) -> int:
        segments = self._split(rel)
        return self._walk(segments, create=False)

    def unlink(self, rel: str) -> None:
        """Снимает саму запись каталога: символическая ссылка не разыменовывается."""
        parent, name = self._parent_and_name(rel, create=False)
        try:
            os.unlink(name, dir_fd=parent)
        finally:
            os.close(parent)

    def _parent_and_name(self, rel: str, *, create: bool) -> tuple[int, str]:
        segments = self._split(rel)
        parent = self._walk(segments[:-1], create=create)
        return parent, segments[-1]

    @staticmethod
    def _split(rel: str) -> list[str]:
        norm = os.path.normpath(rel)
        if norm.startswith(("/", "..")):
            msg = f"invalid relative path {rel!r}"
            raise MountError(msg)

        return norm.split(os.sep)

    def _walk(self, segments: Sequence[str], *, create: bool) -> int:
        """Спускается по каталогам до последнего сегмента; отдаёт его описатель."""
        fd = os.open(self._root, self.DIR_FLAGS)
        try:
            for name in segments:
                if create:
                    self._make_dir(fd, name)

                fd = self._step(fd, name)
        except BaseException:
            os.close(fd)
            raise

        return fd

    def _step(self, fd: int, name: str) -> int:
        nested = os.open(name, self.DIR_FLAGS, dir_fd=fd)
        os.close(fd)
        return nested

    @classmethod
    def _make_dir(cls, fd: int, name: str) -> None:
        try:
            os.mkdir(name, cls.DIR_MODE, dir_fd=fd)
        except FileExistsError:
            return


class FileOperations:
    """write/read/stat/delete/list по относительному пути внутри mountpoint."""

    def __init__(self, root: str, chunk_bytes: int) -> None:
        self._path = ImagePath(root)
        self._chunk_bytes = chunk_bytes

    NO_SPACE_ERRNO: ClassVar[frozenset[int]] = frozenset(
        (errno.ENOSPC, errno.EDQUOT, errno.EFBIG)
    )
    """Образ кончился: недописанный файл сносится, чтобы не занимать остаток."""

    def write(self, rel: str, src: BinaryIO) -> LauncherExit:
        try:
            fd = self._path.open_write(rel)
        except OSError as e:
            return self._refused(rel, e)

        try:
            self._require_regular(fd)
        except NotRegularFileError:
            os.close(fd)
            return self._not_regular(rel)

        try:
            with os.fdopen(fd, "wb") as f:
                shutil.copyfileobj(src, f)
                f.flush()
                os.fsync(f.fileno())
        except OSError as e:
            if e.errno not in self.NO_SPACE_ERRNO:
                raise
            self._discard(rel)
            WrapOutput.error(f"no space left in the workspace image for {rel!r}")
            return LauncherExit.NO_SPACE

        return LauncherExit.OK

    def _discard(self, rel: str) -> None:
        try:
            self._path.unlink(rel)
        except OSError:
            trace(f"cannot remove partial file {rel!r}")

    def read(self, rel: str, window: ReadWindow, dst: BinaryIO) -> LauncherExit:
        """Заголовок с полным размером, затем тело окна чанками."""
        try:
            fd, size = self._open_regular(rel)
        except OSError as e:
            return self._refused(rel, e)
        except NotRegularFileError:
            return self._not_regular(rel)

        with os.fdopen(fd, "rb") as f:
            dst.write(ReadHeader.render(size))

            f.seek(window.offset)
            remaining = window.resolve_length(size)
            while remaining > 0:
                chunk = f.read(min(self._chunk_bytes, remaining))
                if not chunk:
                    break
                dst.write(chunk)
                remaining -= len(chunk)

        dst.flush()
        return LauncherExit.OK

    def stat(self, rel: str, dst: BinaryIO) -> LauncherExit:
        """Только заголовок с размером: существование без чтения тела."""
        try:
            fd, size = self._open_regular(rel)
        except OSError as e:
            return self._refused(rel, e)
        except NotRegularFileError:
            return self._not_regular(rel)

        os.close(fd)

        dst.write(ReadHeader.render(size))
        dst.flush()
        return LauncherExit.OK

    def _open_regular(self, rel: str) -> tuple[int, int]:
        """Описатель обычного файла и его размер; тип берётся с уже открытого fd."""
        fd = self._path.open_read(rel)
        try:
            return fd, self._require_regular(fd)
        except BaseException:
            os.close(fd)
            raise

    @staticmethod
    def _require_regular(fd: int) -> int:
        """Размер открытого файла; всё, что не обычный файл, — отказ."""
        info = os.fstat(fd)
        if not stat_module.S_ISREG(info.st_mode):
            raise NotRegularFileError

        return info.st_size

    @classmethod
    def _refused(cls, rel: str, failure: OSError) -> LauncherExit:
        if failure.errno == errno.ENOENT:
            return LauncherExit.NOT_FOUND

        if failure.errno in ImagePath.REFUSED_ERRNO:
            return cls._not_regular(rel)

        raise failure

    @staticmethod
    def _not_regular(rel: str) -> LauncherExit:
        WrapOutput.error(f"not a regular file: {rel!r}")
        return LauncherExit.NOT_REGULAR

    def delete(self, rel: str) -> LauncherExit:
        try:
            self._path.unlink(rel)
        except OSError as e:
            return self._refused(rel, e)

        return LauncherExit.OK

    def list_dir(self, rel: str, dst: BinaryIO) -> LauncherExit:
        """Имена обычных файлов каталога, по одному в строке, в порядке имён."""
        try:
            fd = self._path.open_dir(rel)
        except OSError as e:
            return self._listing_refused(e)

        try:
            names = self._regular_names(fd)
            for name in names:
                dst.write(name.encode("utf-8") + b"\n")
        finally:
            os.close(fd)

        dst.flush()
        return LauncherExit.OK

    @staticmethod
    def _listing_refused(failure: OSError) -> LauncherExit:
        if failure.errno == errno.ENOENT:
            return LauncherExit.NOT_FOUND

        if failure.errno in ImagePath.REFUSED_ERRNO:
            return LauncherExit.NOT_FOUND

        raise failure

    @staticmethod
    def _regular_names(fd: int) -> list[str]:
        """Симлинки в листинг не попадают: их содержимое всё равно не отдаётся."""
        names: list[str] = []
        for entry in sorted(os.listdir(fd)):
            info = os.stat(entry, dir_fd=fd, follow_symlinks=False)
            if not stat_module.S_ISREG(info.st_mode):
                continue

            names.append(entry)

        return names


class Launcher:
    """Оркестратор: валидация -> локи и образы -> mount -> операция -> гашение."""

    USERNS_SYSCTL: ClassVar[str] = "/proc/sys/user/max_user_namespaces"
    """bwrap --disable-userns несовместим с mount fuse, поэтому sysctl после mount."""

    def __init__(
        self,
        template: str,
        images: list[tuple[str, str]],
        options: LauncherOptions,
        limits: ResourceLimits,
        channels: ChannelBridge,
    ) -> None:
        self._images = images
        self._options = options
        self._limits = limits
        self._channels = channels
        self._store = ImageStore(
            template, SparseCopier(options.copy_chunk_bytes), options.lock_wait_sec
        )

    @classmethod
    def main(cls, argv: list[str]) -> int:
        try:
            channels = ChannelBridge.from_env(os.environ)
        except MountError as e:
            WrapOutput.error(str(e))
            return LauncherExit.MOUNT_ERROR

        WrapOutput.configure(plain=channels.active)

        args = cls._parse_args(argv)
        images: list[tuple[str, str]] = []
        for pair in args.image:
            images.append((pair[0], pair[1]))
        options = LauncherOptions(
            mount_wait_sec=args.mount_wait_sec,
            mount_poll_sec=args.mount_poll_sec,
            shutdown_wait_sec=args.shutdown_wait_sec,
            lock_wait_sec=args.lock_wait_sec,
            copy_chunk_bytes=args.copy_chunk_bytes,
        )
        limits = ResourceLimits(
            max_memory_bytes=args.max_memory_bytes,
            max_cpu_sec=args.max_cpu_sec,
            max_file_size_bytes=args.max_file_size_bytes,
            max_open_files=args.max_open_files,
            oom_score_adj=args.oom_score_adj,
        )
        launcher = cls(args.template, images, options, limits, channels)
        try:
            return launcher.run(args.mode, args.args)
        except (MountError, OSError, ValueError) as e:
            WrapOutput.error(str(e))
            return LauncherExit.MOUNT_ERROR

    def run(self, mode: LauncherMode, op_args: list[str]) -> int:
        operation = self._plan(mode, op_args)
        readonly = mode.is_readonly()
        started = time.monotonic()
        trace(f"operation {mode.value!r}, images: {len(self._images)}")

        for image, _ in self._images:
            if not readonly:
                self._store.acquire(image)
                continue

            if not self._store.acquire_shared(image):
                trace(f"image {image} does not exist: nothing to read")
                return LauncherExit.NOT_FOUND

        mounter = FuseMounter(self._options, pass_fds=self._store.lock_fds)
        try:
            for image, mnt in self._images:
                mounter.mount(image, mnt, readonly=readonly)
            code = operation()
            elapsed_ms = int((time.monotonic() - started) * 1000)
            trace(f"operation {mode.value!r} finished rc={code} in {elapsed_ms}ms")
            return code
        finally:
            mounter.shutdown()

    def _block_new_userns(self) -> None:
        """Нужно только перед чужим кодом: файловые операции — наш же код."""
        with open(self.USERNS_SYSCTL, "w") as f:
            f.write("0")
        trace(f"{self.USERNS_SYSCTL}=0: nested user namespaces denied to the command")

    def _plan(self, mode: LauncherMode, op_args: list[str]) -> Callable[[], int]:
        if mode is LauncherMode.RUN:
            argument = self._single_argument(mode, op_args)
            argv = shlex.split(argument)
            if not argv:
                msg = "run: empty command"
                raise MountError(msg)
            return lambda: self._run_command(argv)

        if mode is LauncherMode.GROUP:
            entries = self._group_entries(op_args)
            return lambda: self._run_group(entries)

        if mode is LauncherMode.READ:
            rel, window = self._read_arguments(op_args)
            return lambda: self._read_operation(rel, window)

        argument = self._single_argument(mode, op_args)
        return lambda: self._file_operation(mode, argument)

    def _operations(self) -> FileOperations:
        return FileOperations(self._images[0][1], self._options.copy_chunk_bytes)

    def _read_operation(self, rel: str, window: ReadWindow) -> LauncherExit:
        ops = self._operations()
        CapabilityDropper().drop_all()
        return ops.read(rel, window, sys.stdout.buffer)

    def _file_operation(self, mode: LauncherMode, argument: str) -> LauncherExit:
        ops = self._operations()
        CapabilityDropper().drop_all()
        if mode is LauncherMode.WRITE:
            return ops.write(argument, sys.stdin.buffer)
        if mode is LauncherMode.STAT:
            return ops.stat(argument, sys.stdout.buffer)
        if mode is LauncherMode.LIST:
            return ops.list_dir(argument, sys.stdout.buffer)
        return ops.delete(argument)

    SINGLE_STAGE: ClassVar[str] = "run"
    """Имя вырожденной стадии одиночного режима в трассировке супервизора."""

    STDIN_INHERITED: ClassVar[int] = 0
    """Одиночный run: tool_stdin стадии уже стоит нулевым дескриптором лаунчера."""

    def _run_command(self, argv: list[str]) -> int:
        self._block_new_userns()
        trace(
            f"command limits: memory={self._limits.max_memory_bytes}B "
            f"cpu={self._limits.max_cpu_sec}s file={self._limits.max_file_size_bytes}B "
            f"open_files={self._limits.max_open_files} "
            f"oom_score_adj={self._limits.oom_score_adj}"
        )

        # каналы стадии едут вниз с теми же номерами: их сообщил env верхней ступени
        plan = _StagePlan(
            stage=self.SINGLE_STAGE,
            argv=tuple(argv),
            stdin_fd=self.STDIN_INHERITED,
            pass_fds=self._store.lock_fds + self._channels.pass_fds,
            limits=self._limits,
            cgroup_dir=None,
        )

        supervisor = StageSupervisor([plan], None, ())
        return supervisor.run()

    def _group_entries(self, op_args: list[str]) -> list[StageEntry]:
        """Разбор и проверка записей стадий: падаем до захвата локов."""
        if not op_args:
            msg = "group: at least one stage entry expected"
            raise MountError(msg)

        if self._channels.fd_of(Channel.WRAP_RESULT) is None:
            msg = "group: wrap_result channel is not declared"
            raise MountError(msg)

        entries: list[StageEntry] = []
        for raw in op_args:
            entries.append(StageEntry.parse(raw))

        self._check_stage_ids(entries)
        self._check_stage_fds(entries)
        self._check_stage_cgroups(entries)

        return entries

    def _run_group(self, entries: Sequence[StageEntry]) -> int:
        self._block_new_userns()

        # огибающая группы (--max-*) душит супервизор и всё, что он породит;
        # точные значения стадии ставит preexec вложенного bwrap
        self._limits.apply_to_current_process()

        reporter = self._wrap_result_writer()

        plans: list[_StagePlan] = []
        close_fds: list[int] = []
        for entry in entries:
            plans.append(self._group_plan(entry))
            close_fds.extend(entry.owned_fds())

        supervisor = StageSupervisor(plans, reporter, close_fds)
        return supervisor.run()

    def _group_plan(self, entry: StageEntry) -> _StagePlan:
        return _StagePlan(
            stage=entry.stage,
            argv=entry.argv(),
            stdin_fd=entry.stdin_fd,
            pass_fds=self._store.lock_fds + entry.pass_fds,
            limits=entry.limits,
            cgroup_dir=entry.cgroup_dir,
        )

    def _wrap_result_writer(self) -> WrapResultWriter:
        fd = self._channels.fd_of(Channel.WRAP_RESULT)

        if fd is None:
            msg = "group: wrap_result channel is not declared"
            raise MountError(msg)

        return WrapResultWriter(fd)

    @staticmethod
    def _check_stage_ids(entries: Sequence[StageEntry]) -> None:
        seen: set[str] = set()

        for entry in entries:
            if entry.stage in seen:
                msg = f"group: duplicate stage id {entry.stage!r}"
                raise MountError(msg)

            seen.add(entry.stage)

    def _check_stage_fds(self, entries: Sequence[StageEntry]) -> None:
        """Каждый fd принадлежит ровно одной стадии и не задевает каналы группы."""
        seen: set[int] = set()

        for fd in self._channels.pass_fds:
            seen.add(fd)

        for entry in entries:
            for fd in entry.owned_fds():
                if fd in seen:
                    msg = f"group: stage {entry.stage!r}: fd {fd} is already taken"
                    raise MountError(msg)

                seen.add(fd)

    @staticmethod
    def _check_stage_cgroups(entries: Sequence[StageEntry]) -> None:
        for entry in entries:
            if entry.cgroup_dir is None:
                continue

            if os.path.isdir(entry.cgroup_dir):
                continue

            msg = (
                f"group: stage {entry.stage!r}: cgroup {entry.cgroup_dir!r} "
                f"is not a directory"
            )
            raise MountError(msg)

    @classmethod
    def _parse_args(cls, argv: list[str]) -> argparse.Namespace:
        parser = argparse.ArgumentParser(prog="workspace-launcher")
        parser.add_argument("--template", required=True)
        parser.add_argument(
            "--image", nargs=2, action="append", metavar=("IMG", "MNT"), required=True
        )
        parser.add_argument("--mount-wait-sec", type=float, required=True)
        parser.add_argument("--mount-poll-sec", type=float, required=True)
        parser.add_argument("--shutdown-wait-sec", type=float, required=True)
        parser.add_argument("--lock-wait-sec", type=float, required=True)
        parser.add_argument("--copy-chunk-bytes", type=int, required=True)
        parser.add_argument("--max-memory-bytes", type=int, required=True)
        parser.add_argument("--max-cpu-sec", type=int, required=True)
        parser.add_argument("--max-file-size-bytes", type=int, required=True)
        parser.add_argument("--max-open-files", type=int, required=True)
        parser.add_argument("--oom-score-adj", type=int, required=True)
        parser.add_argument("mode", type=LauncherMode, choices=tuple(LauncherMode))
        parser.add_argument("args", nargs=argparse.REMAINDER)
        return parser.parse_args(argv)

    @staticmethod
    def _single_argument(mode: LauncherMode, op_args: list[str]) -> str:
        if len(op_args) != 1:
            msg = f"{mode}: exactly one argument expected, got {len(op_args)}"
            raise MountError(msg)
        return op_args[0]

    READ_ARGS: ClassVar[int] = 3
    """Аргументы read: относительный путь, offset, length."""

    @classmethod
    def _read_arguments(cls, op_args: list[str]) -> tuple[str, ReadWindow]:
        if len(op_args) != cls.READ_ARGS:
            msg = f"read: expected <rel> <offset> <length>, got {len(op_args)} args"
            raise MountError(msg)

        try:
            window = ReadWindow.from_argv(op_args[1], op_args[2])
        except ValueError as e:
            msg = f"read: invalid window: {e}"
            raise MountError(msg) from e

        return op_args[0], window


_LAUNCHER_MODULE = "boba.workspace.launcher"

FUSE_DEVICE = "/dev/fuse"


def require_fuse() -> None:
    """Проверяет предпосылки монтирования образов; падает громко и сразу."""
    if not os.path.exists(FUSE_DEVICE):
        msg = f"workspace: fuse is required, but {FUSE_DEVICE} is missing"
        raise MountError(msg)
    if shutil.which("fuse2fs") is None:
        msg = "workspace: fuse2fs not found in PATH"
        raise MountError(msg)
    if shutil.which("bwrap") is None:
        msg = "workspace: bwrap not found in PATH"
        raise MountError(msg)


@dataclass(frozen=True)
class ChainChannelArgv:
    """Канальная сборка fuse-цепочки: опции внешнего bwrap уезжают в wrap_args.

    В argv остаётся `bwrap --args FD -- python -m launcher …`: профиль внешней
    ступени (бинды, setenv, пути образов) не виден в ps и не упирается в
    MAX_ARG_STRLEN.
    """

    BWRAP_BIN: ClassVar[str] = "bwrap"

    argv: tuple[str, ...]
    bwrap_options: tuple[str, ...]

    @classmethod
    def build(  # noqa: PLR0913
        cls,
        *,
        images: Sequence[tuple[str, str]],
        template: str,
        op: Sequence[str],
        python_bin: str,
        options: LauncherOptions,
        limits: ResourceLimits,
        wrap_args_fd: int,
        rw_paths: Sequence[str],
        network: bool,
        channel_env: Mapping[str, str] | None,
    ) -> ChainChannelArgv:
        """channel_env — пары `Channel.env_name` без wrap_args: его вычитывает
        --args внешнего bwrap, вниз по цепочке канал не передаётся."""
        bwrap_path = cls._bwrap_path(cls.BWRAP_BIN)
        pairs = cls._absolute_images(images)

        bwrap_options = cls._bwrap_options(
            pairs, rw_paths=rw_paths, network=network, channel_env=channel_env
        )
        tail = cls._launcher_argv(
            python_bin=python_bin,
            template=template,
            options=options,
            limits=limits,
            images=pairs,
            op=op,
        )

        argv = (bwrap_path, "--args", str(wrap_args_fd), "--", *tail)

        return cls(argv=argv, bwrap_options=tuple(bwrap_options))

    @staticmethod
    def _bwrap_path(bwrap_bin: str) -> str:
        bwrap_path = shutil.which(bwrap_bin)
        if not bwrap_path:
            msg = f"workspace: {bwrap_bin!r} not found in PATH"
            raise MountError(msg)

        return bwrap_path

    @staticmethod
    def _absolute_images(images: Sequence[tuple[str, str]]) -> list[tuple[str, str]]:
        """Корень песочницы read-only: относительные пути приводим к абсолютным."""
        absolute: list[tuple[str, str]] = []
        for image, mnt in images:
            absolute.append((os.path.abspath(image), os.path.abspath(mnt)))

        return absolute

    @staticmethod
    def _bwrap_options(
        images: Sequence[tuple[str, str]],
        *,
        rw_paths: Sequence[str],
        network: bool,
        channel_env: Mapping[str, str] | None,
    ) -> list[str]:
        """CAP_SYS_ADMIN только в userns; --disable-userns несовместим с mount fuse.

        channel_env — номера каналов через --setenv: --clearenv стирает окружение.
        """
        options = [
            "--die-with-parent",
            "--unshare-user",
            "--uid",
            "0",
            "--gid",
            "0",
            "--cap-add",
            "CAP_SYS_ADMIN",
            # CAP_SYS_RESOURCE (в userns) — право обнулить max_user_namespaces
            "--cap-add",
            "CAP_SYS_RESOURCE",
            # CAP_NET_ADMIN (в userns) — loopback вложенного --unshare-net:
            # стадия без сети изолируется внутри цепочки per-stage
            "--cap-add",
            "CAP_NET_ADMIN",
            "--unshare-pid",
            "--unshare-ipc",
            "--unshare-uts",
            "--unshare-cgroup-try",
            "--hostname",
            "sandbox",
            "--new-session",
            "--ro-bind",
            "/",
            "/",
        ]

        writable: list[str] = []
        for image, mnt in images:
            writable.append(os.path.dirname(image))
            writable.append(os.path.dirname(mnt))
        for path in rw_paths:
            writable.append(os.path.abspath(path))
        for path in dict.fromkeys(writable):
            options += ["--bind", path, path]

        options += [
            "--dev",
            "/dev",
            "--dev-bind",
            FUSE_DEVICE,
            FUSE_DEVICE,
            "--proc",
            "/proc",
            "--clearenv",
            "--setenv",
            "PATH",
            os.environ.get("PATH", "/usr/bin:/bin"),
        ]

        for name in LauncherEnv:
            value = os.environ.get(name.value)
            if not value:
                continue
            options += ["--setenv", name.value, value]

        if channel_env:
            for env_name, env_value in channel_env.items():
                options += ["--setenv", env_name, env_value]

        if not network:
            options.append("--unshare-net")

        return options

    @staticmethod
    def _launcher_argv(  # noqa: PLR0913
        *,
        python_bin: str,
        template: str,
        options: LauncherOptions,
        limits: ResourceLimits,
        images: Sequence[tuple[str, str]],
        op: Sequence[str],
    ) -> list[str]:
        argv = [
            python_bin,
            "-m",
            _LAUNCHER_MODULE,
            "--template",
            template,
            "--mount-wait-sec",
            str(options.mount_wait_sec),
            "--mount-poll-sec",
            str(options.mount_poll_sec),
            "--shutdown-wait-sec",
            str(options.shutdown_wait_sec),
            "--lock-wait-sec",
            str(options.lock_wait_sec),
            "--copy-chunk-bytes",
            str(options.copy_chunk_bytes),
            "--max-memory-bytes",
            str(limits.max_memory_bytes),
            "--max-cpu-sec",
            str(limits.max_cpu_sec),
            "--max-file-size-bytes",
            str(limits.max_file_size_bytes),
            "--max-open-files",
            str(limits.max_open_files),
            "--oom-score-adj",
            str(limits.oom_score_adj),
        ]

        for image, mnt in images:
            argv += ["--image", image, mnt]

        argv += list(op)

        return argv


def build_chain_argv(  # noqa: PLR0913
    *,
    images: Sequence[tuple[str, str]],
    template: str,
    op: Sequence[str],
    python_bin: str,
    options: LauncherOptions,
    limits: ResourceLimits,
    rw_paths: Sequence[str] = (),
    network: bool = False,
    bwrap_bin: str = "bwrap",
    channel_env: Mapping[str, str] | None = None,
) -> list[str]:
    """Цепочка файловых операций над образом: опции внешнего bwrap едут в argv.

    images — пары (образ, mountpoint); op — write/read/delete плюс аргумент.
    Запуск инструмента собирает `ChainChannelArgv.build`: там профиль уходит
    каналом wrap_args.
    """
    bwrap_path = ChainChannelArgv._bwrap_path(bwrap_bin)
    pairs = ChainChannelArgv._absolute_images(images)

    argv = [bwrap_path]
    argv += ChainChannelArgv._bwrap_options(
        pairs, rw_paths=rw_paths, network=network, channel_env=channel_env
    )
    argv.append("--")
    argv += ChainChannelArgv._launcher_argv(
        python_bin=python_bin,
        template=template,
        options=options,
        limits=limits,
        images=pairs,
        op=op,
    )

    return argv


def render_image_path(template: str, variables: Mapping[str, str]) -> str:
    try:
        return template.format_map(dict(variables))
    except KeyError as e:
        msg = f"workspace: variable {{{e.args[0]}}} in path {template!r} is not defined"
        raise MountError(msg) from e


class LauncherConfig(BaseModel):
    """Тайминги и размеры операций лаунчера образов; задаются явно."""

    model_config = ConfigDict(extra="ignore")

    mount_wait_sec: float = Field(
        gt=0,
        description="Сколько ждать появления fuse-монтирования, сек.",
    )
    mount_poll_sec: float = Field(
        gt=0,
        description="Период опроса mountinfo при ожидании монтирования, сек.",
    )
    shutdown_wait_sec: float = Field(
        gt=0,
        description="Сколько ждать штатного выхода fuse2fs после SIGTERM, сек.",
    )
    lock_wait_sec: float = Field(
        gt=0,
        description="Сколько ждать flock на образе, сек.",
    )
    copy_chunk_bytes: int = Field(
        gt=0,
        description="Размер блока sparse-копирования шаблонного образа, байт.",
    )

    def to_options(self) -> LauncherOptions:
        return LauncherOptions(
            mount_wait_sec=self.mount_wait_sec,
            mount_poll_sec=self.mount_poll_sec,
            shutdown_wait_sec=self.shutdown_wait_sec,
            lock_wait_sec=self.lock_wait_sec,
            copy_chunk_bytes=self.copy_chunk_bytes,
        )


@dataclass(frozen=True)
class LauncherOptions:
    """Тайминги и размеры лаунчера; значения приходят из профиля."""

    mount_wait_sec: float
    mount_poll_sec: float
    shutdown_wait_sec: float
    lock_wait_sec: float
    copy_chunk_bytes: int


@dataclass(frozen=True)
class ResourceLimits:
    """Лимиты команды: RLIMIT_AS/CPU/FSIZE/NOFILE + oom; 0 — не выставлять."""

    max_memory_bytes: int = 0
    max_cpu_sec: int = 0
    max_file_size_bytes: int = 0
    max_open_files: int = 0
    oom_score_adj: int = 0

    def apply_to_current_process(self) -> None:
        if self.max_memory_bytes:
            memory = (self.max_memory_bytes, self.max_memory_bytes)
            resource.setrlimit(resource.RLIMIT_AS, memory)
        if self.max_cpu_sec:
            cpu = (self.max_cpu_sec, self.max_cpu_sec)
            resource.setrlimit(resource.RLIMIT_CPU, cpu)
        if self.max_file_size_bytes:
            fsize = (self.max_file_size_bytes, self.max_file_size_bytes)
            resource.setrlimit(resource.RLIMIT_FSIZE, fsize)
        if self.max_open_files:
            nofile = (self.max_open_files, self.max_open_files)
            resource.setrlimit(resource.RLIMIT_NOFILE, nofile)
        if self.oom_score_adj:
            self._write_oom_score_adj("self", self.oom_score_adj)

    def apply_to_process(self, pid: int) -> None:
        """prlimit из родителя: не требует preexec_fn, безопасен при потоках."""
        if self.max_memory_bytes:
            memory = (self.max_memory_bytes, self.max_memory_bytes)
            resource.prlimit(pid, resource.RLIMIT_AS, memory)
        if self.max_cpu_sec:
            cpu = (self.max_cpu_sec, self.max_cpu_sec)
            resource.prlimit(pid, resource.RLIMIT_CPU, cpu)
        if self.max_file_size_bytes:
            fsize = (self.max_file_size_bytes, self.max_file_size_bytes)
            resource.prlimit(pid, resource.RLIMIT_FSIZE, fsize)
        if self.max_open_files:
            nofile = (self.max_open_files, self.max_open_files)
            resource.prlimit(pid, resource.RLIMIT_NOFILE, nofile)
        if self.oom_score_adj:
            self._write_oom_score_adj(str(pid), self.oom_score_adj)

    @staticmethod
    def _write_oom_score_adj(pid: str, value: int) -> None:
        """Поднять своему/чужому (тот же uid) процессу можно без привилегий."""
        with open(f"/proc/{pid}/oom_score_adj", "w") as f:
            f.write(str(value))


class WrapResultWriter:
    """Отчёт кодов стадий строками wrap_result; обрыв читателя отключает отчёт."""

    def __init__(self, fd: int) -> None:
        self._fd = fd
        self._broken = False

    def report(self, stage: str, returncode: int | None) -> None:
        if self._broken:
            return

        report = StageExit(stage=stage, rc=ShellExit.of(returncode))

        try:
            os.write(self._fd, report.encode_line())
        except BrokenPipeError:
            self._broken = True
            trace(f"wrap_result reader is gone, rc of stage {stage!r} is lost")


class StageEntry(BaseModel):
    """Запись стадии режима group: команда вложенного bwrap, каналы, лимиты, cgroup.

    Формирует приложение: `render` — в аргумент режима group, лаунчер разбирает
    `parse` на границе. Номера fd действительны внутри внешней песочницы:
    pass_fds сохраняет их по всей цепочке. Точные rlimit'ы стадии ставит
    preexec супервизора; `--max-*` самого лаунчера в группе несут огибающую.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    MIN_DYNAMIC_FD: ClassVar[int] = 3

    stage: str = Field(min_length=1)
    command: str = Field(min_length=1)
    stdin_fd: int | None = Field(ge=0)
    pass_fds: tuple[int, ...]
    limits: ResourceLimits
    cgroup_dir: str | None

    @field_validator("command")
    @classmethod
    def _splittable_command(cls, value: str) -> str:
        try:
            parts = shlex.split(value)
        except ValueError as exc:
            msg = f"unparsable command: {exc}"
            raise ValueError(msg) from exc

        if not parts:
            msg = "empty command"
            raise ValueError(msg)

        return value

    @field_validator("pass_fds")
    @classmethod
    def _dynamic_fds(cls, value: tuple[int, ...]) -> tuple[int, ...]:
        for fd in value:
            if fd < cls.MIN_DYNAMIC_FD:
                msg = f"channel fd {fd} clashes with stdio"
                raise ValueError(msg)

        return value

    @field_validator("stdin_fd")
    @classmethod
    def _dynamic_stdin(cls, value: int | None) -> int | None:
        if value is None:
            return None

        if value < cls.MIN_DYNAMIC_FD:
            msg = f"stdin fd {value} clashes with stdio"
            raise ValueError(msg)

        return value

    def render(self) -> str:
        return self.model_dump_json()

    @classmethod
    def parse(cls, raw: str) -> StageEntry:
        try:
            return cls.model_validate_json(raw)
        except ValidationError as exc:
            raise MountError(f"invalid stage entry: {exc}") from exc

    def argv(self) -> tuple[str, ...]:
        return tuple(shlex.split(self.command))

    def owned_fds(self) -> tuple[int, ...]:
        """Дескрипторы записи: канальные и stdin; свои копии супервизор закрывает
        после запуска стадий, иначе EOF каналов не дойдёт до приложения."""
        fds = list(self.pass_fds)

        if self.stdin_fd is not None:
            fds.append(self.stdin_fd)

        return tuple(fds)


@dataclass(frozen=True)
class _StagePlan:
    """Готовый к запуску вложенный bwrap одной стадии."""

    stage: str
    argv: tuple[str, ...]
    stdin_fd: int | None
    pass_fds: tuple[int, ...]
    limits: ResourceLimits
    cgroup_dir: str | None


class _SignalGuard:
    """TERM/INT на время супервизора: флаг вместо немедленной смерти процесса."""

    SIGNALS: ClassVar[tuple[int, ...]] = (signal.SIGTERM, signal.SIGINT)

    def __init__(self) -> None:
        self._received: int | None = None
        self._previous: list[tuple[int, _SignalHandler]] = []

    @property
    def received(self) -> int | None:
        return self._received

    def __enter__(self) -> _SignalGuard:
        for signum in self.SIGNALS:
            previous = signal.signal(signum, self._remember)
            self._previous.append((signum, previous))

        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        for signum, previous in self._previous:
            signal.signal(signum, previous)

        self._previous.clear()

    def _remember(self, signum: int, frame: FrameType | None) -> None:
        self._received = signum


class StageSupervisor:
    """Вложенные bwrap'ы mount-группы: preexec с cgroup и rlimit, отчёт rc, уборка.

    Все стадии стартуют одновременно, порядок работы задают данные в каналах.
    Смерть стадии группу не валит — решение принимает раннер по wrap_result и
    убивает внешний процесс целиком; сигнал TERM/INT гасит вложенных и
    завершает работу ошибкой обвязки.
    """

    POLL_SEC: ClassVar[float] = 0.05

    def __init__(
        self,
        plans: Sequence[_StagePlan],
        reporter: WrapResultWriter | None,
        close_after_spawn: Sequence[int],
    ) -> None:
        if reporter is None and len(plans) != 1:
            msg = "supervisor without wrap_result serves exactly one stage"
            raise MountError(msg)

        self._plans = list(plans)
        self._reporter = reporter
        self._close_after_spawn = list(close_after_spawn)

    def run(self) -> int:
        """Код обвязки; коды стадий с репортёром уезжают строками wrap_result,
        без репортёра (вырожденный одиночный run) — возвращаются напрямую."""
        procs: dict[str, subprocess.Popen[bytes]] = {}

        with _SignalGuard() as guard:
            try:
                for plan in self._plans:
                    procs[plan.stage] = self._spawn(plan)

                self._close_inherited()

                finished = self._watch(procs, guard)
            finally:
                self._kill_all(procs)

        if self._reporter is not None:
            return int(LauncherExit.OK)

        return finished[self._plans[0].stage]

    def _spawn(self, plan: _StagePlan) -> subprocess.Popen[bytes]:
        stdin_fd = subprocess.DEVNULL
        if plan.stdin_fd is not None:
            stdin_fd = plan.stdin_fd

        def prepare_child() -> None:
            FuseMounter.set_pdeathsig()

            if plan.cgroup_dir is not None:
                self._enter_cgroup(plan.cgroup_dir)

            plan.limits.apply_to_current_process()

        try:
            proc = subprocess.Popen(  # noqa: S603
                list(plan.argv),
                shell=False,
                stdin=stdin_fd,
                pass_fds=plan.pass_fds,
                preexec_fn=prepare_child,  # noqa: PLW1509
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise MountError(f"stage {plan.stage!r} spawn failed: {exc}") from exc

        trace(f"stage {plan.stage!r}: nested bwrap pid {proc.pid}")

        return proc

    @staticmethod
    def _enter_cgroup(cgroup_dir: str) -> None:
        """Запись «0» в cgroup.procs: работает и без трансляции pid в userns."""
        procs_path = os.path.join(cgroup_dir, "cgroup.procs")

        fd = os.open(procs_path, os.O_WRONLY)
        os.write(fd, b"0")
        os.close(fd)

    def _close_inherited(self) -> None:
        """Свои копии fd стадий: пока они открыты, EOF каналов не наступит."""
        for fd in self._close_after_spawn:
            os.close(fd)

        self._close_after_spawn.clear()

    def _watch(
        self, procs: Mapping[str, subprocess.Popen[bytes]], guard: _SignalGuard
    ) -> dict[str, int]:
        finished: dict[str, int] = {}

        while len(finished) < len(procs):
            if guard.received is not None:
                msg = f"terminated by signal {guard.received}"
                raise MountError(msg)

            progressed = self._reap_finished(procs, finished)
            if not progressed:
                time.sleep(self.POLL_SEC)

        return finished

    def _reap_finished(
        self,
        procs: Mapping[str, subprocess.Popen[bytes]],
        finished: dict[str, int],
    ) -> bool:
        progressed = False

        for stage, proc in procs.items():
            if stage in finished:
                continue

            returncode = proc.poll()
            if returncode is None:
                continue

            rc = ShellExit.of(returncode)
            finished[stage] = rc
            progressed = True
            trace(f"stage {stage!r} exited rc={rc}")

            if self._reporter is not None:
                self._reporter.report(stage, returncode)

        return progressed

    @staticmethod
    def _kill_all(procs: Mapping[str, subprocess.Popen[bytes]]) -> None:
        for proc in procs.values():
            if proc.poll() is None:
                proc.kill()

        for proc in procs.values():
            proc.wait()


if __name__ == "__main__":
    sys.exit(Launcher.main(sys.argv[1:]))
