"""Запуск в workspace-образе: fuse2fs-цепочка, её конфиг и лимиты процесса."""

from __future__ import annotations

import argparse
import ctypes
import errno
import os
import resource
import shlex
import shutil
import stat as stat_module
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import IntEnum, StrEnum
from typing import BinaryIO, ClassVar

from pydantic import BaseModel, ConfigDict, Field

from boba.toolkit.binaries import (
    SandboxBinary,
    TrustedBinaries,
    UntrustedBinaryError,
)
from boba.toolkit.images import (
    FuseMounter,
    ImageStore,
    LauncherMarker,
    LauncherOptions,
    MountError,
    SparseCopier,
    trace,
)
from boba.toolkit.timing import Elapsed, ProcessAge

__all__ = [
    "FUSE_DEVICE",
    "CapabilityDropper",
    "FileHead",
    "FileOperations",
    "HostPath",
    "ImageMountPoint",
    "ImagePath",
    "Launcher",
    "LauncherEnv",
    "LauncherExit",
    "LauncherMode",
    "MountingConfig",
    "NotRegularFileError",
    "ReadHeader",
    "ReadWindow",
    "ResourceLimits",
    "build_chain_argv",
    "render_image_path",
    "require_fuse",
]


"""Точки монтирования ro-образов: tmpfs цепочки, на хосте ничего не пишется.

Каталог песочницы в дереве установки только для чтения, поэтому точку рядом
с образом там не создать; tmpfs накрывает существующий /tmp, который есть в
любом корне — bwrap не умеет заводить каталог под tmpfs на read-only корне.
"""


class LauncherExit(IntEnum):
    """Коды возврата лаунчера."""

    OK = 0
    MOUNT_ERROR = MountError.EXIT_CODE
    NOT_FOUND = 3
    NO_SPACE = 4
    NOT_REGULAR = 5


class LauncherMode(StrEnum):
    """Операции лаунчера над смонтированным образом."""

    RUN = "run"
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


class FileHead(BaseModel):
    """Свойства файла из заголовка читающих операций: размер и версия.

    revision — момент последней записи в наносекундах: по нему видно правку,
    не изменившую размер (переписанная строка той же длины).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    size: int = Field(ge=0)
    revision: int = Field(ge=0)


class ReadHeader:
    """Первая строка stdout читающих операций: размер файла и его версия.

    Заголовок без revision принимается как нулевая версия: так читается
    вывод лаунчера прошлой сборки, оставшийся в собранном образе.
    """

    PREFIX: ClassVar[bytes] = b"size="
    REVISION: ClassVar[bytes] = b"rev="

    @classmethod
    def render(cls, head: FileHead) -> bytes:
        size = str(head.size).encode("ascii")
        revision = str(head.revision).encode("ascii")

        return cls.PREFIX + size + b" " + cls.REVISION + revision + b"\n"

    @classmethod
    def parse(cls, line: bytes) -> FileHead:
        if not line.startswith(cls.PREFIX):
            msg = f"invalid read header: {line!r}"
            raise ValueError(msg)

        fields = line[len(cls.PREFIX) :].strip().split()
        if not fields:
            msg = f"invalid read header: {line!r}"
            raise ValueError(msg)

        revision = 0
        for field in fields[1:]:
            if field.startswith(cls.REVISION):
                revision = int(field[len(cls.REVISION) :])

        return FileHead(size=int(fields[0]), revision=revision)


class LauncherEnv(StrEnum):
    """Переменные окружения портативного python: без них лаунчер не стартует."""

    LD_LIBRARY_PATH = "LD_LIBRARY_PATH"
    PYTHONHOME = "PYTHONHOME"
    PYTHONPATH = "PYTHONPATH"


class HostPath:
    """Путь на хосте, пришедший в argv лаунчера.

    Приводится к каноничному виду (`..` и симлинки раскрываются) и обязан быть
    абсолютным: относительный или уводящий наружу путь дальше не проходит.
    """

    @classmethod
    def checked(cls, raw: str) -> str:
        # пустой --template законен: без rw-образа шаблон не нужен
        if not raw:
            return ""

        path = os.path.realpath(raw)
        if not os.path.isabs(path):
            msg = f"path must be absolute: {raw!r}"
            raise argparse.ArgumentTypeError(msg)

        return path


class NotRegularFileError(RuntimeError):
    """По пути в образе лежит не обычный файл: каталог, канал или устройство."""


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
            print(  # noqa: T201
                f"{LauncherMarker.ERROR}no space left in the workspace image "
                f"for {rel!r}",
                file=sys.stderr,
            )
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
            fd, head = self._open_regular(rel)
        except OSError as e:
            return self._refused(rel, e)
        except NotRegularFileError:
            return self._not_regular(rel)

        size = head.size

        with os.fdopen(fd, "rb") as f:
            dst.write(ReadHeader.render(head))

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
        """Только заголовок: существование и версия без чтения тела."""
        try:
            fd, head = self._open_regular(rel)
        except OSError as e:
            return self._refused(rel, e)
        except NotRegularFileError:
            return self._not_regular(rel)

        os.close(fd)

        dst.write(ReadHeader.render(head))
        dst.flush()
        return LauncherExit.OK

    def _open_regular(self, rel: str) -> tuple[int, FileHead]:
        """Описатель обычного файла и его свойства; тип берётся с открытого fd."""
        fd = self._path.open_read(rel)
        try:
            return fd, self._require_regular(fd)
        except BaseException:
            os.close(fd)
            raise

    @staticmethod
    def _require_regular(fd: int) -> FileHead:
        """Свойства открытого файла; всё, что не обычный файл, — отказ."""
        info = os.fstat(fd)
        if not stat_module.S_ISREG(info.st_mode):
            raise NotRegularFileError

        return FileHead(size=info.st_size, revision=info.st_mtime_ns)

    @classmethod
    def _refused(cls, rel: str, failure: OSError) -> LauncherExit:
        if failure.errno == errno.ENOENT:
            return LauncherExit.NOT_FOUND

        if failure.errno in ImagePath.REFUSED_ERRNO:
            return cls._not_regular(rel)

        raise failure

    @staticmethod
    def _not_regular(rel: str) -> LauncherExit:
        print(  # noqa: T201
            f"{LauncherMarker.ERROR}not a regular file: {rel!r}", file=sys.stderr
        )
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

    EXEC_ARG: ClassVar[str] = "--exec"
    """Заменить собой команду вместо запуска её ребёнком.

    Годится, когда лаунчеру нечего доделывать после команды: корень
    смонтирован read-only, писателей у него нет, а fuse2fs умирает по
    pdeathsig вместе с процессом, который его заменил. Так из дерева уходит
    процесс питона (~35 MB), висевший всю жизнь команды.
    """

    PASS_FDS_ARG: ClassVar[str] = "--pass-fd"  # noqa: S105 — имя опции, не секрет
    """Номер дескриптора канала инструмента; повторяется по одному на канал.

    Приложение открывает каналы на внешнем bwrap, лаунчер передаёт их номера
    вложенному bwrap как есть — иначе subprocess закрыл бы их и каналы не
    доехали бы до тела.
    """

    def __init__(  # noqa: PLR0913 — состав запуска задаётся целиком
        self,
        template: str,
        images: list[tuple[str, str]],
        ro_images: list[tuple[str, str]],
        options: LauncherOptions,
        limits: ResourceLimits,
        binaries: TrustedBinaries,
        channel_fds: Sequence[int] = (),
        replace: bool = False,
    ) -> None:
        self._channel_fds = tuple(channel_fds)
        self._replace = replace
        self._images = images
        self._ro_images = ro_images
        self._options = options
        self._limits = limits
        self._binaries = binaries
        self._store = ImageStore(
            template, SparseCopier(options.copy_chunk_bytes), options.lock_wait_sec
        )

    @classmethod
    def main(cls, argv: list[str]) -> int:
        # возраст процесса = bwrap + старт python с импортами до этой строки
        trace(f"launcher up {ProcessAge.ms()}ms after exec (bwrap + python startup)")

        args = cls._parse_args(argv)
        images: list[tuple[str, str]] = []
        for pair in args.image:
            images.append((pair[0], pair[1]))

        ro_images: list[tuple[str, str]] = []
        for pair in args.ro_image:
            ro_images.append((pair[0], pair[1]))
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
        binaries = TrustedBinaries(dirs=tuple(args.trusted_bin_dir))
        launcher = cls(
            args.template,
            images,
            ro_images,
            options,
            limits,
            binaries,
            tuple(args.pass_fd),
            replace=args.replace,
        )
        try:
            return launcher.run(args.mode, args.args)
        except (MountError, UntrustedBinaryError, OSError, ValueError) as e:
            print(f"{LauncherMarker.ERROR}{e}", file=sys.stderr)  # noqa: T201
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

        # образ только для чтения (корень песочницы) неизменен: его кладёт
        # сборка, писателей у него нет, поэтому лок не берётся — рядом с ним
        # в дереве установки и нет места под lock-файл
        for image, _ in self._ro_images:
            if not os.path.exists(image):
                trace(f"read-only image {image} does not exist")
                return LauncherExit.NOT_FOUND

        mounter = FuseMounter(
            self._options, self._binaries, pass_fds=self._store.lock_fds
        )
        try:
            for image, mnt in self._ro_images:
                mounter.mount(image, mnt, readonly=True)

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

    def _run_command(self, argv: list[str]) -> int:
        self._block_new_userns()
        trace(
            f"command limits: memory={self._limits.max_memory_bytes}B "
            f"cpu={self._limits.max_cpu_sec}s file={self._limits.max_file_size_bytes}B "
            f"open_files={self._limits.max_open_files} "
            f"oom_score_adj={self._limits.oom_score_adj}"
        )

        channel_fds = self._channel_fds
        if channel_fds:
            trace(f"passing channel fds through: {channel_fds}")

        if self._replace:
            self._exec_command(argv, channel_fds)

        def prepare_child() -> None:
            FuseMounter.set_pdeathsig()
            self._limits.apply_to_current_process()

        elapsed = Elapsed()
        rc = subprocess.call(  # noqa: S603
            argv,
            shell=False,
            pass_fds=self._store.lock_fds + channel_fds,
            preexec_fn=prepare_child,
        )
        trace(f"command exited rc={rc} in {elapsed.ms()}ms")

        return rc

    def _exec_command(self, argv: list[str], channel_fds: tuple[int, ...]) -> None:
        """Заменить собой команду; из этого вызова не возвращаются.

        Дескрипторы каналов и локов переживают exec только с снятым
        FD_CLOEXEC. Лимиты ставятся себе — они наследуются заменённым образом.
        """
        for fd in (*self._store.lock_fds, *channel_fds):
            os.set_inheritable(fd, True)

        self._limits.apply_to_current_process()

        trace(f"replacing the launcher with the command: {argv[0]}")
        os.execv(argv[0], argv)  # noqa: S606

    @classmethod
    def _parse_args(cls, argv: list[str]) -> argparse.Namespace:
        parser = argparse.ArgumentParser(prog="workspace-launcher")
        parser.add_argument("--template", required=True, type=HostPath.checked)
        parser.add_argument(
            "--image",
            nargs=2,
            action="append",
            metavar=("IMG", "MNT"),
            default=[],
            type=HostPath.checked,
        )
        parser.add_argument(
            "--ro-image",
            nargs=2,
            action="append",
            metavar=("IMG", "MNT"),
            default=[],
            type=HostPath.checked,
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
        parser.add_argument(
            cls.PASS_FDS_ARG, type=int, action="append", default=[], dest="pass_fd"
        )
        parser.add_argument(cls.EXEC_ARG, action="store_true", dest="replace")
        parser.add_argument("--oom-score-adj", type=int, required=True)
        parser.add_argument(
            "--trusted-bin-dir", action="append", metavar="DIR", required=True
        )
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


def require_fuse(binaries: TrustedBinaries) -> None:
    """Проверяет предпосылки монтирования образов; падает громко и сразу."""
    if not os.path.exists(FUSE_DEVICE):
        msg = f"workspace: fuse is required, but {FUSE_DEVICE} is missing"
        raise RuntimeError(msg)

    binaries.resolve(SandboxBinary.FUSE2FS)
    binaries.resolve(SandboxBinary.BWRAP)


def build_chain_argv(  # noqa: PLR0913, PLR0912, C901 — линейная сборка argv
    *,
    images: Sequence[tuple[str, str]],
    ro_images: Sequence[tuple[str, str]] = (),
    template: str,
    op: Sequence[str],
    python_bin: str,
    options: LauncherOptions,
    limits: ResourceLimits,
    binaries: TrustedBinaries,
    pass_fds: Sequence[int] = (),
    rw_paths: Sequence[str] = (),
    network: bool = False,
    replace: bool = False,
) -> list[str]:
    """images — пары (образ, mountpoint); op — run/write/read/delete + аргумент.
    CAP_SYS_ADMIN только в userns; --disable-userns несовместим с mount fuse."""
    bwrap_path = binaries.resolve(SandboxBinary.BWRAP)

    # корень песочницы read-only: относительные пути приводим к абсолютным
    absolute: list[tuple[str, str]] = []
    for image, mnt in images:
        absolute.append((os.path.abspath(image), os.path.abspath(mnt)))
    images = absolute

    ro_absolute: list[tuple[str, str]] = []
    for image, mnt in ro_images:
        ro_absolute.append((os.path.abspath(image), mnt))
    ro_images = ro_absolute
    argv = [
        bwrap_path,
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
        "--unshare-pid",
        "--as-pid-1",
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
    for image, _mnt in images:
        writable.append(os.path.dirname(image))
    for path in rw_paths:
        writable.append(os.path.abspath(path))
    for path in dict.fromkeys(writable):
        argv += ["--bind", path, path]
    mount_dirs: list[str] = []
    for _image, mnt in (*ro_images, *images):
        # точку монтирования надо где-то создать: её родитель — tmpfs
        parent = os.path.dirname(mnt)
        if parent in mount_dirs:
            continue

        mount_dirs.append(parent)

    for parent in mount_dirs:
        argv += ["--tmpfs", parent]

    argv += [
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
        argv += ["--setenv", name.value, value]
    if not network:
        argv.append("--unshare-net")
    argv += [
        "--",
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
    if replace:
        argv.append(Launcher.EXEC_ARG)

    for fd in pass_fds:
        argv += [Launcher.PASS_FDS_ARG, str(fd)]

    for directory in binaries.dirs:
        argv += ["--trusted-bin-dir", directory]

    for image, mnt in ro_images:
        argv += ["--ro-image", image, mnt]

    for image, mnt in images:
        argv += ["--image", image, mnt]
    argv += list(op)
    return argv


def render_image_path(template: str, variables: Mapping[str, str]) -> str:
    try:
        return template.format_map(dict(variables))
    except KeyError as e:
        msg = f"workspace: variable {{{e.args[0]}}} in path {template!r} is not defined"
        raise RuntimeError(msg) from e


class ImageMountPoint:
    """Точка монтирования образа внутри namespace цепочки; одна на весь код."""

    SUFFIX: ClassVar[str] = ".mnt"

    @classmethod
    def under(cls, mount_dir: str, image: str) -> str:
        name = os.path.basename(image)
        return os.path.join(mount_dir, f"{name}{cls.SUFFIX}")


class MountingConfig(BaseModel):
    """Тайминги и размеры операций монтирования образов; задаются явно.

    Одна запись на приложение: ими пользуется и цепочка запуска песочницы, и
    хранилище вложений.
    """

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


if __name__ == "__main__":
    sys.exit(Launcher.main(sys.argv[1:]))
