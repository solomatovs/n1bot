"""Запуск в workspace-образе: fuse2fs-цепочка, её конфиг и лимиты процесса."""

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
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import IntEnum, StrEnum
from typing import BinaryIO, ClassVar

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "FUSE_DEVICE",
    "CapabilityDropper",
    "FileOperations",
    "FuseMounter",
    "ImageStore",
    "Launcher",
    "LauncherConfig",
    "LauncherEnv",
    "LauncherExit",
    "LauncherMarker",
    "LauncherMode",
    "LauncherOptions",
    "MountError",
    "ResourceLimits",
    "SparseCopier",
    "build_chain_argv",
    "render_image_path",
    "require_fuse",
    "trace",
]


class LauncherExit(IntEnum):
    """Коды возврата лаунчера."""

    OK = 0
    MOUNT_ERROR = 2
    NOT_FOUND = 3
    NO_SPACE = 4


class LauncherMarker(StrEnum):
    """Маркеры строк stderr: хост отличает трассировку и сбой от чужого шума."""

    LOG = "sandbox-mount: "
    ERROR = "sandbox-mount-error: "


class LauncherMode(StrEnum):
    """Операции лаунчера над смонтированным образом."""

    RUN = "run"
    WRITE = "write"
    READ = "read"
    DELETE = "delete"


class LauncherEnv(StrEnum):
    """Переменные окружения портативного python: без них лаунчер не стартует."""

    LD_LIBRARY_PATH = "LD_LIBRARY_PATH"
    PYTHONHOME = "PYTHONHOME"
    PYTHONPATH = "PYTHONPATH"


def trace(message: str) -> None:
    """Ход монтирования — только в stderr: stdout занят данными операции."""
    print(f"{LauncherMarker.LOG}{message}", file=sys.stderr, flush=True)  # noqa: T201


class MountError(RuntimeError):
    """Сбой подготовки или монтирования образа."""


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

    def __init__(self, template: str, copier: SparseCopier) -> None:
        self._template = template
        self._copier = copier
        self._lock_fds: list[int] = []

    @property
    def lock_fds(self) -> tuple[int, ...]:
        return tuple(self._lock_fds)

    def acquire(self, image: str) -> None:
        started = time.monotonic()
        self._lock(image)
        waited_ms = int((time.monotonic() - started) * 1000)
        trace(f"lock on {image} acquired in {waited_ms}ms")
        if os.path.exists(image):
            trace(f"image {image} already exists ({os.path.getsize(image)} bytes)")
            return
        self._materialize(image)

    def release_all(self) -> None:
        for fd in self._lock_fds:
            os.close(fd)
        self._lock_fds.clear()

    def _lock(self, image: str) -> None:
        fd = os.open(image + self.LOCK_SUFFIX, os.O_WRONLY | os.O_CREAT, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        os.set_inheritable(fd, True)
        self._lock_fds.append(fd)

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

    def mount(self, image: str, mnt: str) -> None:
        fuse2fs = shutil.which("fuse2fs")
        if fuse2fs is None:
            msg = "fuse2fs not found in PATH"
            raise MountError(msg)
        os.makedirs(mnt, exist_ok=True)
        daemon = subprocess.Popen(  # noqa: S603
            [fuse2fs, "-f", image, mnt],
            shell=False,
            stdin=subprocess.DEVNULL,
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


class FileOperations:
    """write/read/delete по относительному пути внутри mountpoint."""

    def __init__(self, root: str) -> None:
        self._root = root

    NO_SPACE_ERRNO: ClassVar[frozenset[int]] = frozenset(
        (errno.ENOSPC, errno.EDQUOT, errno.EFBIG)
    )
    """Образ кончился: недописанный файл сносится, чтобы не занимать остаток."""

    def write(self, rel: str, src: BinaryIO) -> LauncherExit:
        path = self._resolve(rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        try:
            with open(path, "wb") as f:
                shutil.copyfileobj(src, f)
                f.flush()
                os.fsync(f.fileno())
        except OSError as e:
            if e.errno not in self.NO_SPACE_ERRNO:
                raise
            self._discard(path)
            print(  # noqa: T201
                f"{LauncherMarker.ERROR}no space left in the workspace image "
                f"for {rel!r}",
                file=sys.stderr,
            )
            return LauncherExit.NO_SPACE
        return LauncherExit.OK

    @staticmethod
    def _discard(path: str) -> None:
        try:
            os.remove(path)
        except OSError:
            trace(f"cannot remove partial file {path}")

    def read(self, rel: str, dst: BinaryIO) -> LauncherExit:
        try:
            f = open(self._resolve(rel), "rb")  # noqa: SIM115
        except FileNotFoundError:
            return LauncherExit.NOT_FOUND
        with f:
            shutil.copyfileobj(f, dst)
        dst.flush()
        return LauncherExit.OK

    def delete(self, rel: str) -> LauncherExit:
        try:
            os.remove(self._resolve(rel))
        except FileNotFoundError:
            return LauncherExit.NOT_FOUND
        return LauncherExit.OK

    def _resolve(self, rel: str) -> str:
        norm = os.path.normpath(rel)
        if norm.startswith(("/", "..")):
            msg = f"invalid relative path {rel!r}"
            raise MountError(msg)
        return os.path.join(self._root, norm)


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
    ) -> None:
        self._images = images
        self._options = options
        self._limits = limits
        self._store = ImageStore(template, SparseCopier(options.copy_chunk_bytes))

    @classmethod
    def main(cls, argv: list[str]) -> int:
        args = cls._parse_args(argv)
        images: list[tuple[str, str]] = []
        for pair in args.image:
            images.append((pair[0], pair[1]))
        options = LauncherOptions(
            mount_wait_sec=args.mount_wait_sec,
            mount_poll_sec=args.mount_poll_sec,
            shutdown_wait_sec=args.shutdown_wait_sec,
            copy_chunk_bytes=args.copy_chunk_bytes,
        )
        limits = ResourceLimits(
            max_memory_bytes=args.max_memory_bytes,
            max_cpu_sec=args.max_cpu_sec,
            max_file_size_bytes=args.max_file_size_bytes,
            max_open_files=args.max_open_files,
            oom_score_adj=args.oom_score_adj,
        )
        launcher = cls(args.template, images, options, limits)
        try:
            return launcher.run(args.mode, args.args)
        except (MountError, OSError, ValueError) as e:
            print(f"{LauncherMarker.ERROR}{e}", file=sys.stderr)  # noqa: T201
            return LauncherExit.MOUNT_ERROR

    def run(self, mode: LauncherMode, op_args: list[str]) -> int:
        operation = self._plan(mode, op_args)
        started = time.monotonic()
        trace(f"operation {mode.value!r}, images: {len(self._images)}")
        for image, _ in self._images:
            self._store.acquire(image)
        mounter = FuseMounter(self._options, pass_fds=self._store.lock_fds)
        try:
            for image, mnt in self._images:
                mounter.mount(image, mnt)
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
        argument = self._single_argument(mode, op_args)
        if mode is LauncherMode.RUN:
            argv = shlex.split(argument)
            if not argv:
                msg = "run: empty command"
                raise MountError(msg)
            return lambda: self._run_command(argv)
        return lambda: self._file_operation(mode, argument)

    def _file_operation(self, mode: LauncherMode, argument: str) -> LauncherExit:
        ops = FileOperations(self._images[0][1])
        CapabilityDropper().drop_all()
        if mode is LauncherMode.WRITE:
            return ops.write(argument, sys.stdin.buffer)
        if mode is LauncherMode.READ:
            return ops.read(argument, sys.stdout.buffer)
        return ops.delete(argument)

    def _run_command(self, argv: list[str]) -> int:
        self._block_new_userns()
        trace(
            f"command limits: memory={self._limits.max_memory_bytes}B "
            f"cpu={self._limits.max_cpu_sec}s file={self._limits.max_file_size_bytes}B "
            f"open_files={self._limits.max_open_files} "
            f"oom_score_adj={self._limits.oom_score_adj}"
        )

        def prepare_child() -> None:
            FuseMounter.set_pdeathsig()
            self._limits.apply_to_current_process()

        return subprocess.call(  # noqa: S603
            argv,
            shell=False,
            pass_fds=self._store.lock_fds,
            preexec_fn=prepare_child,
        )

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


_LAUNCHER_MODULE = "boba.workspace.launcher"

FUSE_DEVICE = "/dev/fuse"


def require_fuse() -> None:
    """Проверяет предпосылки монтирования образов; падает громко и сразу."""
    if not os.path.exists(FUSE_DEVICE):
        msg = f"workspace: fuse is required, but {FUSE_DEVICE} is missing"
        raise RuntimeError(msg)
    if shutil.which("fuse2fs") is None:
        msg = "workspace: fuse2fs not found in PATH"
        raise RuntimeError(msg)
    if shutil.which("bwrap") is None:
        msg = "workspace: bwrap not found in PATH"
        raise RuntimeError(msg)


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
) -> list[str]:
    """images — пары (образ, mountpoint); op — run/write/read/delete + аргумент.
    CAP_SYS_ADMIN только в userns; --disable-userns несовместим с mount fuse."""
    bwrap_path = shutil.which(bwrap_bin)
    if not bwrap_path:
        msg = f"workspace: {bwrap_bin!r} not found in PATH"
        raise RuntimeError(msg)
    # корень песочницы read-only: относительные пути приводим к абсолютным
    absolute: list[tuple[str, str]] = []
    for image, mnt in images:
        absolute.append((os.path.abspath(image), os.path.abspath(mnt)))
    images = absolute
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
        argv += ["--bind", path, path]
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


def render_image_path(template: str, variables: Mapping[str, str]) -> str:
    try:
        return template.format_map(dict(variables))
    except KeyError as e:
        msg = f"workspace: variable {{{e.args[0]}}} in path {template!r} is not defined"
        raise RuntimeError(msg) from e


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
    copy_chunk_bytes: int = Field(
        gt=0,
        description="Размер блока sparse-копирования шаблонного образа, байт.",
    )

    def to_options(self) -> LauncherOptions:
        return LauncherOptions(
            mount_wait_sec=self.mount_wait_sec,
            mount_poll_sec=self.mount_poll_sec,
            shutdown_wait_sec=self.shutdown_wait_sec,
            copy_chunk_bytes=self.copy_chunk_bytes,
        )


@dataclass(frozen=True)
class LauncherOptions:
    """Тайминги и размеры лаунчера; значения приходят из профиля."""

    mount_wait_sec: float
    mount_poll_sec: float
    shutdown_wait_sec: float
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


if __name__ == "__main__":
    sys.exit(Launcher.main(sys.argv[1:]))
