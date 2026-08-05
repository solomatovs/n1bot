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

import pytest

from boba.workspace.launcher import (
    EXIT_MOUNT_ERROR,
    EXIT_NOT_FOUND,
    LAUNCHER_ERROR_PREFIX,
    FileOperations,
    FuseMounter,
    ImageStore,
    Launcher,
    LauncherConfig,
    LauncherOptions,
    MountError,
    ResourceLimits,
    SparseCopier,
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
    "--mount-wait-sec", "10.0",
    "--mount-poll-sec", "0.05",
    "--shutdown-wait-sec", "5.0",
    "--copy-chunk-bytes", "1048576",
    "--max-memory-bytes", "0",
    "--max-cpu-sec", "0",
    "--max-file-size-bytes", "0",
    "--max-open-files", "0",
    "--oom-score-adj", "0",
)


def _launcher_options(**kw: float) -> LauncherOptions:
    """Тайминги задаются явно: дефолтов у LauncherOptions нет."""
    values: dict[str, float] = {
        "mount_wait_sec": 10.0,
        "mount_poll_sec": 0.05,
        "shutdown_wait_sec": 5.0,
        "copy_chunk_bytes": 1 << 20,
    }
    values.update(kw)
    return LauncherOptions(
        mount_wait_sec=values["mount_wait_sec"],
        mount_poll_sec=values["mount_poll_sec"],
        shutdown_wait_sec=values["shutdown_wait_sec"],
        copy_chunk_bytes=int(values["copy_chunk_bytes"]),
    )



class TestSparseCopier:
    @staticmethod
    def _copy(src: Path, dst: Path) -> None:
        SparseCopier(CHUNK).copy(str(src), str(dst))

    def test_content_preserved(self, tmp_path: Path) -> None:
        src, dst = tmp_path / "src", tmp_path / "dst"
        data = bytes(range(256)) * 300
        src.write_bytes(data)
        self._copy(src, dst)
        assert dst.read_bytes() == data

    def test_holes_stay_holes(self, tmp_path: Path) -> None:
        src, dst = tmp_path / "src", tmp_path / "dst"
        with src.open("wb") as f:
            f.write(b"head")
            f.seek(8 * 1024 * 1024)
            f.write(b"tail")
        self._copy(src, dst)
        assert dst.read_bytes() == src.read_bytes()
        assert dst.stat().st_blocks * 512 < dst.stat().st_size

    def test_zero_blocks_become_holes(self, tmp_path: Path) -> None:
        src, dst = tmp_path / "src", tmp_path / "dst"
        src.write_bytes(b"\0" * (4 * CHUNK))
        self._copy(src, dst)
        assert dst.read_bytes() == src.read_bytes()
        assert dst.stat().st_blocks * 512 < dst.stat().st_size

    def test_empty_file(self, tmp_path: Path) -> None:
        src, dst = tmp_path / "src", tmp_path / "dst"
        src.touch()
        self._copy(src, dst)
        assert dst.stat().st_size == 0


class TestImageStore:
    @staticmethod
    def _store(template: Path) -> ImageStore:
        return ImageStore(str(template), SparseCopier(CHUNK))

    def test_creates_image_from_template(
        self, tmp_path: Path, template: Path
    ) -> None:
        image = tmp_path / "img"
        store = self._store(template)
        try:
            store.acquire(str(image))
            assert image.read_bytes() == b"TEMPLATE"
        finally:
            store.release_all()

    def test_existing_image_untouched(self, tmp_path: Path, template: Path) -> None:
        image = tmp_path / "img"
        image.write_bytes(b"EXISTING")
        store = self._store(template)
        try:
            store.acquire(str(image))
            assert image.read_bytes() == b"EXISTING"
        finally:
            store.release_all()

    def test_lock_file_created(self, tmp_path: Path, template: Path) -> None:
        image = tmp_path / "img"
        store = self._store(template)
        try:
            store.acquire(str(image))
            assert (tmp_path / ("img" + ImageStore.LOCK_SUFFIX)).exists()
        finally:
            store.release_all()

    def test_missing_template_raises_and_cleans_tmp(self, tmp_path: Path) -> None:
        store = ImageStore(str(tmp_path / "absent"), SparseCopier(CHUNK))
        image = tmp_path / "img"
        try:
            with pytest.raises(MountError, match="not found"):
                store.acquire(str(image))
        finally:
            store.release_all()
        assert not image.exists()
        leftovers: list[Path] = []
        for path in tmp_path.iterdir():
            if ".tmp." in path.name:
                leftovers.append(path)
        assert not leftovers

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
            assert not done.wait(0.3)
            first.release_all()
            assert done.wait(3)
        finally:
            thread.join()
            second.release_all()


class TestFileOperations:
    def test_write_creates_dirs_and_content(self, tmp_path: Path) -> None:
        rc = FileOperations(str(tmp_path)).write("a/b/c.txt", io.BytesIO(b"data"))
        assert rc == 0
        assert (tmp_path / "a" / "b" / "c.txt").read_bytes() == b"data"

    def test_write_overwrites(self, tmp_path: Path) -> None:
        ops = FileOperations(str(tmp_path))
        ops.write("f.txt", io.BytesIO(b"old"))
        ops.write("f.txt", io.BytesIO(b"new"))
        assert (tmp_path / "f.txt").read_bytes() == b"new"

    def test_read_returns_content(self, tmp_path: Path) -> None:
        (tmp_path / "f.txt").write_bytes(b"payload")
        out = io.BytesIO()
        assert FileOperations(str(tmp_path)).read("f.txt", out) == 0
        assert out.getvalue() == b"payload"

    def test_read_missing_is_not_found(self, tmp_path: Path) -> None:
        assert FileOperations(str(tmp_path)).read("nope", io.BytesIO()) == (
            EXIT_NOT_FOUND
        )

    def test_delete_then_not_found(self, tmp_path: Path) -> None:
        (tmp_path / "f.txt").write_bytes(b"x")
        ops = FileOperations(str(tmp_path))
        assert ops.delete("f.txt") == 0
        assert not (tmp_path / "f.txt").exists()
        assert ops.delete("f.txt") == EXIT_NOT_FOUND

    @pytest.mark.parametrize("rel", ["/abs", "../x", "a/../../x"])
    def test_escape_rejected(self, tmp_path: Path, rel: str) -> None:
        with pytest.raises(MountError, match="invalid relative path"):
            FileOperations(str(tmp_path)).delete(rel)

    def test_path_normalized_inside_root(self, tmp_path: Path) -> None:
        FileOperations(str(tmp_path)).write("a/./b/../c.txt", io.BytesIO(b"x"))
        assert (tmp_path / "a" / "c.txt").exists()


class TestFuseMounter:
    @staticmethod
    def _sleeper() -> subprocess.Popen[bytes]:
        return subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            shell=False,
        )

    def test_is_mounted_true_for_root(self) -> None:
        assert FuseMounter.is_mounted("/")

    def test_is_mounted_false_for_plain_dir(self, tmp_path: Path) -> None:
        assert not FuseMounter.is_mounted(str(tmp_path))

    def test_missing_fuse2fs_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "boba.workspace.launcher.shutil.which", lambda _: None
        )
        mounter = FuseMounter(_launcher_options())
        with pytest.raises(MountError, match="fuse2fs"):
            mounter.mount(str(tmp_path / "img"), str(tmp_path / "mnt"))

    def test_dead_daemon_raises_with_exit_code(self, tmp_path: Path) -> None:
        daemon = subprocess.Popen(
            [sys.executable, "-c", "raise SystemExit(7)"],
            shell=False,
        )
        daemon.wait()
        mounter = FuseMounter(_launcher_options(mount_wait_sec=1.0))
        with pytest.raises(MountError, match="code 7"):
            mounter._wait_mounted(str(tmp_path), daemon)

    def test_wait_timeout_raises(self, tmp_path: Path) -> None:
        daemon = self._sleeper()
        mounter = FuseMounter(
            _launcher_options(mount_wait_sec=0.2, mount_poll_sec=0.01)
        )
        try:
            with pytest.raises(MountError, match="was not mounted"):
                mounter._wait_mounted(str(tmp_path), daemon)
        finally:
            daemon.kill()
            daemon.wait()

    def test_shutdown_terminates_daemon(self) -> None:
        daemon = self._sleeper()
        mounter = FuseMounter(_launcher_options())
        mounter._daemons.append(daemon)
        mounter.shutdown()
        assert daemon.returncode == -signal.SIGTERM

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
        assert daemon.stdout is not None
        daemon.stdout.readline()
        mounter = FuseMounter(_launcher_options(shutdown_wait_sec=0.2))
        mounter._daemons.append(daemon)
        mounter.shutdown()
        assert daemon.returncode == -signal.SIGKILL

    def test_shutdown_is_idempotent(self) -> None:
        daemon = self._sleeper()
        mounter = FuseMounter(_launcher_options())
        mounter._daemons.append(daemon)
        mounter.shutdown()
        mounter.shutdown()
        assert daemon.returncode == -signal.SIGTERM


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
        assert int(caps["CapEff"], 16) == 0
        assert int(caps["CapPrm"], 16) == 0


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
        assert rc == EXIT_MOUNT_ERROR
        assert LAUNCHER_ERROR_PREFIX in capsys.readouterr().err
        assert not (tmp_path / ("img" + ImageStore.LOCK_SUFFIX)).exists()
        assert not (tmp_path / "img").exists()

    def test_empty_run_command_rejected(
        self, tmp_path: Path, template: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc = self._main(tmp_path, template, "run", "   ")
        assert rc == EXIT_MOUNT_ERROR
        assert "empty command" in capsys.readouterr().err

    def test_unbalanced_quotes_rejected(
        self, tmp_path: Path, template: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc = self._main(tmp_path, template, "run", "'unclosed")
        assert rc == EXIT_MOUNT_ERROR
        assert LAUNCHER_ERROR_PREFIX in capsys.readouterr().err

    def test_missing_template_is_mount_error(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc = self._main(tmp_path, tmp_path / "absent", "delete", "x")
        assert rc == EXIT_MOUNT_ERROR
        assert LAUNCHER_ERROR_PREFIX in capsys.readouterr().err

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
                "read",
                "x",
            ]
        )
        assert args.mount_wait_sec == 1.5
        assert args.copy_chunk_bytes == 4096
        assert args.max_memory_bytes == 1048576
        assert args.max_cpu_sec == 7
        assert args.max_file_size_bytes == 2048
        assert args.oom_score_adj == 800
        assert args.mode == "read"
        assert args.args == ["x"]

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
        )

    def test_options_rendered_as_flags(self) -> None:
        options = LauncherOptions(
            mount_wait_sec=3.5,
            mount_poll_sec=0.1,
            shutdown_wait_sec=2.0,
            copy_chunk_bytes=4096,
        )
        argv = self._argv(["read", "x"], options)
        assert argv[argv.index("--mount-wait-sec") + 1] == "3.5"
        assert argv[argv.index("--mount-poll-sec") + 1] == "0.1"
        assert argv[argv.index("--shutdown-wait-sec") + 1] == "2.0"
        assert argv[argv.index("--copy-chunk-bytes") + 1] == "4096"

    def test_run_command_shlex_roundtrip(self) -> None:
        inner = ["/bin/echo", "a b", "it's", "--flag=v"]
        argv = self._argv(["run", shlex.join(inner)], _launcher_options())
        assert shlex.split(argv[-1]) == inner

    def test_config_requires_all_timings(self) -> None:
        with pytest.raises(ValueError, match="mount_wait_sec"):
            LauncherConfig.model_validate({})

    def test_config_maps_to_options(self) -> None:
        cfg = LauncherConfig(
            mount_wait_sec=1.0,
            mount_poll_sec=0.1,
            shutdown_wait_sec=2.0,
            copy_chunk_bytes=4096,
        )
        assert cfg.to_options() == _launcher_options(
            mount_wait_sec=1.0,
            mount_poll_sec=0.1,
            shutdown_wait_sec=2.0,
            copy_chunk_bytes=4096,
        )

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
        )
        assert argv[argv.index("--max-memory-bytes") + 1] == "1048576"
        assert argv[argv.index("--max-cpu-sec") + 1] == "7"
        assert argv[argv.index("--max-file-size-bytes") + 1] == "2048"


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
        assert str(64 * 1024 * 1024) in address_space
        assert " 5 " in f"{cpu_time} "
        assert str(8 * 1024 * 1024) in file_size

    def test_zero_limits_do_nothing(self) -> None:
        before = Path("/proc/self/limits").read_text()
        ResourceLimits().apply_to_process(os.getpid())
        assert Path("/proc/self/limits").read_text() == before
