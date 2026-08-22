"""Unit-тесты лаунчера workspace-образов: каждый компонент в отдельности."""

from __future__ import annotations

import fcntl
import io
import os
import shlex
import signal
import subprocess
import sys
import threading
from pathlib import Path
from typing import ClassVar

import pytest

from boba.toolkit.binaries import TrustedBinaries
from boba.toolkit.images import (
    FuseMounter,
    ImageStore,
    LauncherMarker,
    LauncherOptions,
    MountError,
    PartialCopy,
    SparseCopier,
)
from boba.workspace.launcher import (
    FileOperations,
    Launcher,
    LauncherExit,
    MountingConfig,
    ReadHeader,
    ReadWindow,
    ResourceLimits,
    build_chain_argv,
)

CHUNK = 1 << 16


@pytest.fixture(autouse=True)
def chainlit_context() -> None:
    pass


@pytest.fixture
def template(tmp_path: Path) -> Path:
    path = tmp_path / "template.img"
    path.write_bytes(b"TEMPLATE")
    return path


_REQUIRED_FLAGS = (
    "--mount-wait-sec",
    "10.0",
    "--mount-poll-sec",
    "0.05",
    "--shutdown-wait-sec",
    "5.0",
    "--lock-wait-sec",
    "10.0",
    "--copy-chunk-bytes",
    "1048576",
    "--max-memory-bytes",
    "0",
    "--max-cpu-sec",
    "0",
    "--max-file-size-bytes",
    "0",
    "--max-open-files",
    "0",
    "--oom-score-adj",
    "0",
    "--trusted-bin-dir",
    "/usr/bin",
)


def _launcher_options(**kw: float) -> LauncherOptions:
    """Тайминги задаются явно: дефолтов у LauncherOptions нет."""
    values: dict[str, float] = {
        "mount_wait_sec": 10.0,
        "mount_poll_sec": 0.05,
        "shutdown_wait_sec": 5.0,
        "lock_wait_sec": 10.0,
        "copy_chunk_bytes": 1 << 20,
    }
    values.update(kw)
    return LauncherOptions(
        mount_wait_sec=values["mount_wait_sec"],
        mount_poll_sec=values["mount_poll_sec"],
        shutdown_wait_sec=values["shutdown_wait_sec"],
        lock_wait_sec=values["lock_wait_sec"],
        copy_chunk_bytes=int(values["copy_chunk_bytes"]),
    )


def _bin_dirs() -> list[str]:
    """В тестах каталоги берутся из PATH; в проде их задаёт конфиг."""
    dirs: list[str] = []

    for entry in os.environ.get("PATH", "").split(os.pathsep):
        if not entry.startswith("/"):
            continue

        dirs.append(entry)

    return dirs


def _trusted() -> TrustedBinaries:
    return TrustedBinaries(dirs=tuple(_bin_dirs()))


class TestSparseCopier:
    @staticmethod
    def _copy(src: Path, dst: Path) -> None:
        SparseCopier(CHUNK).copy(str(src), str(dst))

    def test_content_preserved(self, tmp_path: Path) -> None:
        src, dst = tmp_path / "src", tmp_path / "dst"
        data = bytes(range(256)) * 300
        src.write_bytes(data)
        self._copy(src, dst)
        if dst.read_bytes() != data:
            raise AssertionError("dst.read_bytes() == data")

    def test_holes_stay_holes(self, tmp_path: Path) -> None:
        src, dst = tmp_path / "src", tmp_path / "dst"
        with src.open("wb") as f:
            f.write(b"head")
            f.seek(8 * 1024 * 1024)
            f.write(b"tail")
        self._copy(src, dst)
        if dst.read_bytes() != src.read_bytes():
            raise AssertionError("dst.read_bytes() == src.read_bytes()")
        if dst.stat().st_blocks * 512 >= dst.stat().st_size:
            raise AssertionError("dst.stat().st_blocks * 512 < dst.stat().st_size")

    def test_zero_blocks_become_holes(self, tmp_path: Path) -> None:
        src, dst = tmp_path / "src", tmp_path / "dst"
        src.write_bytes(b"\0" * (4 * CHUNK))
        self._copy(src, dst)
        if dst.read_bytes() != src.read_bytes():
            raise AssertionError("dst.read_bytes() == src.read_bytes()")
        if dst.stat().st_blocks * 512 >= dst.stat().st_size:
            raise AssertionError("dst.stat().st_blocks * 512 < dst.stat().st_size")

    def test_empty_file(self, tmp_path: Path) -> None:
        src, dst = tmp_path / "src", tmp_path / "dst"
        src.touch()
        self._copy(src, dst)
        if dst.stat().st_size != 0:
            raise AssertionError("dst.stat().st_size == 0")


class TestImageStore:
    @staticmethod
    def _store(template: Path) -> ImageStore:
        return ImageStore(str(template), SparseCopier(CHUNK), lock_wait_sec=10.0)

    def test_creates_image_from_template(self, tmp_path: Path, template: Path) -> None:
        image = tmp_path / "img"
        store = self._store(template)
        try:
            store.acquire(str(image))
            if image.read_bytes() != b"TEMPLATE":
                raise AssertionError('image.read_bytes() == b"TEMPLATE"')
        finally:
            store.release_all()

    def test_existing_image_untouched(self, tmp_path: Path, template: Path) -> None:
        image = tmp_path / "img"
        image.write_bytes(b"EXISTING")
        store = self._store(template)
        try:
            store.acquire(str(image))
            if image.read_bytes() != b"EXISTING":
                raise AssertionError('image.read_bytes() == b"EXISTING"')
        finally:
            store.release_all()

    def test_lock_file_created(self, tmp_path: Path, template: Path) -> None:
        image = tmp_path / "img"
        store = self._store(template)
        try:
            store.acquire(str(image))
            if not ((tmp_path / ("img" + ImageStore.LOCK_SUFFIX)).exists()):
                raise AssertionError('(tmp_path / ("img" + ImageStore.LOCK_SUFFIX)).e…')
        finally:
            store.release_all()

    def test_busy_lock_raises_after_timeout(
        self, tmp_path: Path, template: Path
    ) -> None:
        image = tmp_path / "img"
        holder = self._store(template)
        waiter = ImageStore(str(template), SparseCopier(CHUNK), lock_wait_sec=0.2)
        try:
            holder.acquire(str(image))
            with pytest.raises(MountError, match="held by another"):
                waiter.acquire(str(image))
        finally:
            waiter.release_all()
            holder.release_all()

    def test_failed_materialize_releases_lock(self, tmp_path: Path) -> None:
        """Повтор после сбоя не должен упереться в собственный залоченный fd."""
        store = ImageStore(
            str(tmp_path / "absent"), SparseCopier(CHUNK), lock_wait_sec=0.2
        )
        image = tmp_path / "img"
        try:
            with pytest.raises(MountError, match="not found"):
                store.acquire(str(image))
            with pytest.raises(MountError, match="not found"):
                store.acquire(str(image))
        finally:
            store.release_all()

    def test_missing_template_raises_and_cleans_tmp(self, tmp_path: Path) -> None:
        store = ImageStore(
            str(tmp_path / "absent"), SparseCopier(CHUNK), lock_wait_sec=10.0
        )
        image = tmp_path / "img"
        try:
            with pytest.raises(MountError, match="not found"):
                store.acquire(str(image))
        finally:
            store.release_all()
        if image.exists():
            raise AssertionError("not image.exists()")
        leftovers: list[Path] = []
        for path in tmp_path.iterdir():
            if ".tmp." in path.name:
                leftovers.append(path)
        if leftovers:
            raise AssertionError("not leftovers")

    DEAD_PID: ClassVar[int] = 999_999
    """Pid, которого нет: владелец частичной копии умер, не докопировав её."""

    OWN_PID: ClassVar[int] = 1
    """Pid исполнителя вызова: в своём pid namespace он у всех равен 1."""

    def test_abandoned_partial_copy_removed_on_acquire(
        self, tmp_path: Path, template: Path
    ) -> None:
        image = tmp_path / "img"
        partial = Path(PartialCopy.render(str(image), self.DEAD_PID))
        partial.write_bytes(b"half a copy")
        store = self._store(template)

        try:
            store.acquire(str(image))
        finally:
            store.release_all()

        if not (image.exists()):
            raise AssertionError("image.exists()")
        if partial.exists():
            raise AssertionError("not partial.exists()")

    def test_partial_copy_with_a_live_pid_removed_on_acquire(
        self, tmp_path: Path, template: Path
    ) -> None:
        """Pid в имени копии ничего не решает: под эксклюзивным локом её нет."""
        image = tmp_path / "img"
        partial = Path(PartialCopy.render(str(image), self.OWN_PID))
        partial.write_bytes(b"copy in progress")
        store = self._store(template)

        try:
            store.acquire(str(image))
        finally:
            store.release_all()

        if partial.exists():
            raise AssertionError("копия под чужим pid всё равно должна быть убрана")

    def test_alien_name_is_not_a_partial_copy(self, tmp_path: Path) -> None:
        image = str(tmp_path / "img")

        if PartialCopy.owner_of(image, f"{image}.tmp.notapid") is not None:
            raise AssertionError('PartialCopy.owner_of(image, f"{image}.tmp.notapid")…')
        if PartialCopy.owner_of(image, f"{image}.backup") is not None:
            raise AssertionError('PartialCopy.owner_of(image, f"{image}.backup") is N…')
        if PartialCopy.owner_of(image, PartialCopy.render(image, 42)) != 42:
            raise AssertionError("PartialCopy.owner_of(image, PartialCopy.render(imag…")

    def test_lock_held_blocks_second_owner(
        self, tmp_path: Path, template: Path
    ) -> None:
        image = tmp_path / "img"
        store = self._store(template)
        store.acquire(str(image))
        fd = os.open(str(image) + ImageStore.LOCK_SUFFIX, os.O_WRONLY)
        try:
            with pytest.raises(BlockingIOError):
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            store.release_all()
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(fd)

    def test_concurrent_acquire_serialized(
        self, tmp_path: Path, template: Path
    ) -> None:
        image = tmp_path / "img"
        first = self._store(template)
        first.acquire(str(image))
        second = self._store(template)
        done = threading.Event()

        def worker() -> None:
            second.acquire(str(image))
            done.set()

        thread = threading.Thread(target=worker)
        thread.start()
        try:
            if done.wait(0.3):
                raise AssertionError("not done.wait(0.3)")
            first.release_all()
            if not (done.wait(3)):
                raise AssertionError("done.wait(3)")
        finally:
            thread.join()
            second.release_all()


class TestFileOperations:
    @staticmethod
    def _ops(tmp_path: Path) -> FileOperations:
        return FileOperations(str(tmp_path), CHUNK)

    @staticmethod
    def _split(out: io.BytesIO) -> tuple[int, bytes]:
        header, _, body = out.getvalue().partition(b"\n")
        return ReadHeader.parse(header).size, body

    def test_write_creates_dirs_and_content(self, tmp_path: Path) -> None:
        rc = self._ops(tmp_path).write("a/b/c.txt", io.BytesIO(b"data"))
        if rc != 0:
            raise AssertionError("rc == 0")
        if (tmp_path / "a" / "b" / "c.txt").read_bytes() != b"data":
            raise AssertionError('(tmp_path / "a" / "b" / "c.txt").read_bytes() == b"…')

    def test_write_overwrites(self, tmp_path: Path) -> None:
        ops = self._ops(tmp_path)
        ops.write("f.txt", io.BytesIO(b"old"))
        ops.write("f.txt", io.BytesIO(b"new"))
        if (tmp_path / "f.txt").read_bytes() != b"new":
            raise AssertionError('(tmp_path / "f.txt").read_bytes() == b"new"')

    def test_read_returns_header_and_content(self, tmp_path: Path) -> None:
        (tmp_path / "f.txt").write_bytes(b"payload")
        out = io.BytesIO()

        rc = self._ops(tmp_path).read("f.txt", ReadWindow.entire(), out)

        if rc != 0:
            raise AssertionError("rc == 0")
        if self._split(out) != (7, b"payload"):
            raise AssertionError('self._split(out) == (7, b"payload")')

    def test_read_window_slices_body(self, tmp_path: Path) -> None:
        (tmp_path / "f.txt").write_bytes(b"0123456789")
        out = io.BytesIO()

        window = ReadWindow(offset=3, length=4)
        rc = self._ops(tmp_path).read("f.txt", window, out)

        if rc != 0:
            raise AssertionError("rc == 0")
        if self._split(out) != (10, b"3456"):
            raise AssertionError('self._split(out) == (10, b"3456")')

    def test_read_window_past_end_is_empty(self, tmp_path: Path) -> None:
        (tmp_path / "f.txt").write_bytes(b"abc")
        out = io.BytesIO()

        window = ReadWindow(offset=10, length=5)
        rc = self._ops(tmp_path).read("f.txt", window, out)

        if rc != 0:
            raise AssertionError("rc == 0")
        if self._split(out) != (3, b""):
            raise AssertionError('self._split(out) == (3, b"")')

    def test_read_missing_is_not_found(self, tmp_path: Path) -> None:
        rc = self._ops(tmp_path).read("nope", ReadWindow.entire(), io.BytesIO())
        if rc != LauncherExit.NOT_FOUND:
            raise AssertionError("rc == LauncherExit.NOT_FOUND")

    def test_stat_reports_size_without_body(self, tmp_path: Path) -> None:
        (tmp_path / "f.txt").write_bytes(b"payload")
        out = io.BytesIO()

        rc = self._ops(tmp_path).stat("f.txt", out)

        if rc != 0:
            raise AssertionError("rc == 0")
        if self._split(out) != (7, b""):
            raise AssertionError('self._split(out) == (7, b"")')

    def test_stat_revision_changes_on_same_size_rewrite(self, tmp_path: Path) -> None:
        """Правка той же длины видна по версии: слежение канваса ловит её."""
        target = tmp_path / "f.txt"
        target.write_bytes(b"aaaa")

        first = io.BytesIO()
        self._ops(tmp_path).stat("f.txt", first)
        before = ReadHeader.parse(first.getvalue().partition(b"\n")[0])

        os.utime(target, ns=(before.revision + 10**9, before.revision + 10**9))
        target.write_bytes(b"bbbb")

        second = io.BytesIO()
        self._ops(tmp_path).stat("f.txt", second)
        after = ReadHeader.parse(second.getvalue().partition(b"\n")[0])

        if after.size != before.size:
            raise AssertionError("размер обязан совпасть в этом сценарии")
        if after.revision == before.revision:
            raise AssertionError("версия не изменилась при правке той же длины")

    def test_header_without_revision_reads_as_zero(self) -> None:
        """Заголовок лаунчера прошлой сборки читается без версии."""
        head = ReadHeader.parse(b"size=42")

        if head.size != 42:
            raise AssertionError("head.size == 42")
        if head.revision != 0:
            raise AssertionError("head.revision == 0")

    def test_stat_missing_is_not_found(self, tmp_path: Path) -> None:
        rc = self._ops(tmp_path).stat("nope", io.BytesIO())
        if rc != LauncherExit.NOT_FOUND:
            raise AssertionError("rc == LauncherExit.NOT_FOUND")

    def test_stat_rejects_directory(self, tmp_path: Path) -> None:
        (tmp_path / "sub").mkdir()
        rc = self._ops(tmp_path).stat("sub", io.BytesIO())
        if rc != LauncherExit.NOT_REGULAR:
            raise AssertionError("rc == LauncherExit.NOT_REGULAR")

    def test_read_rejects_directory(self, tmp_path: Path) -> None:
        (tmp_path / "sub").mkdir()
        rc = self._ops(tmp_path).read("sub", ReadWindow.entire(), io.BytesIO())
        if rc != LauncherExit.NOT_REGULAR:
            raise AssertionError("rc == LauncherExit.NOT_REGULAR")

    def test_write_rejects_existing_directory(self, tmp_path: Path) -> None:
        (tmp_path / "sub").mkdir()
        rc = self._ops(tmp_path).write("sub", io.BytesIO(b"x"))
        if rc != LauncherExit.NOT_REGULAR:
            raise AssertionError("rc == LauncherExit.NOT_REGULAR")

    def test_read_does_not_block_on_fifo(self, tmp_path: Path) -> None:
        """stat перед open: именованный канал без писателя не подвешивает read."""
        fifo = tmp_path / "pipe"
        os.mkfifo(fifo)

        rc = self._ops(tmp_path).read("pipe", ReadWindow.entire(), io.BytesIO())

        if rc != LauncherExit.NOT_REGULAR:
            raise AssertionError("rc == LauncherExit.NOT_REGULAR")

    def test_stat_does_not_block_on_fifo(self, tmp_path: Path) -> None:
        fifo = tmp_path / "pipe"
        os.mkfifo(fifo)

        rc = self._ops(tmp_path).stat("pipe", io.BytesIO())

        if rc != LauncherExit.NOT_REGULAR:
            raise AssertionError("rc == LauncherExit.NOT_REGULAR")

    def test_delete_then_not_found(self, tmp_path: Path) -> None:
        (tmp_path / "f.txt").write_bytes(b"x")
        ops = self._ops(tmp_path)
        if ops.delete("f.txt") != 0:
            raise AssertionError('ops.delete("f.txt") == 0')
        if (tmp_path / "f.txt").exists():
            raise AssertionError('not (tmp_path / "f.txt").exists()')
        if ops.delete("f.txt") != LauncherExit.NOT_FOUND:
            raise AssertionError('ops.delete("f.txt") == LauncherExit.NOT_FOUND')

    @pytest.mark.parametrize("rel", ["/abs", "../x", "a/../../x"])
    def test_escape_rejected(self, tmp_path: Path, rel: str) -> None:
        with pytest.raises(MountError, match="invalid relative path"):
            self._ops(tmp_path).delete(rel)

    def test_path_normalized_inside_root(self, tmp_path: Path) -> None:
        self._ops(tmp_path).write("a/./b/../c.txt", io.BytesIO(b"x"))
        if not ((tmp_path / "a" / "c.txt").exists()):
            raise AssertionError('(tmp_path / "a" / "c.txt").exists()')


class TestSymlinkEscape:
    """Симлинк внутри образа не должен выводить чтение за его пределы.

    Содержимое образа пишет bash-тул, поэтому ссылка на файл хоста — то, что
    туда реально может попасть.
    """

    @staticmethod
    def _ops(tmp_path: Path) -> FileOperations:
        root = tmp_path / "root"
        root.mkdir()
        return FileOperations(str(root), CHUNK)

    @pytest.fixture
    def outside(self, tmp_path: Path) -> Path:
        secret = tmp_path / "outside.txt"
        secret.write_bytes(b"host secret")
        return secret

    def test_read_refuses_symlinked_file(self, tmp_path: Path, outside: Path) -> None:
        ops = self._ops(tmp_path)
        (tmp_path / "root" / "leak").symlink_to(outside)

        out = io.BytesIO()
        rc = ops.read("leak", ReadWindow.entire(), out)

        if rc != LauncherExit.NOT_REGULAR:
            raise AssertionError("rc == LauncherExit.NOT_REGULAR")
        if b"host secret" in out.getvalue():
            raise AssertionError('b"host secret" not in out.getvalue()')

    def test_stat_refuses_symlinked_file(self, tmp_path: Path, outside: Path) -> None:
        ops = self._ops(tmp_path)
        (tmp_path / "root" / "leak").symlink_to(outside)

        rc = ops.stat("leak", io.BytesIO())

        if rc != LauncherExit.NOT_REGULAR:
            raise AssertionError("rc == LauncherExit.NOT_REGULAR")

    def test_read_refuses_symlinked_parent(self, tmp_path: Path, outside: Path) -> None:
        """Промежуточный каталог тоже проверяется, а не только имя файла."""
        ops = self._ops(tmp_path)
        (tmp_path / "root" / "upload").symlink_to(outside.parent)

        out = io.BytesIO()
        rc = ops.read("upload/outside.txt", ReadWindow.entire(), out)

        if rc != LauncherExit.NOT_REGULAR:
            raise AssertionError("rc == LauncherExit.NOT_REGULAR")
        if b"host secret" in out.getvalue():
            raise AssertionError('b"host secret" not in out.getvalue()')

    def test_write_refuses_symlinked_target(
        self, tmp_path: Path, outside: Path
    ) -> None:
        """Иначе запись во вложение перезаписала бы файл хоста."""
        ops = self._ops(tmp_path)
        (tmp_path / "root" / "leak").symlink_to(outside)

        rc = ops.write("leak", io.BytesIO(b"overwritten"))

        if rc != LauncherExit.NOT_REGULAR:
            raise AssertionError("rc == LauncherExit.NOT_REGULAR")
        if outside.read_bytes() != b"host secret":
            raise AssertionError('outside.read_bytes() == b"host secret"')

    def test_delete_removes_the_link_not_the_target(
        self, tmp_path: Path, outside: Path
    ) -> None:
        ops = self._ops(tmp_path)
        link = tmp_path / "root" / "leak"
        link.symlink_to(outside)

        if ops.delete("leak") != LauncherExit.OK:
            raise AssertionError('ops.delete("leak") == LauncherExit.OK')
        if link.is_symlink():
            raise AssertionError("not link.is_symlink()")
        if outside.read_bytes() != b"host secret":
            raise AssertionError('outside.read_bytes() == b"host secret"')

    def test_listing_skips_symlinks(self, tmp_path: Path, outside: Path) -> None:
        ops = self._ops(tmp_path)
        (tmp_path / "root" / "real.txt").write_bytes(b"x")
        (tmp_path / "root" / "leak").symlink_to(outside)

        out = io.BytesIO()
        if ops.list_dir(".", out) != LauncherExit.OK:
            raise AssertionError('ops.list_dir(".", out) == LauncherExit.OK')

        if out.getvalue().split() != [b"real.txt"]:
            raise AssertionError('out.getvalue().split() == [b"real.txt"]')


class TestFuseMounter:
    @staticmethod
    def _sleeper() -> subprocess.Popen[bytes]:
        return subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            shell=False,
        )

    def test_is_mounted_true_for_root(self) -> None:
        if not (FuseMounter.is_mounted("/")):
            raise AssertionError('FuseMounter.is_mounted("/")')

    def test_is_mounted_false_for_plain_dir(self, tmp_path: Path) -> None:
        if FuseMounter.is_mounted(str(tmp_path)):
            raise AssertionError("not FuseMounter.is_mounted(str(tmp_path))")

    def test_missing_fuse2fs_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("boba.workspace.launcher.shutil.which", lambda _: None)
        mounter = FuseMounter(_launcher_options(), _trusted())
        with pytest.raises(MountError, match="fuse2fs"):
            mounter.mount(str(tmp_path / "img"), str(tmp_path / "mnt"), readonly=False)

    def test_dead_daemon_raises_with_exit_code(self, tmp_path: Path) -> None:
        daemon = subprocess.Popen(
            [sys.executable, "-c", "raise SystemExit(7)"],
            shell=False,
        )
        daemon.wait()
        mounter = FuseMounter(_launcher_options(mount_wait_sec=1.0), _trusted())
        with pytest.raises(MountError, match="code 7"):
            mounter._wait_mounted(str(tmp_path), daemon)

    def test_wait_timeout_raises(self, tmp_path: Path) -> None:
        daemon = self._sleeper()
        mounter = FuseMounter(
            _launcher_options(mount_wait_sec=0.2, mount_poll_sec=0.01), _trusted()
        )
        try:
            with pytest.raises(MountError, match="was not mounted"):
                mounter._wait_mounted(str(tmp_path), daemon)
        finally:
            daemon.kill()
            daemon.wait()

    def test_shutdown_terminates_daemon(self) -> None:
        daemon = self._sleeper()
        mounter = FuseMounter(_launcher_options(), _trusted())
        mounter._daemons.append(daemon)
        mounter.shutdown()
        if daemon.returncode != -signal.SIGTERM:
            raise AssertionError("daemon.returncode == -signal.SIGTERM")

    def test_shutdown_kills_stubborn_daemon(self) -> None:
        code = (
            "import signal, time\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "print('ready', flush=True)\n"
            "time.sleep(30)\n"
        )
        daemon = subprocess.Popen(  # noqa: S603
            [sys.executable, "-c", code], stdout=subprocess.PIPE, shell=False
        )
        if daemon.stdout is None:
            raise AssertionError("daemon.stdout is not None")
        daemon.stdout.readline()
        mounter = FuseMounter(_launcher_options(shutdown_wait_sec=0.2), _trusted())
        mounter._daemons.append(daemon)
        mounter.shutdown()
        if daemon.returncode != -signal.SIGKILL:
            raise AssertionError("daemon.returncode == -signal.SIGKILL")

    def test_shutdown_is_idempotent(self) -> None:
        daemon = self._sleeper()
        mounter = FuseMounter(_launcher_options(), _trusted())
        mounter._daemons.append(daemon)
        mounter.shutdown()
        mounter.shutdown()
        if daemon.returncode != -signal.SIGTERM:
            raise AssertionError("daemon.returncode == -signal.SIGTERM")


class TestCapabilityDropper:
    def test_drop_all_clears_caps(self) -> None:
        code = (
            "from boba.workspace.launcher import CapabilityDropper\n"
            "CapabilityDropper().drop_all()\n"
            "print(open('/proc/self/status').read())\n"
        )
        env = {**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)}
        out = subprocess.run(  # noqa: S603
            [sys.executable, "-c", code],
            env=env,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        caps: dict[str, str] = {}
        for line in out.splitlines():
            if line.startswith("Cap"):
                caps[line.split(":")[0]] = line.split()[1]
        if int(caps["CapEff"], 16) != 0:
            raise AssertionError('int(caps["CapEff"], 16) == 0')
        if int(caps["CapPrm"], 16) != 0:
            raise AssertionError('int(caps["CapPrm"], 16) == 0')


class TestLauncherMain:
    @staticmethod
    def _main(tmp_path: Path, template: Path, *op: str) -> int:
        return Launcher.main(
            [
                "--template",
                str(template),
                "--image",
                str(tmp_path / "img"),
                str(tmp_path / "mnt"),
                *_REQUIRED_FLAGS,
                *op,
            ]
        )

    def test_unknown_mode_rejected_by_argparse(
        self, tmp_path: Path, template: Path
    ) -> None:
        with pytest.raises(SystemExit):
            self._main(tmp_path, template, "chmod", "x")

    def test_extra_arguments_fail_before_any_lock(
        self, tmp_path: Path, template: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc = self._main(tmp_path, template, "run", "a", "b")
        if rc != LauncherExit.MOUNT_ERROR:
            raise AssertionError("rc == LauncherExit.MOUNT_ERROR")
        if LauncherMarker.ERROR not in capsys.readouterr().err:
            raise AssertionError("LauncherMarker.ERROR in capsys.readouterr().err")
        if (tmp_path / ("img" + ImageStore.LOCK_SUFFIX)).exists():
            raise AssertionError('not (tmp_path / ("img" + ImageStore.LOCK_SUFFIX)).e…')
        if (tmp_path / "img").exists():
            raise AssertionError('not (tmp_path / "img").exists()')

    def test_empty_run_command_rejected(
        self, tmp_path: Path, template: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc = self._main(tmp_path, template, "run", "   ")
        if rc != LauncherExit.MOUNT_ERROR:
            raise AssertionError("rc == LauncherExit.MOUNT_ERROR")
        if "empty command" not in capsys.readouterr().err:
            raise AssertionError('"empty command" in capsys.readouterr().err')

    def test_unbalanced_quotes_rejected(
        self, tmp_path: Path, template: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc = self._main(tmp_path, template, "run", "'unclosed")
        if rc != LauncherExit.MOUNT_ERROR:
            raise AssertionError("rc == LauncherExit.MOUNT_ERROR")
        if LauncherMarker.ERROR not in capsys.readouterr().err:
            raise AssertionError("LauncherMarker.ERROR in capsys.readouterr().err")

    def test_missing_template_is_mount_error(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc = self._main(tmp_path, tmp_path / "absent", "delete", "x")
        if rc != LauncherExit.MOUNT_ERROR:
            raise AssertionError("rc == LauncherExit.MOUNT_ERROR")
        if LauncherMarker.ERROR not in capsys.readouterr().err:
            raise AssertionError("LauncherMarker.ERROR in capsys.readouterr().err")

    def test_cli_options_parsed_into_launcher(self) -> None:
        args = Launcher._parse_args(
            [
                "--template",
                "/t",
                "--image",
                "/i",
                "/m",
                "--mount-wait-sec",
                "1.5",
                "--copy-chunk-bytes",
                "4096",
                "--mount-poll-sec",
                "0.05",
                "--shutdown-wait-sec",
                "5.0",
                "--lock-wait-sec",
                "2.5",
                "--max-memory-bytes",
                "1048576",
                "--max-cpu-sec",
                "7",
                "--max-file-size-bytes",
                "2048",
                "--max-open-files",
                "64",
                "--oom-score-adj",
                "800",
                "--trusted-bin-dir",
                "/usr/bin",
                "read",
                "x",
            ]
        )
        if args.trusted_bin_dir != ["/usr/bin"]:
            raise AssertionError('args.trusted_bin_dir == ["/usr/bin"]')
        if args.mount_wait_sec != 1.5:
            raise AssertionError("args.mount_wait_sec == 1.5")
        if args.lock_wait_sec != 2.5:
            raise AssertionError("args.lock_wait_sec == 2.5")
        if args.copy_chunk_bytes != 4096:
            raise AssertionError("args.copy_chunk_bytes == 4096")
        if args.max_memory_bytes != 1048576:
            raise AssertionError("args.max_memory_bytes == 1048576")
        if args.max_cpu_sec != 7:
            raise AssertionError("args.max_cpu_sec == 7")
        if args.max_file_size_bytes != 2048:
            raise AssertionError("args.max_file_size_bytes == 2048")
        if args.oom_score_adj != 800:
            raise AssertionError("args.oom_score_adj == 800")
        if args.mode != "read":
            raise AssertionError('args.mode == "read"')
        if args.args != ["x"]:
            raise AssertionError('args.args == ["x"]')

    def test_cli_requires_all_options(self) -> None:
        """Скрытых значений нет: без флагов лаунчер не запускается."""
        with pytest.raises(SystemExit):
            Launcher._parse_args(
                ["--template", "/t", "--image", "/i", "/m", "read", "x"]
            )


class TestChainOptions:
    @staticmethod
    def _argv(op: list[str], options: LauncherOptions) -> list[str]:
        return build_chain_argv(
            images=[("/ws/a.ext4", "/ws/a.ext4.mnt")],
            template="/t.ext4",
            op=op,
            python_bin="/usr/bin/python3",
            options=options,
            limits=ResourceLimits(),
            binaries=_trusted(),
        )

    def test_options_rendered_as_flags(self) -> None:
        options = LauncherOptions(
            mount_wait_sec=3.5,
            mount_poll_sec=0.1,
            shutdown_wait_sec=2.0,
            lock_wait_sec=4.5,
            copy_chunk_bytes=4096,
        )
        argv = self._argv(["read", "x"], options)
        if argv[argv.index("--mount-wait-sec") + 1] != "3.5":
            raise AssertionError('argv[argv.index("--mount-wait-sec") + 1] == "3.5"')
        if argv[argv.index("--mount-poll-sec") + 1] != "0.1":
            raise AssertionError('argv[argv.index("--mount-poll-sec") + 1] == "0.1"')
        if argv[argv.index("--shutdown-wait-sec") + 1] != "2.0":
            raise AssertionError('argv[argv.index("--shutdown-wait-sec") + 1] == "2.0"')
        if argv[argv.index("--lock-wait-sec") + 1] != "4.5":
            raise AssertionError('argv[argv.index("--lock-wait-sec") + 1] == "4.5"')
        if argv[argv.index("--copy-chunk-bytes") + 1] != "4096":
            raise AssertionError('argv[argv.index("--copy-chunk-bytes") + 1] == "4096"')

    def test_run_command_shlex_roundtrip(self) -> None:
        inner = ["/bin/echo", "a b", "it's", "--flag=v"]
        argv = self._argv(["run", shlex.join(inner)], _launcher_options())
        if shlex.split(argv[-1]) != inner:
            raise AssertionError("shlex.split(argv[-1]) == inner")

    def test_config_requires_all_timings(self) -> None:
        with pytest.raises(ValueError, match="mount_wait_sec"):
            MountingConfig.model_validate({})

    def test_config_maps_to_options(self) -> None:
        cfg = MountingConfig(
            mount_wait_sec=1.0,
            mount_poll_sec=0.1,
            shutdown_wait_sec=2.0,
            lock_wait_sec=3.0,
            copy_chunk_bytes=4096,
        )
        if not (
            cfg.to_options()
            == _launcher_options(
                mount_wait_sec=1.0,
                mount_poll_sec=0.1,
                shutdown_wait_sec=2.0,
                lock_wait_sec=3.0,
                copy_chunk_bytes=4096,
            )
        ):
            raise AssertionError("cfg.to_options() == _launcher_options( mount_wait_s…")

    def test_limits_rendered_as_flags(self) -> None:
        limits = ResourceLimits(
            max_memory_bytes=1048576,
            max_cpu_sec=7,
            max_file_size_bytes=2048,
        )
        argv = build_chain_argv(
            images=[("/ws/a.ext4", "/ws/a.ext4.mnt")],
            template="/t.ext4",
            op=["read", "x"],
            python_bin="/usr/bin/python3",
            options=_launcher_options(),
            limits=limits,
            binaries=_trusted(),
        )
        if argv[argv.index("--max-memory-bytes") + 1] != "1048576":
            raise AssertionError('argv[argv.index("--max-memory-bytes") + 1] == "1048…')
        if argv[argv.index("--max-cpu-sec") + 1] != "7":
            raise AssertionError('argv[argv.index("--max-cpu-sec") + 1] == "7"')
        if argv[argv.index("--max-file-size-bytes") + 1] != "2048":
            raise AssertionError('argv[argv.index("--max-file-size-bytes") + 1] == "2…')


class TestResourceLimits:
    def test_apply_to_process_sets_limits(self) -> None:
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            shell=False,
        )
        try:
            limits = ResourceLimits(
                max_memory_bytes=64 * 1024 * 1024,
                max_cpu_sec=5,
                max_file_size_bytes=8 * 1024 * 1024,
            )
            limits.apply_to_process(proc.pid)
            limits_text = Path(f"/proc/{proc.pid}/limits").read_text()
        finally:
            proc.kill()
            proc.wait()
        address_space = ""
        cpu_time = ""
        file_size = ""
        for line in limits_text.splitlines():
            if line.startswith("Max address space"):
                address_space = line
            if line.startswith("Max cpu time"):
                cpu_time = line
            if line.startswith("Max file size"):
                file_size = line
        if str(64 * 1024 * 1024) not in address_space:
            raise AssertionError("str(64 * 1024 * 1024) in address_space")
        if " 5 " not in f"{cpu_time} ":
            raise AssertionError('" 5 " in f"{cpu_time} "')
        if str(8 * 1024 * 1024) not in file_size:
            raise AssertionError("str(8 * 1024 * 1024) in file_size")

    def test_zero_limits_do_nothing(self) -> None:
        before = Path("/proc/self/limits").read_text()
        ResourceLimits().apply_to_process(os.getpid())
        if Path("/proc/self/limits").read_text() != before:
            raise AssertionError('Path("/proc/self/limits").read_text() == before')
