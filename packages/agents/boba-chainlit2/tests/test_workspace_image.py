"""Workspace-образ: общий контейнер треда для песочницы и storage."""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from boba.chainlit2.agent.tools.sandbox.config import BashSandboxConfig
from boba.chainlit2.agent.tools.sandbox.profile import SandboxProfile
from boba.chainlit2.agent.tools.sandbox.tools import build_bash_tool
from boba.chainlit2.chat.data.storage import ImageStorageClient, LocalStorageClient
from boba.chainlit2.infra.config import LocalStorageConfig
from boba.chainlit2.workspace import FUSE_DEVICE, build_chain_argv, render_image_path
from boba.chainlit2.workspace.options import LauncherOptions, ResourceLimits

HOST_RO_BINDS = ("/usr", "/bin", "/sbin", "/lib", "/lib64", "/etc/alternatives")


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
    assert mkfs is not None
    subprocess.run(  # noqa: S603 — argv фиксирован, путь резолвится which
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
    "launcher": {},
    "tmpfs": (),
    "network": False,
    "env_set": {},
    "timeout_sec": 30,
    "max_memory_bytes": 512 * 1024 * 1024,
    "max_cpu_sec": 30,
    "max_file_size_bytes": 64 * 1024 * 1024,
    "max_open_files": 256,
    "max_processes": 256,
    "max_output_bytes": 256 * 1024,
    "cwd": "",
}


def _profile(**kw: object) -> SandboxProfile:
    return SandboxProfile.model_validate({**_PROFILE_BASE, **kw})


def _bash(tmp_path: Path, template: Path, thread_id: str = "t1", **profile_kw):
    cfg = BashSandboxConfig(
        profiles={
            "default": _profile(
                ro_binds=HOST_RO_BINDS,
                rw_images=(f"{_image_tpl(tmp_path)}:/workspace",),
                image_template=str(template),
                cwd="/workspace",
                **profile_kw,
            )
        },
        default_profile="default",
    )
    return build_bash_tool(cfg, lambda: {"user_id": "7", "thread_id": thread_id})


def _invoke(tool, command: str, stdin: str = "") -> dict:
    msg = tool.invoke(
        {
            "args": {"command": command, "stdin": stdin, "profile": ""},
            "id": "call-bash",
            "name": "bash",
            "type": "tool_call",
        }
    )
    return msg.artifact.payload


def _storage(tmp_path: Path, template: Path) -> ImageStorageClient:
    cfg = LocalStorageConfig(
        kind="image",
        image_path=_image_tpl(tmp_path),
        image_template=str(template),
        op_timeout_sec=60,
    )
    client = LocalStorageClient.from_config(cfg)
    assert isinstance(client, ImageStorageClient)
    return client


class TestConfig:
    def test_image_kind_requires_paths(self) -> None:
        with pytest.raises(ValueError, match="image_path and image_template"):
            LocalStorageConfig(kind="image")

    def test_local_kind_requires_files_dir(self) -> None:
        with pytest.raises(ValueError, match="requires files_dir"):
            LocalStorageConfig(kind="local")

    def test_unknown_kind_rejected(self) -> None:
        with pytest.raises(ValueError, match="'local' or 'image'"):
            LocalStorageConfig.model_validate({"kind": "s3", "files_dir": "/srv"})

    def test_factory_picks_image_client(self) -> None:
        cfg = LocalStorageConfig(
            kind="image", image_path="/ws/{user_id}/{thread_id}.ext4",
            image_template="/t.ext4",
        )
        assert isinstance(LocalStorageClient.from_config(cfg), ImageStorageClient)

    def test_factory_picks_local_client(self) -> None:
        cfg = LocalStorageConfig(files_dir="/srv/files")
        assert type(LocalStorageClient.from_config(cfg)) is LocalStorageClient

    def test_profile_images_require_template(self) -> None:
        with pytest.raises(ValueError, match="image_template is empty"):
            _profile(rw_images=("/ws/a.ext4:/workspace",))


class TestObjectKey:
    @staticmethod
    def _client() -> ImageStorageClient:
        cfg = LocalStorageConfig(
            kind="image", image_path="/ws/{user_id}/{thread_id}.ext4",
            image_template="/t.ext4",
        )
        client = LocalStorageClient.from_config(cfg)
        assert isinstance(client, ImageStorageClient)
        return client

    def test_key_splits_into_image_and_rel(self) -> None:
        assert self._client()._image_and_rel("7/t1/upload/el-1") == (
            "/ws/7/t1.ext4",
            "upload/el-1",
        )

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
        cfg = LocalStorageConfig(
            kind="image",
            image_path="./data/ws/{user_id}/{thread_id}.ext4",
            image_template="./data/tpl.ext4",
        )
        assert cfg.image_path.startswith("/")
        assert cfg.image_template.startswith("/")

    def test_relative_image_bound_by_absolute_path(self) -> None:
        argv = build_chain_argv(
            images=[("./ws/a.ext4", "./ws/a.ext4.mnt")],
            template="./t.ext4",
            op=["write", "upload/x"],
            python_bin="/usr/bin/python3",
            options=LauncherOptions(),
            limits=ResourceLimits(),
            rw_paths=["./shared"],
        )
        binds = []
        for i, arg in enumerate(argv):
            if arg == "--bind":
                binds.append(argv[i + 1])
        assert binds
        for path in binds:
            assert os.path.isabs(path), path
        assert argv[argv.index("--image") + 1].startswith("/")


class TestChainArgv:
    @staticmethod
    def _argv(**kw) -> list[str]:
        return build_chain_argv(
            images=[("/ws/a.ext4", "/ws/a.ext4.mnt")],
            template="/t.ext4",
            op=["write", "upload/x"],
            python_bin="/usr/bin/python3",
            options=LauncherOptions(),
            limits=ResourceLimits(),
            **kw,
        )

    def test_limits_rendered_as_flags(self) -> None:
        limits = ResourceLimits(max_memory_bytes=64 * 1024 * 1024, max_cpu_sec=5)
        argv = build_chain_argv(
            images=[("/ws/a.ext4", "/ws/a.ext4.mnt")],
            template="/t.ext4",
            op=["write", "upload/x"],
            python_bin="/usr/bin/python3",
            options=LauncherOptions(),
            limits=limits,
        )
        memory = argv[argv.index("--max-memory-bytes") + 1]
        cpu = argv[argv.index("--max-cpu-sec") + 1]
        assert memory == str(64 * 1024 * 1024)
        assert cpu == "5"

    def test_outer_bwrap_is_root_in_userns(self) -> None:
        argv = self._argv()
        assert argv[0].endswith("bwrap")
        assert os.path.isabs(argv[0]) or shutil.which("bwrap") is None
        assert "--unshare-user" in argv
        assert argv[argv.index("--uid") + 1] == "0"

    def test_namespaces_isolated_with_neutral_hostname(self) -> None:
        argv = self._argv()
        assert "--unshare-pid" in argv
        assert "--unshare-ipc" in argv
        assert "--new-session" in argv
        assert argv[argv.index("--hostname") + 1] == "sandbox"

    def test_host_fs_readonly_except_image_dir(self) -> None:
        argv = self._argv()
        i = argv.index("--ro-bind")
        assert argv[i + 1 : i + 3] == ["/", "/"]
        b = argv.index("--bind")
        assert argv[b + 1 : b + 3] == ["/ws", "/ws"]

    def test_rw_paths_bound_writable(self) -> None:
        argv = self._argv(rw_paths=("/srv/data",))
        assert "/srv/data" in argv

    def test_only_fuse_device_exposed(self) -> None:
        argv = self._argv()
        assert argv[argv.index("--dev") + 1] == "/dev"
        assert argv[argv.index("--dev-bind") + 1] == "/dev/fuse"

    def test_env_cleared_to_path_only(self) -> None:
        argv = self._argv()
        assert "--clearenv" in argv
        assert argv[argv.index("--setenv") + 1] == "PATH"

    def test_sys_admin_capability_added(self) -> None:
        argv = self._argv()
        assert argv[argv.index("--cap-add") + 1] == "CAP_SYS_ADMIN"

    def test_network_isolated_by_default(self) -> None:
        assert "--unshare-net" in self._argv()

    def test_network_enabled_on_demand(self) -> None:
        assert "--unshare-net" not in self._argv(network=True)

    def test_launcher_gets_image_and_op(self) -> None:
        argv = self._argv()
        i = argv.index("--image")
        assert argv[i + 1 : i + 3] == ["/ws/a.ext4", "/ws/a.ext4.mnt"]
        assert argv[-2:] == ["write", "upload/x"]


@needs_fuse
class TestLiveImage:
    """Реальные монтирования: bash и storage поверх одного образа."""

    def test_write_persists_between_calls(self, tmp_path: Path, template: Path) -> None:
        tool = _bash(tmp_path, template)
        _invoke(tool, "echo hello > f.txt")
        assert _invoke(tool, "cat f.txt")["stdout"].strip() == "hello"

    def test_image_created_from_template(self, tmp_path: Path, template: Path) -> None:
        _invoke(_bash(tmp_path, template), "true")
        assert (tmp_path / "ws" / "7.ext4").is_file()

    def test_no_mount_leaks_to_host(self, tmp_path: Path, template: Path) -> None:
        _invoke(_bash(tmp_path, template), "true")
        assert "fuse2fs" not in Path("/proc/mounts").read_text()

    def test_size_limit_is_enforced(self, tmp_path: Path, template: Path) -> None:
        payload = _invoke(
            _bash(tmp_path, template),
            "dd if=/dev/zero of=/workspace/big bs=1M count=64 2>&1",
        )
        assert "No space left" in payload["stdout"]

    def test_storage_upload_visible_in_sandbox(
        self, tmp_path: Path, template: Path
    ) -> None:
        storage = _storage(tmp_path, template)
        asyncio.run(storage.upload_file("7/t1/upload/el-1", b"attachment"))
        payload = _invoke(_bash(tmp_path, template), "cat /workspace/upload/el-1")
        assert payload["stdout"].strip() == "attachment"

    def test_sandbox_write_readable_by_storage(
        self, tmp_path: Path, template: Path
    ) -> None:
        tool = _bash(tmp_path, template)
        _invoke(tool, "mkdir -p upload && echo from-bash > upload/x")
        storage = _storage(tmp_path, template)
        assert asyncio.run(storage.read_file("7/t1/upload/x")).strip() == b"from-bash"

    def test_storage_roundtrip(self, tmp_path: Path, template: Path) -> None:
        storage = _storage(tmp_path, template)

        async def cycle() -> bytes:
            await storage.upload_file("7/t1/upload/el-2", "текст")
            return await storage.read_file("7/t1/upload/el-2")

        assert asyncio.run(cycle()).decode() == "текст"

    def test_storage_delete(self, tmp_path: Path, template: Path) -> None:
        storage = _storage(tmp_path, template)

        async def cycle() -> tuple[bool, bool]:
            await storage.upload_file("7/t1/upload/el-3", b"x")
            return (
                await storage.delete_file("7/t1/upload/el-3"),
                await storage.delete_file("7/t1/upload/el-3"),
            )

        assert asyncio.run(cycle()) == (True, False)

    def test_storage_read_missing_raises(
        self, tmp_path: Path, template: Path
    ) -> None:
        storage = _storage(tmp_path, template)
        asyncio.run(storage.upload_file("7/t1/upload/seed", b"x"))
        with pytest.raises(FileNotFoundError):
            asyncio.run(storage.read_file("7/t1/upload/missing"))

    def test_storage_waits_for_busy_image(
        self, tmp_path: Path, template: Path
    ) -> None:
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
            return await storage.read_file("7/t1/upload/after")

        assert asyncio.run(race()) == b"waited"

    def test_image_not_recreated_on_second_call(
        self, tmp_path: Path, template: Path
    ) -> None:
        tool = _bash(tmp_path, template)
        _invoke(tool, "true")
        image = tmp_path / "ws" / "7.ext4"
        ino = image.stat().st_ino
        _invoke(tool, "true")
        assert image.stat().st_ino == ino

    def test_image_copy_is_sparse(self, tmp_path: Path, template: Path) -> None:
        _invoke(_bash(tmp_path, template), "true")
        image = tmp_path / "ws" / "7.ext4"
        assert image.stat().st_blocks * 512 < image.stat().st_size

    def test_concurrent_bash_calls_serialized(
        self, tmp_path: Path, template: Path
    ) -> None:
        tool = _bash(tmp_path, template, timeout_sec=60)
        futures = []
        with ThreadPoolExecutor(max_workers=2) as pool:
            for i in range(2):
                futures.append(pool.submit(_invoke, tool, f"echo {i} > par-{i}.txt"))
        for future in futures:
            assert future.result()["exit_code"] == 0
        both = _invoke(tool, "cat par-0.txt par-1.txt")
        assert both["stdout"].split() == ["0", "1"]

    def test_run_command_has_no_capabilities(
        self, tmp_path: Path, template: Path
    ) -> None:
        payload = _invoke(_bash(tmp_path, template), "grep CapEff /proc/self/status")
        assert payload["stdout"].split()[1] == "0000000000000000"

    def test_userns_creation_blocked(self, tmp_path: Path, template: Path) -> None:
        payload = _invoke(
            _bash(tmp_path, template), "unshare -U true 2>&1; echo rc=$?"
        )
        assert "rc=0" not in payload["stdout"]

    def test_workspace_shared_between_threads(
        self, tmp_path: Path, template: Path
    ) -> None:
        """Образ на пользователя: второй тред видит файлы первого."""
        _invoke(_bash(tmp_path, template, thread_id="t1"), "echo from-t1 > shared.txt")
        payload = _invoke(_bash(tmp_path, template, thread_id="t2"), "cat shared.txt")
        assert payload["stdout"].strip() == "from-t1"

    def test_single_image_per_user(self, tmp_path: Path, template: Path) -> None:
        _invoke(_bash(tmp_path, template, thread_id="t1"), "true")
        _invoke(_bash(tmp_path, template, thread_id="t2"), "true")
        images: list[Path] = []
        for path in (tmp_path / "ws").iterdir():
            if path.suffix == ".ext4":
                images.append(path)
        assert [p.name for p in images] == ["7.ext4"]

    def test_upload_of_one_thread_visible_in_another(
        self, tmp_path: Path, template: Path
    ) -> None:
        storage = _storage(tmp_path, template)
        asyncio.run(storage.upload_file("7/t1/upload/el-9", b"attachment"))
        payload = _invoke(
            _bash(tmp_path, template, thread_id="t2"), "cat /workspace/upload/el-9"
        )
        assert payload["stdout"].strip() == "attachment"

    def test_hostname_is_neutral(self, tmp_path: Path, template: Path) -> None:
        payload = _invoke(_bash(tmp_path, template), "uname -n")
        assert payload["stdout"].strip() == "sandbox"

    def test_memory_limit_visible(self, tmp_path: Path, template: Path) -> None:
        tool = _bash(tmp_path, template, max_memory_bytes=64 * 1024 * 1024)
        payload = _invoke(tool, "ulimit -v")
        assert payload["stdout"].strip() == str(64 * 1024)

    def test_cpu_limit_visible(self, tmp_path: Path, template: Path) -> None:
        tool = _bash(tmp_path, template, max_cpu_sec=5)
        payload = _invoke(tool, "ulimit -t")
        assert payload["stdout"].strip() == "5"

    def test_memory_limit_enforced(self, tmp_path: Path, template: Path) -> None:
        tool = _bash(tmp_path, template, max_memory_bytes=64 * 1024 * 1024)
        payload = _invoke(
            tool, "dd if=/dev/zero of=/dev/null bs=200M count=1 2>&1; echo rc=$?"
        )
        assert "rc=0" not in payload["stdout"]

    def test_file_size_limit_visible(self, tmp_path: Path, template: Path) -> None:
        tool = _bash(tmp_path, template, max_file_size_bytes=8 * 1024 * 1024)
        payload = _invoke(tool, "ulimit -f")
        # bash показывает RLIMIT_FSIZE в блоках по 1024 байта
        assert payload["stdout"].strip() == str(8 * 1024 * 1024 // 1024)

    def test_open_files_limit_visible(self, tmp_path: Path, template: Path) -> None:
        tool = _bash(tmp_path, template, max_open_files=128)
        payload = _invoke(tool, "ulimit -n")
        assert payload["stdout"].strip() == "128"

    def test_process_limit_visible(self, tmp_path: Path, template: Path) -> None:
        tool = _bash(tmp_path, template, max_processes=32)
        payload = _invoke(tool, "ulimit -u")
        assert payload["stdout"].strip() == "32"

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
        forked = int(payload["stdout"].split()[1])
        assert 0 < forked < 40

    def test_file_size_limit_enforced(self, tmp_path: Path, template: Path) -> None:
        tool = _bash(tmp_path, template, max_file_size_bytes=1024 * 1024)
        payload = _invoke(
            tool, "dd if=/dev/zero of=big bs=64k count=32 2>&1; echo rc=$?"
        )
        assert "rc=0" not in payload["stdout"]

    def test_broken_template_raises_mount_error(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.ext4"
        bad.write_bytes(b"not an ext4 image")
        tool = _bash(tmp_path, bad)
        with pytest.raises(RuntimeError, match="image not mounted"):
            _invoke(tool, "true")

    def test_upload_without_overwrite_keeps_existing(
        self, tmp_path: Path, template: Path
    ) -> None:
        storage = _storage(tmp_path, template)

        async def cycle() -> bytes:
            await storage.upload_file("7/t1/upload/keep", b"first")
            await storage.upload_file("7/t1/upload/keep", b"second", overwrite=False)
            return await storage.read_file("7/t1/upload/keep")

        assert asyncio.run(cycle()) == b"first"
