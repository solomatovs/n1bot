"""Премонтированный корень: монтирование на старте, подмена профиля, запуск."""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from boba.sandbox import (
    RootfsPremount,
    RootfsPremountError,
    SandboxCaller,
    SandboxProfile,
)
from boba.workspace.launcher import FUSE_DEVICE, FuseMounter

needs_fuse = pytest.mark.skipif(
    shutil.which("bwrap") is None
    or shutil.which("fuse2fs") is None
    or shutil.which("mkfs.ext4") is None
    or not os.path.exists(FUSE_DEVICE),
    reason="нужны bwrap, fuse2fs, mkfs.ext4 и /dev/fuse",
)

HOST_RO_BINDS = ("/usr", "/bin", "/sbin", "/lib", "/lib64", "/etc/alternatives")


def _bin_dirs() -> list[str]:
    dirs: list[str] = []
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        if entry.startswith("/"):
            dirs.append(entry)

    return dirs


def _profile(**kw: Any) -> SandboxProfile:
    base: dict[str, Any] = {
        "rootfs": "",
        "rootfs_image": "",
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
    return SandboxProfile.model_validate({**base, **kw})


@pytest.fixture
def mini_rootfs(tmp_path: Path) -> str:
    """Маленький ext4-образ с файлом-маркером в корне."""
    tree = tmp_path / "tree"
    tree.mkdir()
    (tree / "marker").write_text("premounted")

    # точки монтирования bwrap: на ro-корне он их создать не может;
    # хостовые симлинки (/bin -> usr/bin) bwrap воспроизводит сам
    for name in ("proc", "dev", "tmp"):
        (tree / name).mkdir()

    for path in HOST_RO_BINDS:
        if os.path.islink(path):
            (tree / path.lstrip("/")).symlink_to(os.readlink(path))
            continue

        if os.path.isdir(path):
            (tree / path.lstrip("/")).mkdir(parents=True, exist_ok=True)

    mkfs = shutil.which("mkfs.ext4")
    if mkfs is None:
        pytest.skip("mkfs.ext4 недоступен")

    image = tmp_path / "mini-rootfs.ext4"
    subprocess.run(  # noqa: S603
        [
            mkfs,
            "-q",
            "-F",
            "-d",
            str(tree),
            str(image),
            "4m",
        ],
        check=True,
        capture_output=True,
    )
    return str(image)


@pytest.fixture
def premount(tmp_path: Path) -> Iterator[RootfsPremount]:
    mounter = RootfsPremount(str(tmp_path / "mnt"))
    try:
        yield mounter
    finally:
        RootfsPremount.reset()
        mounter.shutdown()


class TestApply:
    def test_without_active_returns_profile_as_is(self) -> None:
        RootfsPremount.reset()
        profile = _profile(rootfs_image="/srv/rootfs.ext4")

        if RootfsPremount.apply(profile) is not profile:
            raise AssertionError("без активного режима профиль не меняется")

    def test_unmounted_image_is_refused(
        self, premount: RootfsPremount
    ) -> None:
        RootfsPremount.activate(premount)
        profile = _profile(rootfs_image="/srv/rootfs.ext4")

        with pytest.raises(RootfsPremountError):
            RootfsPremount.apply(profile)

    def test_profile_without_image_passes_through(
        self, premount: RootfsPremount
    ) -> None:
        RootfsPremount.activate(premount)
        profile = _profile(rootfs="/srv/dir")

        if RootfsPremount.apply(profile) is not profile:
            raise AssertionError("профиль без rootfs_image не меняется")


@needs_fuse
class TestMount:
    def test_mount_apply_run_shutdown(
        self, premount: RootfsPremount, mini_rootfs: str
    ) -> None:
        profile = _profile(
            rootfs_image=mini_rootfs,
            ro_binds=tuple(
                {"host": path, "target": path}
                for path in HOST_RO_BINDS
                if os.path.exists(path)
            ),
            tmpfs=({"path": "/tmp", "size_bytes": 16 * 1024 * 1024},),  # noqa: S108
        )
        premount.mount_profiles({"base": profile})
        RootfsPremount.activate(premount)

        applied = RootfsPremount.apply(profile)
        if applied.rootfs_image:
            raise AssertionError("rootfs_image должен быть снят")

        marker = os.path.join(applied.rootfs, "marker")
        if not os.path.exists(marker):
            raise AssertionError("маркер образа не виден в точке монтирования")

        caller = SandboxCaller("premount-test", applied, dict)
        outcome = caller.call_text("cat /marker", stdin="")

        if outcome.result.exit_code != 0:
            raise AssertionError(
                f"rc={outcome.result.exit_code}: {outcome.result.stderr}"
            )

        if outcome.result.stdout.strip() != "premounted":
            raise AssertionError(f"stdout={outcome.result.stdout!r}")

        mnt = applied.rootfs
        premount.shutdown()

        if FuseMounter.is_mounted(os.path.realpath(mnt)):
            raise AssertionError("после shutdown точка должна быть свободна")

    def test_same_image_mounted_once(
        self, premount: RootfsPremount, mini_rootfs: str
    ) -> None:
        first = _profile(rootfs_image=mini_rootfs)
        second = _profile(rootfs_image=mini_rootfs, network=True)

        premount.mount_profiles({"a": first, "b": second})
        RootfsPremount.activate(premount)

        applied_a = RootfsPremount.apply(first)
        applied_b = RootfsPremount.apply(second)

        if applied_a.rootfs != applied_b.rootfs:
            raise AssertionError("один образ должен монтироваться один раз")

    def test_stale_mount_is_released(
        self, tmp_path: Path, mini_rootfs: str
    ) -> None:
        profile = _profile(rootfs_image=mini_rootfs)

        stale = RootfsPremount(str(tmp_path / "mnt"))
        stale.mount_profiles({"base": profile})
        # демоны stale-экземпляра намеренно не гасятся: имитация прошлого запуска

        fresh = RootfsPremount(str(tmp_path / "mnt"))
        try:
            fresh.mount_profiles({"base": profile})
            applied = fresh._apply(profile)
            if not os.path.exists(os.path.join(applied.rootfs, "marker")):
                raise AssertionError("свежий mount не отдаёт содержимое")
        finally:
            fresh.shutdown()
            stale.shutdown()
