"""Лаунчер операций с workspace-образом: flock -> fuse2fs -> операция -> уборка.

Исполняется `python -m` внутри outer bwrap (см. chain.py), поэтому только
stdlib; mount снимается ядром при гибели поддерева процессов.
"""

from __future__ import annotations

import argparse
import contextlib
import ctypes
import errno
import fcntl
import os
import shlex
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from typing import BinaryIO, ClassVar

from boba.chainlit2.workspace.options import LauncherOptions, ResourceLimits

__all__ = [
    "EXIT_MOUNT_ERROR",
    "EXIT_NOT_FOUND",
    "LAUNCHER_ERROR_PREFIX",
    "LAUNCHER_LOG_PREFIX",
    "CapabilityDropper",
    "FileOperations",
    "FuseMounter",
    "ImageStore",
    "Launcher",
    "LauncherOptions",
    "MountError",
    "SparseCopier",
    "trace",
]

EXIT_MOUNT_ERROR = 2
EXIT_NOT_FOUND = 3

LAUNCHER_LOG_PREFIX = "sandbox-mount: "
"""Маркер трассировки в stderr: хост уводит такие строки в лог, LLM их не видит."""

LAUNCHER_ERROR_PREFIX = "sandbox-mount-error: "
"""Отдельный маркер сбоя: по нему хост отличает его от обычной трассировки."""


def trace(message: str) -> None:
    """Ход монтирования — только в stderr: stdout занят данными операции."""
    print(f"{LAUNCHER_LOG_PREFIX}{message}", file=sys.stderr, flush=True)  # noqa: T201


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
        trace(
            f"{image} mounted at {mnt} in {elapsed_ms}ms "
            f"(fuse2fs pid {daemon.pid})"
        )

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

    def write(self, rel: str, src: BinaryIO) -> int:
        path = self._resolve(rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            shutil.copyfileobj(src, f)
            f.flush()
            os.fsync(f.fileno())
        return 0

    def read(self, rel: str, dst: BinaryIO) -> int:
        try:
            f = open(self._resolve(rel), "rb")  # noqa: SIM115
        except FileNotFoundError:
            return EXIT_NOT_FOUND
        with f:
            shutil.copyfileobj(f, dst)
        dst.flush()
        return 0

    def delete(self, rel: str) -> int:
        try:
            os.remove(self._resolve(rel))
        except FileNotFoundError:
            return EXIT_NOT_FOUND
        return 0

    def _resolve(self, rel: str) -> str:
        norm = os.path.normpath(rel)
        if norm.startswith(("/", "..")):
            msg = f"invalid relative path {rel!r}"
            raise MountError(msg)
        return os.path.join(self._root, norm)


class Launcher:
    """Оркестратор: валидация -> локи и образы -> mount -> операция -> гашение."""

    MODES: ClassVar[tuple[str, ...]] = ("run", "write", "read", "delete")

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
            print(f"{LAUNCHER_ERROR_PREFIX}{e}", file=sys.stderr)  # noqa: T201
            return EXIT_MOUNT_ERROR

    def run(self, mode: str, op_args: list[str]) -> int:
        operation = self._plan(mode, op_args)
        started = time.monotonic()
        trace(f"operation {mode!r}, images: {len(self._images)}")
        for image, _ in self._images:
            self._store.acquire(image)
        mounter = FuseMounter(self._options, pass_fds=self._store.lock_fds)
        try:
            for image, mnt in self._images:
                mounter.mount(image, mnt)
            code = operation()
            elapsed_ms = int((time.monotonic() - started) * 1000)
            trace(f"operation {mode!r} finished rc={code} in {elapsed_ms}ms")
            return code
        finally:
            mounter.shutdown()

    def _block_new_userns(self) -> None:
        """Нужно только перед чужим кодом: файловые операции — наш же код."""
        with open(self.USERNS_SYSCTL, "w") as f:
            f.write("0")
        trace(f"{self.USERNS_SYSCTL}=0: nested user namespaces denied to the command")

    def _plan(self, mode: str, op_args: list[str]) -> Callable[[], int]:
        if mode not in self.MODES:
            msg = f"unknown operation {mode!r}"
            raise MountError(msg)
        argument = self._single_argument(mode, op_args)
        if mode == "run":
            argv = shlex.split(argument)
            if not argv:
                msg = "run: empty command"
                raise MountError(msg)
            return lambda: self._run_command(argv)
        return lambda: self._file_operation(mode, argument)

    def _file_operation(self, mode: str, argument: str) -> int:
        ops = FileOperations(self._images[0][1])
        CapabilityDropper().drop_all()
        if mode == "write":
            return ops.write(argument, sys.stdin.buffer)
        if mode == "read":
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
        parser.add_argument("mode", choices=cls.MODES)
        parser.add_argument("args", nargs=argparse.REMAINDER)
        return parser.parse_args(argv)

    @staticmethod
    def _single_argument(mode: str, op_args: list[str]) -> str:
        if len(op_args) != 1:
            msg = f"{mode}: exactly one argument expected, got {len(op_args)}"
            raise MountError(msg)
        return op_args[0]


if __name__ == "__main__":
    sys.exit(Launcher.main(sys.argv[1:]))
