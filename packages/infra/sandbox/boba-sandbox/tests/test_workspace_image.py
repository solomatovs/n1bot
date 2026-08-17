"""Workspace-образ: общий контейнер треда для песочницы и storage."""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
from collections.abc import AsyncIterator, Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from boba.chainlit.data.storage import (
    ImageStorageClient,
    LocalStorageClient,
    StorageClient,
    StorageError,
    StorageFactory,
    StorageNotFoundError,
)
from boba.chainlit.infra.config import LocalStorageConfig
from boba.sandbox.caller import SandboxCaller
from boba.sandbox.profile import SandboxProfile
from boba.sandbox.runner import SandboxMountError
from boba.tool.shell.tools import BashToolConfig, build_bash_tool
from boba.toolkit.binaries import TrustedBinaries
from boba.toolkit.result import ShellResult
from boba.workspace.launcher import (
    FUSE_DEVICE,
    LauncherOptions,
    ReadWindow,
    ResourceLimits,
    build_chain_argv,
    render_image_path,
)

HOST_RO_BINDS = ("/usr", "/bin", "/sbin", "/lib", "/lib64", "/etc/alternatives")

OUTPUT_LIMITS = BashToolConfig(max_output_bytes=4 * 1024 * 1024)


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


async def read_all(storage: StorageClient, object_key: str) -> bytes:
    """Файл целиком: накопление в памяти — забота вызывающего, а не слоя."""
    async with await storage.open_stream(object_key, ReadWindow.entire()) as body:
        collected = bytearray()
        async for chunk in body.chunks:
            collected.extend(chunk)

    return bytes(collected)


def _storage_cfg(**kw: Any) -> LocalStorageConfig:
    """Тайминги лаунчера обязательны: дефолтов у конфига нет."""
    fields: dict[str, Any] = {
        "launcher": {
            "mount_wait_sec": 10.0,
            "mount_poll_sec": 0.05,
            "shutdown_wait_sec": 5.0,
            "lock_wait_sec": 10.0,
            "copy_chunk_bytes": 1 << 20,
        },
        "binaries": {"dirs": _bin_dirs()},
    }
    fields.update(kw)
    return LocalStorageConfig.model_validate(fields)


needs_fuse = pytest.mark.skipif(
    shutil.which("bwrap") is None
    or shutil.which("fuse2fs") is None
    or shutil.which("mkfs.ext4") is None
    or not os.path.exists(FUSE_DEVICE),
    reason="нужны bwrap, fuse2fs, mkfs.ext4 и /dev/fuse",
)


@pytest.fixture(autouse=True)
def chainlit_context() -> None:
    pass


@pytest.fixture
def template(tmp_path: Path) -> Path:
    """Шаблонный ext4-образ; без журнала — fuse2fs пишет только так."""
    path = tmp_path / "template.ext4"
    with path.open("wb") as f:
        f.truncate(16 * 1024 * 1024)
    mkfs = shutil.which("mkfs.ext4")
    if mkfs is None:
        raise AssertionError("mkfs is not None")
    subprocess.run(  # noqa: S603
        [mkfs, "-F", "-q", "-O", "^has_journal", "-m", "0", str(path)],
        check=True,
    )
    return path


def _image_tpl(tmp_path: Path) -> str:
    """Образ общий на пользователя: {thread_id} в пути отсутствует."""
    return f"{tmp_path}/ws/{{user_id}}.ext4"


_PROFILE_BASE: dict[str, object] = {
    "rootfs": "",
    "ro_binds": (),
    "rw_binds": (),
    "rw_images": (),
    "image_template": "",
    "launcher": {
        "mount_wait_sec": 10.0,
        "mount_poll_sec": 0.05,
        "shutdown_wait_sec": 5.0,
        "lock_wait_sec": 10.0,
        "copy_chunk_bytes": 1 << 20,
    },
    "binaries": {"dirs": _bin_dirs()},
    "tmpfs": (),
    "network": False,
    "env_set": {},
    "timeout_sec": 30,
    "max_memory_bytes": 512 * 1024 * 1024,
    "max_cpu_sec": 30,
    "max_file_size_bytes": 64 * 1024 * 1024,
    "max_open_files": 256,
    "max_processes": 256,
    "cgroup_base": "",
    "oom_score_adj": 0,
    "cwd": "",
}


def _profile(**kw: object) -> SandboxProfile:
    return SandboxProfile.model_validate({**_PROFILE_BASE, **kw})


def _bash(tmp_path: Path, template: Path, thread_id: str = "t1", **profile_kw):
    profile_dto = _profile(
        ro_binds=HOST_RO_BINDS,
        rw_images=(f"{_image_tpl(tmp_path)}:/workspace",),
        image_template=str(template),
        cwd="/workspace",
        **profile_kw,
    )

    def launchers(tool: str):
        return SandboxCaller(
            tool, profile_dto, lambda: {"user_id": "7", "thread_id": thread_id}
        )

    return build_bash_tool(OUTPUT_LIMITS, launchers)


def _invoke(tool, command: str, stdin: str = "") -> ShellResult:
    msg = tool.invoke(
        {
            "args": {"command": command, "stdin": stdin},
            "id": "call-bash",
            "name": "bash",
            "type": "tool_call",
        }
    )
    if not isinstance(msg.artifact, ShellResult):
        raise AssertionError("isinstance(msg.artifact, ShellResult)")

    return msg.artifact


def _storage(
    tmp_path: Path, template: Path, op_timeout_sec: int = 60
) -> ImageStorageClient:
    cfg = _storage_cfg(
        kind="image",
        image_path=_image_tpl(tmp_path),
        image_template=str(template),
        op_timeout_sec=op_timeout_sec,
    )
    client = StorageFactory.create(cfg)
    if not (isinstance(client, ImageStorageClient)):
        raise AssertionError("isinstance(client, ImageStorageClient)")
    return client


def _launcher_options() -> LauncherOptions:
    """Тайминги задаются явно: дефолтов у LauncherOptions нет."""
    return LauncherOptions(
        mount_wait_sec=10.0,
        mount_poll_sec=0.05,
        shutdown_wait_sec=5.0,
        lock_wait_sec=10.0,
        copy_chunk_bytes=1 << 20,
    )


class TestConfig:
    def test_image_kind_requires_paths(self) -> None:
        with pytest.raises(ValueError, match="image_path and image_template"):
            _storage_cfg(kind="image")

    def test_local_kind_requires_files_dir(self) -> None:
        with pytest.raises(ValueError, match="requires files_dir"):
            _storage_cfg(kind="local")

    def test_unknown_kind_rejected(self) -> None:
        with pytest.raises(ValueError, match="'local' or 'image'"):
            _storage_cfg(kind="s3", files_dir="/srv")

    def test_factory_picks_image_client(self) -> None:
        cfg = _storage_cfg(
            kind="image",
            image_path="/ws/{user_id}.ext4",
            image_template="/t.ext4",
        )
        if not (isinstance(StorageFactory.create(cfg), ImageStorageClient)):
            raise AssertionError("isinstance(StorageFactory.create(cfg), ImageStorage…")

    def test_factory_picks_local_client(self) -> None:
        cfg = _storage_cfg(files_dir="/srv/files")
        if type(StorageFactory.create(cfg)) is not LocalStorageClient:
            raise AssertionError("type(StorageFactory.create(cfg)) is LocalStorageCli…")

    def test_profile_images_require_template(self) -> None:
        with pytest.raises(ValueError, match="image_template is empty"):
            _profile(rw_images=("/ws/a.ext4:/workspace",))


class TestObjectKey:
    @staticmethod
    def _client() -> ImageStorageClient:
        cfg = _storage_cfg(
            kind="image",
            image_path="/ws/{user_id}.ext4",
            image_template="/t.ext4",
        )
        client = StorageFactory.create(cfg)
        if not (isinstance(client, ImageStorageClient)):
            raise AssertionError("isinstance(client, ImageStorageClient)")
        return client

    def test_key_splits_into_image_and_rel(self) -> None:
        """Образ на пользователя, thread_id остаётся частью пути внутри."""
        if not (
            self._client()._image_and_rel("7/t1/upload/report.pdf")
            == (
                "/ws/7.ext4",
                "t1/upload/report.pdf",
            )
        ):
            raise AssertionError('self._client()._image_and_rel("7/t1/upload/report.p…')

    def test_short_key_rejected(self) -> None:
        with pytest.raises(ValueError, match="invalid object_key"):
            self._client()._image_and_rel("7/t1")

    def test_traversal_rejected(self) -> None:
        with pytest.raises(ValueError, match="invalid object_key"):
            self._client()._image_and_rel("7/../etc/passwd")

    def test_render_image_path_needs_variables(self) -> None:
        with pytest.raises(RuntimeError, match="is not defined"):
            render_image_path("/ws/{user_id}.ext4", {})


class TestRelativePaths:
    """bwrap на read-only корне не создаст точку монтирования по относительному пути."""

    def test_config_makes_image_paths_absolute(self) -> None:
        cfg = _storage_cfg(
            kind="image",
            image_path="./data/ws/{user_id}/{thread_id}.ext4",
            image_template="./data/tpl.ext4",
        )
        if not (cfg.image_path.startswith("/")):
            raise AssertionError('cfg.image_path.startswith("/")')
        if not (cfg.image_template.startswith("/")):
            raise AssertionError('cfg.image_template.startswith("/")')

    def test_relative_image_bound_by_absolute_path(self) -> None:
        argv = build_chain_argv(
            extra_env={},
            images=[("./ws/a.ext4", "./ws/a.ext4.mnt")],
            template="./t.ext4",
            op=["write", "upload/x"],
            python_bin="/usr/bin/python3",
            options=_launcher_options(),
            limits=ResourceLimits(),
            binaries=_trusted(),
            rw_paths=["./shared"],
        )
        binds = []
        for i, arg in enumerate(argv):
            if arg == "--bind":
                binds.append(argv[i + 1])
        if not (binds):
            raise AssertionError("binds")
        for path in binds:
            if not (os.path.isabs(path)):
                raise AssertionError(path)
        if not (argv[argv.index("--image") + 1].startswith("/")):
            raise AssertionError('argv[argv.index("--image") + 1].startswith("/")')


class TestChainArgv:
    @staticmethod
    def _argv(**kw) -> list[str]:
        return build_chain_argv(
            extra_env={},
            images=[("/ws/a.ext4", "/ws/a.ext4.mnt")],
            template="/t.ext4",
            op=["write", "upload/x"],
            python_bin="/usr/bin/python3",
            options=_launcher_options(),
            limits=ResourceLimits(),
            binaries=_trusted(),
            **kw,
        )

    def test_limits_rendered_as_flags(self) -> None:
        limits = ResourceLimits(max_memory_bytes=64 * 1024 * 1024, max_cpu_sec=5)
        argv = build_chain_argv(
            extra_env={},
            images=[("/ws/a.ext4", "/ws/a.ext4.mnt")],
            template="/t.ext4",
            op=["write", "upload/x"],
            python_bin="/usr/bin/python3",
            options=_launcher_options(),
            limits=limits,
            binaries=_trusted(),
        )
        memory = argv[argv.index("--max-memory-bytes") + 1]
        cpu = argv[argv.index("--max-cpu-sec") + 1]
        if memory != str(64 * 1024 * 1024):
            raise AssertionError("memory == str(64 * 1024 * 1024)")
        if cpu != "5":
            raise AssertionError('cpu == "5"')

    def test_outer_bwrap_is_root_in_userns(self) -> None:
        argv = self._argv()
        if not (argv[0].endswith("bwrap")):
            raise AssertionError('argv[0].endswith("bwrap")')
        if not (os.path.isabs(argv[0]) or shutil.which("bwrap") is None):
            raise AssertionError('os.path.isabs(argv[0]) or shutil.which("bwrap") is …')
        if "--unshare-user" not in argv:
            raise AssertionError('"--unshare-user" in argv')
        if argv[argv.index("--uid") + 1] != "0":
            raise AssertionError('argv[argv.index("--uid") + 1] == "0"')

    def test_namespaces_isolated_with_neutral_hostname(self) -> None:
        argv = self._argv()
        if "--unshare-pid" not in argv:
            raise AssertionError('"--unshare-pid" in argv')
        if "--unshare-ipc" not in argv:
            raise AssertionError('"--unshare-ipc" in argv')
        if "--new-session" not in argv:
            raise AssertionError('"--new-session" in argv')
        if argv[argv.index("--hostname") + 1] != "sandbox":
            raise AssertionError('argv[argv.index("--hostname") + 1] == "sandbox"')

    def test_host_fs_readonly_except_image_dir(self) -> None:
        argv = self._argv()
        i = argv.index("--ro-bind")
        if argv[i + 1 : i + 3] != ["/", "/"]:
            raise AssertionError('argv[i + 1 : i + 3] == ["/", "/"]')
        b = argv.index("--bind")
        if argv[b + 1 : b + 3] != ["/ws", "/ws"]:
            raise AssertionError('argv[b + 1 : b + 3] == ["/ws", "/ws"]')

    def test_rw_paths_bound_writable(self) -> None:
        argv = self._argv(rw_paths=("/srv/data",))
        if "/srv/data" not in argv:
            raise AssertionError('"/srv/data" in argv')

    def test_only_fuse_device_exposed(self) -> None:
        argv = self._argv()
        if argv[argv.index("--dev") + 1] != "/dev":
            raise AssertionError('argv[argv.index("--dev") + 1] == "/dev"')
        if argv[argv.index("--dev-bind") + 1] != "/dev/fuse":
            raise AssertionError('argv[argv.index("--dev-bind") + 1] == "/dev/fuse"')

    def test_env_cleared_to_path_only(self) -> None:
        argv = self._argv()
        if "--clearenv" not in argv:
            raise AssertionError('"--clearenv" in argv')
        if argv[argv.index("--setenv") + 1] != "PATH":
            raise AssertionError('argv[argv.index("--setenv") + 1] == "PATH"')

    def test_sys_admin_capability_added(self) -> None:
        argv = self._argv()
        if argv[argv.index("--cap-add") + 1] != "CAP_SYS_ADMIN":
            raise AssertionError('argv[argv.index("--cap-add") + 1] == "CAP_SYS_ADMIN"')

    def test_network_isolated_by_default(self) -> None:
        if "--unshare-net" not in self._argv():
            raise AssertionError('"--unshare-net" in self._argv()')

    def test_network_enabled_on_demand(self) -> None:
        if "--unshare-net" in self._argv(network=True):
            raise AssertionError('"--unshare-net" not in self._argv(network=True)')

    def test_launcher_gets_image_and_op(self) -> None:
        argv = self._argv()
        i = argv.index("--image")
        if argv[i + 1 : i + 3] != ["/ws/a.ext4", "/ws/a.ext4.mnt"]:
            raise AssertionError('argv[i + 1 : i + 3] == ["/ws/a.ext4", "/ws/a.ext4.m…')
        if argv[-2:] != ["write", "upload/x"]:
            raise AssertionError('argv[-2:] == ["write", "upload/x"]')


class TestErrorBoundary:
    """Контракт слоя: наружу выходит только StorageError и его подклассы."""

    @staticmethod
    def _local(tmp_path: Path) -> LocalStorageClient:
        cfg = _storage_cfg(files_dir=str(tmp_path / "files"))
        client = StorageFactory.create(cfg)
        if not (isinstance(client, LocalStorageClient)):
            raise AssertionError("isinstance(client, LocalStorageClient)")
        return client

    @staticmethod
    def _guarded_operations() -> Iterator[str]:
        """Публичная операция фасада — та, у которой есть парный защищённый метод."""
        for name in vars(StorageClient):
            if name.startswith("_"):
                continue

            if f"_{name}" not in vars(StorageClient):
                continue

            yield name

    def test_missing_object_reads_as_not_found(self, tmp_path: Path) -> None:
        storage = self._local(tmp_path)

        with pytest.raises(StorageNotFoundError):
            asyncio.run(read_all(storage, "7/t1/upload/missing"))

    def test_missing_object_stats_as_not_found(self, tmp_path: Path) -> None:
        storage = self._local(tmp_path)

        with pytest.raises(StorageNotFoundError):
            asyncio.run(storage.stat("7/t1/upload/missing"))

    def test_missing_object_opens_as_not_found(self, tmp_path: Path) -> None:
        storage = self._local(tmp_path)

        async def probe() -> None:
            await storage.open_stream("7/t1/upload/missing", ReadWindow.entire())

        with pytest.raises(StorageNotFoundError):
            asyncio.run(probe())

    def test_key_outside_files_dir_is_storage_error(self, tmp_path: Path) -> None:
        storage = self._local(tmp_path)

        with pytest.raises(StorageError, match="outside files_dir"):
            asyncio.run(read_all(storage, "../../etc/passwd"))

    def test_unusable_files_dir_is_storage_error(self, tmp_path: Path) -> None:
        """Файл на месте каталога: системная ошибка не должна утечь наружу."""
        blocker = tmp_path / "files"
        blocker.write_bytes(b"not a directory")
        storage = self._local(tmp_path)

        with pytest.raises(StorageError) as failure:
            asyncio.run(storage.upload_file("7/t1/upload/a.txt", b"x"))

        if not (isinstance(failure.value.__cause__, OSError)):
            raise AssertionError("isinstance(failure.value.__cause__, OSError)")

    def test_directory_instead_of_object_is_storage_error(self, tmp_path: Path) -> None:
        storage = self._local(tmp_path)
        asyncio.run(storage.upload_file("7/t1/upload/a.txt", b"x"))

        with pytest.raises(StorageError) as failure:
            asyncio.run(read_all(storage, "7/t1/upload"))

        if isinstance(failure.value, StorageNotFoundError):
            raise AssertionError("not isinstance(failure.value, StorageNotFoundError)")

    def test_size_is_known_before_the_body(self, tmp_path: Path) -> None:
        """Потолок на объём ставит вызывающий: слой сообщает размер до тела."""
        storage = self._local(tmp_path)
        asyncio.run(storage.upload_file("7/t1/upload/big.bin", b"x" * 64))

        async def opened_size() -> int:
            async with await storage.open_stream(
                "7/t1/upload/big.bin", ReadWindow.entire()
            ) as body:
                return body.stat.size

        if asyncio.run(opened_size()) != 64:
            raise AssertionError("asyncio.run(opened_size()) == 64")

    def test_window_read_returns_slice(self, tmp_path: Path) -> None:
        storage = self._local(tmp_path)
        asyncio.run(storage.upload_file("7/t1/upload/win.bin", b"0123456789"))

        async def window() -> tuple[int, bytes]:
            opened = await storage.open_stream(
                "7/t1/upload/win.bin", ReadWindow(offset=2, length=3)
            )
            collected = bytearray()
            async for chunk in opened.chunks:
                collected.extend(chunk)
            return opened.stat.size, bytes(collected)

        if asyncio.run(window()) != (10, b"234"):
            raise AssertionError('asyncio.run(window()) == (10, b"234")')

    def test_operations_are_not_overridden_past_the_guard(self) -> None:
        operations = list(self._guarded_operations())
        if not (operations):
            raise AssertionError("operations")

        for client in (LocalStorageClient, ImageStorageClient):
            for name in operations:
                if name in vars(client):
                    raise AssertionError(f"{client.__name__}.{name} мимо guard")


@needs_fuse
class TestLiveImage:
    """Реальные монтирования: bash и storage поверх одного образа."""

    def test_write_persists_between_calls(self, tmp_path: Path, template: Path) -> None:
        tool = _bash(tmp_path, template)
        _invoke(tool, "echo hello > f.txt")
        if _invoke(tool, "cat f.txt").stdout.strip() != "hello":
            raise AssertionError('_invoke(tool, "cat f.txt").stdout.strip() == "he…')

    def test_image_created_from_template(self, tmp_path: Path, template: Path) -> None:
        _invoke(_bash(tmp_path, template), "true")
        if not ((tmp_path / "ws" / "7.ext4").is_file()):
            raise AssertionError('(tmp_path / "ws" / "7.ext4").is_file()')

    def test_no_mount_leaks_to_host(self, tmp_path: Path, template: Path) -> None:
        _invoke(_bash(tmp_path, template), "true")
        if "fuse2fs" in Path("/proc/mounts").read_text():
            raise AssertionError('"fuse2fs" not in Path("/proc/mounts").read_text()')

    def test_size_limit_is_enforced(self, tmp_path: Path, template: Path) -> None:
        payload = _invoke(
            _bash(tmp_path, template),
            "dd if=/dev/zero of=/workspace/big bs=1M count=64 2>&1",
        )
        if "No space left" not in payload.stdout:
            raise AssertionError('"No space left" in payload.stdout')

    def test_storage_upload_visible_in_sandbox(
        self, tmp_path: Path, template: Path
    ) -> None:
        storage = _storage(tmp_path, template)
        asyncio.run(storage.upload_file("7/t1/upload/отчёт.csv", b"attachment"))
        payload = _invoke(
            _bash(tmp_path, template), "cat '/workspace/t1/upload/отчёт.csv'"
        )
        if payload.stdout.strip() != "attachment":
            raise AssertionError('payload.stdout.strip() == "attachment"')

    def test_sandbox_write_readable_by_storage(
        self, tmp_path: Path, template: Path
    ) -> None:
        tool = _bash(tmp_path, template)
        _invoke(tool, "mkdir -p t1/upload && echo from-bash > t1/upload/x")
        storage = _storage(tmp_path, template)
        if asyncio.run(read_all(storage, "7/t1/upload/x")).strip() != b"from-bash":
            raise AssertionError('asyncio.run(read_all(storage, "7/t1/upload/x")).str…')

    def test_storage_roundtrip(self, tmp_path: Path, template: Path) -> None:
        storage = _storage(tmp_path, template)

        async def cycle() -> bytes:
            await storage.upload_file("7/t1/upload/el-2", "текст")
            return await read_all(storage, "7/t1/upload/el-2")

        if asyncio.run(cycle()).decode() != "текст":
            raise AssertionError('asyncio.run(cycle()).decode() == "текст"')

    def test_storage_delete(self, tmp_path: Path, template: Path) -> None:
        storage = _storage(tmp_path, template)

        async def cycle() -> tuple[bool, bool]:
            await storage.upload_file("7/t1/upload/el-3", b"x")
            return (
                await storage.delete_file("7/t1/upload/el-3"),
                await storage.delete_file("7/t1/upload/el-3"),
            )

        if asyncio.run(cycle()) != (True, False):
            raise AssertionError("asyncio.run(cycle()) == (True, False)")

    def test_storage_read_missing_raises(self, tmp_path: Path, template: Path) -> None:
        storage = _storage(tmp_path, template)
        asyncio.run(storage.upload_file("7/t1/upload/seed", b"x"))
        with pytest.raises(StorageNotFoundError):
            asyncio.run(read_all(storage, "7/t1/upload/missing"))

    def test_storage_stat_reports_size(self, tmp_path: Path, template: Path) -> None:
        storage = _storage(tmp_path, template)

        async def cycle() -> int:
            await storage.upload_file("7/t1/upload/sized.bin", b"x" * 1234)
            result = await storage.stat("7/t1/upload/sized.bin")
            return result.size

        if asyncio.run(cycle()) != 1234:
            raise AssertionError("asyncio.run(cycle()) == 1234")

    def test_storage_stat_missing_raises(self, tmp_path: Path, template: Path) -> None:
        storage = _storage(tmp_path, template)
        asyncio.run(storage.upload_file("7/t1/upload/seed", b"x"))

        with pytest.raises(StorageNotFoundError):
            asyncio.run(storage.stat("7/t1/upload/missing"))

    def test_storage_window_read(self, tmp_path: Path, template: Path) -> None:
        storage = _storage(tmp_path, template)

        async def cycle() -> tuple[int, bytes]:
            await storage.upload_file("7/t1/upload/win.bin", b"0123456789")
            opened = await storage.open_stream(
                "7/t1/upload/win.bin", ReadWindow(offset=4, length=3)
            )
            collected = bytearray()
            async for chunk in opened.chunks:
                collected.extend(chunk)
            return opened.stat.size, bytes(collected)

        if asyncio.run(cycle()) != (10, b"456"):
            raise AssertionError('asyncio.run(cycle()) == (10, b"456")')

    def test_storage_concurrent_reads(self, tmp_path: Path, template: Path) -> None:
        """Чтение под разделяемым локом: два окна одного образа идут параллельно."""
        storage = _storage(tmp_path, template)

        async def cycle() -> tuple[bytes, bytes]:
            await storage.upload_file("7/t1/upload/a.bin", b"aaa")
            await storage.upload_file("7/t1/upload/b.bin", b"bbb")
            first, second = await asyncio.gather(
                read_all(storage, "7/t1/upload/a.bin"),
                read_all(storage, "7/t1/upload/b.bin"),
            )
            return first, second

        if asyncio.run(cycle()) != (b"aaa", b"bbb"):
            raise AssertionError('asyncio.run(cycle()) == (b"aaa", b"bbb")')

    def test_directory_in_image_is_storage_error(
        self, tmp_path: Path, template: Path
    ) -> None:
        """Каталог вместо файла — отказ слоя, а не пустое тело и не 404."""
        storage = _storage(tmp_path, template)
        asyncio.run(storage.upload_file("7/t1/upload/a.txt", b"x"))

        with pytest.raises(StorageError) as failure:
            asyncio.run(storage.stat("7/t1/upload"))

        if isinstance(failure.value, StorageNotFoundError):
            raise AssertionError("not isinstance(failure.value, StorageNotFoundError)")

    def test_fifo_in_image_does_not_hang_read(
        self, tmp_path: Path, template: Path
    ) -> None:
        """Именованный канал в образе: read отказывает, а не ждёт писателя."""
        _invoke(
            _bash(tmp_path, template), "mkdir -p t1/upload && mkfifo t1/upload/pipe"
        )
        storage = _storage(tmp_path, template, op_timeout_sec=15)

        with pytest.raises(StorageError) as failure:
            asyncio.run(storage.stat("7/t1/upload/pipe"))

        if isinstance(failure.value, StorageNotFoundError):
            raise AssertionError("not isinstance(failure.value, StorageNotFoundError)")

    def test_symlink_planted_by_bash_leaks_nothing(
        self, tmp_path: Path, template: Path
    ) -> None:
        """Содержимое образа пишет bash: ссылка на файл хоста не должна читаться."""
        secret = tmp_path / "outside.txt"
        secret.write_bytes(b"host secret")

        _invoke(
            _bash(tmp_path, template),
            f"mkdir -p t1/upload && ln -s {secret} t1/upload/leak",
        )
        storage = _storage(tmp_path, template)

        with pytest.raises(StorageError) as failure:
            asyncio.run(read_all(storage, "7/t1/upload/leak"))

        if "host secret" in str(failure.value):
            raise AssertionError('"host secret" not in str(failure.value)')

    def test_symlinked_dir_planted_by_bash_leaks_nothing(
        self, tmp_path: Path, template: Path
    ) -> None:
        """Подмена каталога вложений ссылкой наружу тоже не проходит."""
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "secret.txt").write_bytes(b"host secret")

        _invoke(
            _bash(tmp_path, template),
            f"mkdir -p t1 && ln -s {outside} t1/upload",
        )
        storage = _storage(tmp_path, template)

        with pytest.raises(StorageError) as failure:
            asyncio.run(read_all(storage, "7/t1/upload/secret.txt"))

        if "host secret" in str(failure.value):
            raise AssertionError('"host secret" not in str(failure.value)')

    def test_read_does_not_materialize_image(
        self, tmp_path: Path, template: Path
    ) -> None:
        """Чтение из несозданного образа — not found, а не копия шаблона."""
        storage = _storage(tmp_path, template)

        with pytest.raises(StorageNotFoundError):
            asyncio.run(storage.stat("7/t1/upload/anything"))

        if (tmp_path / "ws" / "7.ext4").exists():
            raise AssertionError('not (tmp_path / "ws" / "7.ext4").exists()')

    def test_storage_waits_for_busy_image(self, tmp_path: Path, template: Path) -> None:
        """flock блокирующий: storage дожидается занятой песочницы."""
        tool = _bash(tmp_path, template, timeout_sec=30)
        storage = _storage(tmp_path, template)

        async def race() -> bytes:
            busy = asyncio.get_running_loop().run_in_executor(
                None, _invoke, tool, "sleep 3; echo done > held.txt"
            )
            await asyncio.sleep(0.5)
            await storage.upload_file("7/t1/upload/after", b"waited")
            await busy
            return await read_all(storage, "7/t1/upload/after")

        if asyncio.run(race()) != b"waited":
            raise AssertionError('asyncio.run(race()) == b"waited"')

    def test_image_not_recreated_on_second_call(
        self, tmp_path: Path, template: Path
    ) -> None:
        tool = _bash(tmp_path, template)
        _invoke(tool, "true")
        image = tmp_path / "ws" / "7.ext4"
        ino = image.stat().st_ino
        _invoke(tool, "true")
        if image.stat().st_ino != ino:
            raise AssertionError("image.stat().st_ino == ino")

    def test_image_copy_is_sparse(self, tmp_path: Path, template: Path) -> None:
        _invoke(_bash(tmp_path, template), "true")
        image = tmp_path / "ws" / "7.ext4"
        if image.stat().st_blocks * 512 >= image.stat().st_size:
            raise AssertionError("image.stat().st_blocks * 512 < image.stat().st_size")

    def test_concurrent_bash_calls_serialized(
        self, tmp_path: Path, template: Path
    ) -> None:
        tool = _bash(tmp_path, template, timeout_sec=60)
        futures = []
        with ThreadPoolExecutor(max_workers=2) as pool:
            for i in range(2):
                futures.append(pool.submit(_invoke, tool, f"echo {i} > par-{i}.txt"))
        for future in futures:
            if future.result().exit_code != 0:
                raise AssertionError('future.result().exit_code == 0')
        both = _invoke(tool, "cat par-0.txt par-1.txt")
        if both.stdout.split() != ["0", "1"]:
            raise AssertionError('both.stdout.split() == ["0", "1"]')

    def test_run_command_has_no_capabilities(
        self, tmp_path: Path, template: Path
    ) -> None:
        payload = _invoke(_bash(tmp_path, template), "grep CapEff /proc/self/status")
        if payload.stdout.split()[1] != "0000000000000000":
            raise AssertionError('payload.stdout.split()[1] == "0000000000000000"')

    def test_userns_creation_blocked(self, tmp_path: Path, template: Path) -> None:
        payload = _invoke(_bash(tmp_path, template), "unshare -U true 2>&1; echo rc=$?")
        if "rc=0" in payload.stdout:
            raise AssertionError('"rc=0" not in payload.stdout')

    def test_workspace_shared_between_threads(
        self, tmp_path: Path, template: Path
    ) -> None:
        """Образ на пользователя: второй тред видит файлы первого."""
        _invoke(_bash(tmp_path, template, thread_id="t1"), "echo from-t1 > shared.txt")
        payload = _invoke(_bash(tmp_path, template, thread_id="t2"), "cat shared.txt")
        if payload.stdout.strip() != "from-t1":
            raise AssertionError('payload.stdout.strip() == "from-t1"')

    def test_single_image_per_user(self, tmp_path: Path, template: Path) -> None:
        _invoke(_bash(tmp_path, template, thread_id="t1"), "true")
        _invoke(_bash(tmp_path, template, thread_id="t2"), "true")
        images: list[Path] = []
        for path in (tmp_path / "ws").iterdir():
            if path.suffix == ".ext4":
                images.append(path)
        if [p.name for p in images] != ["7.ext4"]:
            raise AssertionError('[p.name for p in images] == ["7.ext4"]')

    def test_upload_of_one_thread_visible_in_another(
        self, tmp_path: Path, template: Path
    ) -> None:
        storage = _storage(tmp_path, template)
        asyncio.run(storage.upload_file("7/t1/upload/shared.txt", b"attachment"))
        payload = _invoke(
            _bash(tmp_path, template, thread_id="t2"),
            "cat /workspace/t1/upload/shared.txt",
        )
        if payload.stdout.strip() != "attachment":
            raise AssertionError('payload.stdout.strip() == "attachment"')

    def test_hostname_is_neutral(self, tmp_path: Path, template: Path) -> None:
        payload = _invoke(_bash(tmp_path, template), "uname -n")
        if payload.stdout.strip() != "sandbox":
            raise AssertionError('payload.stdout.strip() == "sandbox"')

    def test_memory_limit_visible(self, tmp_path: Path, template: Path) -> None:
        tool = _bash(tmp_path, template, max_memory_bytes=64 * 1024 * 1024)
        payload = _invoke(tool, "ulimit -v")
        if payload.stdout.strip() != str(64 * 1024):
            raise AssertionError('payload.stdout.strip() == str(64 * 1024)')

    def test_cpu_limit_visible(self, tmp_path: Path, template: Path) -> None:
        tool = _bash(tmp_path, template, max_cpu_sec=5)
        payload = _invoke(tool, "ulimit -t")
        if payload.stdout.strip() != "5":
            raise AssertionError('payload.stdout.strip() == "5"')

    def test_memory_limit_enforced(self, tmp_path: Path, template: Path) -> None:
        tool = _bash(tmp_path, template, max_memory_bytes=64 * 1024 * 1024)
        payload = _invoke(
            tool, "dd if=/dev/zero of=/dev/null bs=200M count=1 2>&1; echo rc=$?"
        )
        if "rc=0" in payload.stdout:
            raise AssertionError('"rc=0" not in payload.stdout')

    def test_file_size_limit_visible(self, tmp_path: Path, template: Path) -> None:
        tool = _bash(tmp_path, template, max_file_size_bytes=8 * 1024 * 1024)
        payload = _invoke(tool, "ulimit -f")
        # bash показывает RLIMIT_FSIZE в блоках по 1024 байта
        if payload.stdout.strip() != str(8 * 1024 * 1024 // 1024):
            raise AssertionError('payload.stdout.strip() == str(8 * 1024 * 1024 //…')

    def test_open_files_limit_visible(self, tmp_path: Path, template: Path) -> None:
        tool = _bash(tmp_path, template, max_open_files=128)
        payload = _invoke(tool, "ulimit -n")
        if payload.stdout.strip() != "128":
            raise AssertionError('payload.stdout.strip() == "128"')

    def test_process_limit_visible(self, tmp_path: Path, template: Path) -> None:
        tool = _bash(tmp_path, template, max_processes=32)
        payload = _invoke(tool, "ulimit -u")
        if payload.stdout.strip() != "32":
            raise AssertionError('payload.stdout.strip() == "32"')

    def test_fork_bomb_capped(self, tmp_path: Path, template: Path) -> None:
        code = (
            "import os, time\n"
            "count = 0\n"
            "while count < 40:\n"
            "    try:\n"
            "        pid = os.fork()\n"
            "    except OSError:\n"
            "        break\n"
            "    if pid == 0:\n"
            "        time.sleep(3)\n"
            "        os._exit(0)\n"
            "    count += 1\n"
            "print('forked', count)\n"
        )
        tool = _bash(tmp_path, template, max_processes=16, timeout_sec=60)
        payload = _invoke(tool, "python3 -", stdin=code)
        forked = int(payload.stdout.split()[1])
        if not (0 < forked < 40):
            raise AssertionError("0 < forked < 40")

    def test_file_size_limit_enforced(self, tmp_path: Path, template: Path) -> None:
        tool = _bash(tmp_path, template, max_file_size_bytes=1024 * 1024)
        payload = _invoke(
            tool, "dd if=/dev/zero of=big bs=64k count=32 2>&1; echo rc=$?"
        )
        if "rc=0" in payload.stdout:
            raise AssertionError('"rc=0" not in payload.stdout')

    def test_broken_template_raises_mount_error(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.ext4"
        bad.write_bytes(b"not an ext4 image")
        tool = _bash(tmp_path, bad)
        with pytest.raises(SandboxMountError, match="image not mounted"):
            _invoke(tool, "true")

    def test_broken_template_stays_inside_storage_errors(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.ext4"
        bad.write_bytes(b"not an ext4 image")
        storage = _storage(tmp_path, bad)

        with pytest.raises(StorageError, match="image not mounted"):
            asyncio.run(storage.upload_file("7/t1/upload/x", b"x"))

    def test_upload_without_overwrite_keeps_existing(
        self, tmp_path: Path, template: Path
    ) -> None:
        storage = _storage(tmp_path, template)

        async def cycle() -> bytes:
            await storage.upload_file("7/t1/upload/keep", b"first")
            await storage.upload_file("7/t1/upload/keep", b"second", overwrite=False)
            return await read_all(storage, "7/t1/upload/keep")

        if asyncio.run(cycle()) != b"first":
            raise AssertionError('asyncio.run(cycle()) == b"first"')

    def test_slow_source_outlives_op_timeout(
        self, tmp_path: Path, template: Path
    ) -> None:
        """Медленный клиент — не зависание: пока чанки идут, операция живёт."""
        storage = _storage(tmp_path, template, op_timeout_sec=2)

        async def slow() -> AsyncIterator[bytes]:
            for _ in range(8):
                await asyncio.sleep(0.4)
                yield b"x" * 1024

        async def cycle() -> bytes:
            await storage.upload_stream("7/t1/upload/slow.bin", slow())
            return await read_all(storage, "7/t1/upload/slow.bin")

        if asyncio.run(cycle()) != b"x" * 1024 * 8:
            raise AssertionError('asyncio.run(cycle()) == b"x" * 1024 * 8')

    def test_stalled_source_hits_op_timeout(
        self, tmp_path: Path, template: Path
    ) -> None:
        storage = _storage(tmp_path, template, op_timeout_sec=1)

        async def stalled() -> AsyncIterator[bytes]:
            yield b"data"
            await asyncio.sleep(30)
            yield b"tail"

        async def cycle() -> None:
            await storage.upload_stream("7/t1/upload/stall.bin", stalled())

        with pytest.raises(StorageError, match="stalled"):
            asyncio.run(cycle())
