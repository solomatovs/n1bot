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
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from enum import IntEnum, StrEnum
from typing import BinaryIO, ClassVar

from pydantic import BaseModel, ConfigDict, Field, field_validator

from boba.toolkit.timing import Elapsed
from boba.workspace.binaries import (
    SandboxBinary,
    TrustedBinaries,
    UntrustedBinaryError,
)
from boba.workspace.images import (
    FuseMounter,
    ImageStore,
    LauncherMarker,
    LauncherOptions,
    MountError,
    SparseCopier,
    trace,
)
from boba.workspace.timing import ProcessAge

__all__ = [
    "CapabilityDropper",
    "FileHead",
    "FileOperations",
    "HostPath",
    "ImageMountPoint",
    "ImagePath",
    "Launcher",
    "LauncherChain",
    "LauncherEnv",
    "LauncherExit",
    "LauncherInterpreter",
    "LauncherMode",
    "LauncherView",
    "LayerPath",
    "LayerSources",
    "MountingConfig",
    "NotRegularFileError",
    "OsLoader",
    "ReadHeader",
    "ReadWindow",
    "ResourceLimits",
    "ViewBind",
    "render_image_path",
]


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
            msg = (
                f"read header: expected a line starting with {cls.PREFIX!r}, "
                f"got {line[:200]!r}"
            )
            raise ValueError(msg)

        fields = line[len(cls.PREFIX) :].strip().split()
        if not fields:
            msg = (
                f"read header: expected `{cls.PREFIX.decode()}<size> "
                f"{cls.REVISION.decode()}<revision>`, got {line[:200]!r}"
            )
            raise ValueError(msg)

        revision = 0
        for field in fields[1:]:
            if field.startswith(cls.REVISION):
                revision = int(field[len(cls.REVISION) :])

        return FileHead(size=int(fields[0]), revision=revision)


class LauncherEnv(StrEnum):
    """Окружение интерпретатора лаунчера: с ним портативный python находит
    себя, свои библиотеки и код; всё остальное внешний слой стирает."""

    PATH = "PATH"
    LD_LIBRARY_PATH = "LD_LIBRARY_PATH"
    PYTHONHOME = "PYTHONHOME"
    PYTHONPATH = "PYTHONPATH"


class LauncherInterpreter(BaseModel):
    """Интерпретатор лаунчера цепочки: исполняемый файл, его окружение и корни.

    Лаунчер — та же программа, что и процесс приложения: подкоманда launcher
    собранного бинарника или модуль лаунчера интерпретатором исходников. Во
    внешнем слое цепочки он видит только явный список путей (LauncherView) и
    приносит в него свои. Это факт процесса, а не конфиг: его даёт ProgramRoot
    (boba.runtime.config), его получают построитель цепочки LauncherChain
    хранилища образов и реестр зигот способа запуска песочницы.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    executable: str = Field(
        min_length=1,
        description=(
            "Абсолютный путь исполняемого файла процесса (sys.executable): "
            "интерпретатор исходников или бинарник собранной программы."
        ),
    )
    env: dict[LauncherEnv, str] = Field(
        description=(
            "Заданные у процесса переменные LauncherEnv: с ними лаунчер стартует "
            "во внешнем слое."
        ),
    )
    roots: tuple[str, ...] = Field(
        description=(
            "Абсолютные пути, без которых интерпретатор не стартует: prefix, "
            "каталог исполняемого файла, каталоги sys.path и LD_LIBRARY_PATH. "
            "Текущего каталога процесса и его предков среди них нет: в нём "
            "лежит дерево установки с конфигом и данными."
        ),
    )
    launcher: tuple[str, ...] = Field(
        min_length=1,
        description=(
            "Команда лаунчера до его опций: подкоманда launcher собранной "
            "программы или модуль лаунчера интерпретатором исходников."
        ),
    )
    probe: tuple[str, ...] = Field(
        min_length=1,
        description=(
            "Пустой процесс, которым проба cgroup проверяет вход ребёнка в "
            "leaf: подкоманда probe собранной программы или пустая строка "
            "кода интерпретатором исходников."
        ),
    )

    @field_validator("executable", mode="after")
    @classmethod
    def _absolute_executable(cls, value: str) -> str:
        if not os.path.isabs(value):
            msg = f"launcher interpreter: executable expects an absolute path, got {value!r}"
            raise ValueError(msg)

        return value

    @field_validator("roots", mode="after")
    @classmethod
    def _absolute_roots(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for root in value:
            if os.path.isabs(root):
                continue

            msg = (
                f"launcher interpreter: roots expect absolute paths, got {root!r} "
                f"among {list(value)}"
            )
            raise ValueError(msg)

        return value


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
            msg = f"launcher argv: host path must be absolute, got {raw!r}"
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
            code = ctypes.get_errno()
            msg = f"capset(drop all capabilities) failed: {os.strerror(code)}"
            raise OSError(code, msg)


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
            msg = (
                f"image path: invalid relative path {rel!r}, "
                f"it must stay inside the image root"
            )
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
                msg = f"run: empty command, got argument {argument!r}"
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
            msg = (
                f"{mode}: exactly one argument expected, "
                f"got {len(op_args)}: {op_args!r}"
            )
            raise MountError(msg)
        return op_args[0]

    READ_ARGS: ClassVar[int] = 3
    """Аргументы read: относительный путь, offset, length."""

    @classmethod
    def _read_arguments(cls, op_args: list[str]) -> tuple[str, ReadWindow]:
        if len(op_args) != cls.READ_ARGS:
            msg = (
                f"read: expected <rel> <offset> <length>, "
                f"got {len(op_args)} args: {op_args!r}"
            )
            raise MountError(msg)

        try:
            window = ReadWindow.from_argv(op_args[1], op_args[2])
        except ValueError as e:
            msg = (
                f"read {op_args[0]!r}: invalid window "
                f"offset={op_args[1]!r} length={op_args[2]!r}: {e}"
            )
            raise MountError(msg) from e

        return op_args[0], window


class LayerPath(StrEnum):
    """Точки внешнего слоя, которые цепочка ставит сама: корень, устройства, procfs."""

    ROOT = "/"
    DEV = "/dev"
    FUSE = "/dev/fuse"
    PROC = "/proc"


class OsLoader(StrEnum):
    """Пути загрузчика ОС, которые внешний слой видит всегда.

    Это свойство платформы, а не развёртывания: ld.so каждого ELF цепочки
    (PT_INTERP /lib64/ld-linux-x86-64.so.2), libc и библиотеки модулей
    python (_ctypes, _ssl). Путь-ссылка ставится в слое ссылкой, путь,
    которого на машине нет, пропускается.
    """

    USR = "/usr"
    LIB = "/lib"
    LIB64 = "/lib64"
    LD_CACHE = "/etc/ld.so.cache"


class ViewBind(StrEnum):
    """Режим пути в виде внешнего слоя; значение — флаг бинда bwrap."""

    RO = "--ro-bind"
    RW = "--bind"


@dataclass(frozen=True)
class LayerSources:
    """Источники биндов вложенного слоя: их он ищет в виде внешнего."""

    ro: tuple[str, ...]
    rw: tuple[str, ...]


class LauncherView:
    """Явный вид внешнего слоя цепочки лаунчера вместо корня хоста.

    bwrap перемонтирует на чтение каждую точку под путём своего бинда, и
    точка хоста, исчезнувшая посреди этого цикла, роняет старт с EINVAL.
    Поэтому внешний слой видит не корень хоста, а только то, без чего
    цепочка не работает: загрузчик ОС, интерпретатор лаунчера, исполняемые
    файлы цепочки, образы и источники биндов вложенного слоя. Путей
    развёртывания вид не знает: их приносит построитель цепочки
    (LauncherChain), который его и держит.
    """

    MAX_LINKS: ClassVar[int] = 40
    """Сколько ссылок подряд разыменовывает ядро (MAXSYMLINKS)."""

    def __init__(self, interpreter: LauncherInterpreter) -> None:
        self._interpreter = interpreter

    def args(
        self,
        *,
        ro: Sequence[str],
        rw: Sequence[str],
        executables: Sequence[str],
        tmpfs: Sequence[str],
    ) -> list[str]:
        """Операции bwrap вида: ссылки, tmpfs под точки монтирования, бинды.

        Бинды идут по возрастанию глубины: путь под предком того же режима
        отбрасывается, путь с другим режимом (rw внутри ro) ложится поверх.
        """
        links = self._os_links()

        entries: dict[str, ViewBind] = {}
        loader = (*self._os_paths(), *self._interpreter.roots)
        for path in (*loader, *executables, *ro):
            self._add(entries, self._through(links, path), ViewBind.RO)

        for path in rw:
            self._add(entries, self._through(links, path), ViewBind.RW)

        kept = self._kept(entries)
        self._check_executables(executables, links, kept)

        argv: list[str] = []
        for target, path in links:
            argv += ["--symlink", target, path]

        for parent in dict.fromkeys(tmpfs):
            argv += ["--tmpfs", parent]

        for path, mode in kept:
            argv += [mode.value, path, path]

        return argv

    @staticmethod
    def _os_links() -> list[tuple[str, str]]:
        """Пути загрузчика-ссылки (/lib на слитом /usr): цель и сама ссылка."""
        links: list[tuple[str, str]] = []
        for path in OsLoader:
            if os.path.islink(path.value):
                links.append((os.readlink(path.value), path.value))

        return links

    @staticmethod
    def _os_paths() -> Iterator[str]:
        """Пути загрузчика; на месте ссылки в вид идёт её настоящий путь."""
        for path in OsLoader:
            yield os.path.realpath(path.value)

    def _through(self, links: Sequence[tuple[str, str]], path: str) -> str:
        """Абсолютный путь; под ссылкой загрузчика (/lib на слитом /usr) — через
        её настоящий путь."""
        normal = os.path.abspath(path)
        for _, link in links:
            if not self._covers(link, normal):
                continue

            return os.path.realpath(link) + normal[len(link) :]

        return normal

    @staticmethod
    def _add(entries: dict[str, ViewBind], path: str, mode: ViewBind) -> None:
        """Отсутствующий путь пропускается; rw сильнее ro того же пути."""
        if not os.path.exists(path):
            return

        if mode is ViewBind.RO and path in entries:
            return

        entries[path] = mode

    def _kept(self, entries: Mapping[str, ViewBind]) -> list[tuple[str, ViewBind]]:
        kept: list[tuple[str, ViewBind]] = []
        for path in sorted(entries, key=self._depth):
            mode = entries[path]
            owner = self._owner(path, kept)
            if owner is not None and owner[1] is mode:
                continue

            kept.append((path, mode))

        return kept

    @staticmethod
    def _depth(path: str) -> tuple[int, str]:
        return path.count("/"), path

    def _owner(
        self, path: str, kept: Sequence[tuple[str, ViewBind]]
    ) -> tuple[str, ViewBind] | None:
        """Самый глубокий принятый бинд над путём (или сам путь) и его режим."""
        for entry in reversed(kept):
            if self._covers(entry[0], path):
                return entry

        return None

    def _check_executables(
        self,
        executables: Sequence[str],
        links: Sequence[tuple[str, str]],
        kept: Sequence[tuple[str, ViewBind]],
    ) -> None:
        """Исполняемый файл цепочки обязан открываться внутри вида.

        Ссылка по дороге к нему, которая ведёт за пределы вида
        (/etc/alternatives), внутри слоя не откроется; молча её не чиним —
        бинарь кладётся в доверенный каталог развёртывания.
        """
        for executable in executables:
            if self._opens(self._through(links, executable), links, kept):
                continue

            real = os.path.realpath(executable)
            msg = (
                f"launcher view: chain executable {executable} resolves to {real} "
                "through a link outside the paths the outer sandbox layer sees; "
                "put a regular copy of the binary into the trusted directory of "
                "the program (its libexec directory)"
            )
            raise UntrustedBinaryError(msg)

    def _opens(
        self,
        path: str,
        links: Sequence[tuple[str, str]],
        kept: Sequence[tuple[str, ViewBind]],
    ) -> bool:
        """Путь открывается внутри вида: каждая ссылка по дороге видна в нём."""
        current = path
        for _ in range(self.MAX_LINKS):
            if self._owner(current, kept) is None:
                return False

            link = self._first_link(current, kept)
            if not link:
                return True

            target = os.path.join(os.path.dirname(link), os.readlink(link))
            rest = current[len(link) :]
            current = self._through(links, os.path.normpath(target + rest))

        return False

    def _first_link(self, path: str, kept: Sequence[tuple[str, ViewBind]]) -> str:
        """Первая ссылка пути, какой её видит слой; пусто — ссылок нет.

        Каталоги над биндами слой создаёт сам, а сам бинд монтирует уже
        разыменованный источник: ссылками внутри слоя остаются только записи
        под путём бинда.
        """
        prefix = ""
        for part in path.strip("/").split("/"):
            prefix = f"{prefix}/{part}"
            owner = self._owner(prefix, kept)
            if owner is None:
                continue

            if prefix == owner[0]:
                continue

            if os.path.islink(prefix):
                return prefix

        return ""

    @staticmethod
    def _covers(root: str, path: str) -> bool:
        if path == root:
            return True

        return path.startswith(root.rstrip("/") + "/")


class LauncherChain:
    """Построитель цепочки лаунчера: внешний bwrap с явным видом и команда
    лаунчера в нём.

    Внешний слой — userns с uid 0 и CAP_SYS_ADMIN: fuse2fs монтирует в нём
    образы, а --disable-userns с монтированием fuse несовместим. Владельцы
    цепочки — спавнер зиготы и хранилище образов — создают построитель в
    конструкторе из интерпретатора процесса, доверенных бинарей и таймингов
    монтирования и на каждый запуск зовут check и argv.
    """

    MODULE: ClassVar[str] = "boba.workspace.launcher"
    """Модуль лаунчера для запуска интерпретатором исходников."""

    def __init__(
        self,
        interpreter: LauncherInterpreter,
        binaries: TrustedBinaries,
        options: LauncherOptions,
    ) -> None:
        self._interpreter = interpreter
        self._binaries = binaries
        self._options = options
        self._view = LauncherView(interpreter)

    def check(self) -> None:
        """Предпосылки монтирования образов; падает громко и сразу."""
        if not os.path.exists(LayerPath.FUSE):
            msg = (
                f"workspace: mounting images requires fuse, "
                f"but {LayerPath.FUSE.value} is missing on the host"
            )
            raise RuntimeError(msg)

        self._binaries.resolve(SandboxBinary.FUSE2FS)
        self._binaries.resolve(SandboxBinary.BWRAP)

    def argv(  # noqa: PLR0913 — состав запуска задаётся целиком
        self,
        *,
        images: Sequence[tuple[str, str]],
        ro_images: Sequence[tuple[str, str]],
        template: str,
        op: Sequence[str],
        limits: ResourceLimits,
        pass_fds: Sequence[int],
        nested: LayerSources,
        network: bool,
        replace: bool,
    ) -> list[str]:
        """images — пары (образ, mountpoint) на запись; op — режим и аргументы."""
        bwrap = self._binaries.resolve(SandboxBinary.BWRAP)
        fuse2fs = self._binaries.resolve(SandboxBinary.FUSE2FS)

        rw_images: list[tuple[str, str]] = []
        for image, mnt in images:
            rw_images.append((os.path.abspath(image), os.path.abspath(mnt)))

        # корень цепочки read-only: относительные пути приводим к абсолютным
        read_images: list[tuple[str, str]] = []
        for image, mnt in ro_images:
            read_images.append((os.path.abspath(image), mnt))

        argv = [
            bwrap,
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
            # CAP_SETPCAP — исполнитель вызова сбрасывает им bounding set тела
            "--cap-add",
            "CAP_SETPCAP",
            "--unshare-pid",
            "--as-pid-1",
            "--unshare-ipc",
            "--unshare-uts",
            "--unshare-cgroup-try",
            "--hostname",
            "sandbox",
            "--new-session",
        ]
        argv += self._view.args(
            ro=self._ro_sources(read_images, template, nested),
            rw=self._rw_sources(rw_images, nested),
            executables=(self._interpreter.executable, bwrap, fuse2fs),
            tmpfs=self._mount_parents((*read_images, *rw_images)),
        )
        argv += [
            "--dev",
            LayerPath.DEV.value,
            "--dev-bind",
            LayerPath.FUSE.value,
            LayerPath.FUSE.value,
            "--proc",
            LayerPath.PROC.value,
            "--remount-ro",
            LayerPath.ROOT.value,
            "--chdir",
            LayerPath.ROOT.value,
            "--clearenv",
        ]
        for name, value in self._interpreter.env.items():
            argv += ["--setenv", name.value, value]

        if not network:
            argv.append("--unshare-net")

        argv += [
            "--",
            *self._interpreter.launcher,
            "--template",
            template,
            *self._flags(limits),
        ]
        if replace:
            argv.append(Launcher.EXEC_ARG)

        for fd in pass_fds:
            argv += [Launcher.PASS_FDS_ARG, str(fd)]

        for directory in self._binaries.dirs:
            argv += ["--trusted-bin-dir", directory]

        for image, mnt in read_images:
            argv += ["--ro-image", image, mnt]

        for image, mnt in rw_images:
            argv += ["--image", image, mnt]

        argv += list(op)
        return argv

    def _flags(self, limits: ResourceLimits) -> list[str]:
        return [
            "--mount-wait-sec",
            str(self._options.mount_wait_sec),
            "--mount-poll-sec",
            str(self._options.mount_poll_sec),
            "--shutdown-wait-sec",
            str(self._options.shutdown_wait_sec),
            "--lock-wait-sec",
            str(self._options.lock_wait_sec),
            "--copy-chunk-bytes",
            str(self._options.copy_chunk_bytes),
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

    @staticmethod
    def _ro_sources(
        images: Sequence[tuple[str, str]], template: str, nested: LayerSources
    ) -> list[str]:
        """Образы корня, шаблон и ro-источники вложенного слоя."""
        sources: list[str] = []
        for image, _ in images:
            sources.append(image)

        # пустой шаблон законен: без rw-образа он не нужен
        if template:
            sources.append(os.path.abspath(template))

        sources.extend(nested.ro)
        return sources

    @staticmethod
    def _rw_sources(
        images: Sequence[tuple[str, str]], nested: LayerSources
    ) -> list[str]:
        """Каталоги rw-образов (в них .lock и копия шаблона) и rw-источники."""
        sources: list[str] = []
        for image, _ in images:
            sources.append(os.path.dirname(image))

        sources.extend(nested.rw)
        return sources

    @staticmethod
    def _mount_parents(images: Sequence[tuple[str, str]]) -> list[str]:
        """Точку монтирования надо где-то создать: её родитель — tmpfs."""
        parents: list[str] = []
        for _, mnt in images:
            parents.append(os.path.dirname(mnt))

        return parents


def render_image_path(template: str, variables: Mapping[str, str]) -> str:
    try:
        return template.format_map(dict(variables))
    except KeyError as e:
        known = ", ".join(sorted(variables))
        msg = (
            f"workspace: variable {{{e.args[0]}}} in image path {template!r} "
            f"is not defined (known: {known})"
        )
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
