"""Групповые лимиты: cgroup v2 leaf на запуск; тесты идут в зоне user@<uid>.service.
Миграция в leaf работает, только если pytest запущен внутри зоны (systemd-run)."""

from __future__ import annotations

import contextlib
import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest

from boba.sandbox.cgroup import CgroupError, CgroupManager, GroupLimits
from boba.sandbox.profile import SandboxProfile
from boba.stand.zygote import ROOTFS_IMAGE, ProfileFields
from boba.workspace.launcher import ResourceLimits


def _bin_dirs() -> list[str]:
    """В тестах каталоги берутся из PATH; в проде их задаёт конфиг."""
    dirs: list[str] = []

    for entry in os.environ.get("PATH", "").split(os.pathsep):
        if not entry.startswith("/"):
            continue

        dirs.append(entry)

    return dirs


@pytest.fixture(autouse=True)
def chainlit_context() -> None:
    pass


class TestGroupLimits:
    """Чистая логика: контроллеры, формат cpu.max, признак «запрошено»."""

    def test_not_requested_when_absent(self) -> None:
        if GroupLimits().requested is not False:
            raise AssertionError("GroupLimits().requested is False")

    def test_requested_by_any_field(self) -> None:
        if GroupLimits(memory_bytes=1).requested is not True:
            raise AssertionError("GroupLimits(memory_bytes=1).requested is True")
        if GroupLimits(cpu_percent=100).requested is not True:
            raise AssertionError("GroupLimits(cpu_percent=100).requested is True")
        if GroupLimits(cpu_weight=100).requested is not True:
            raise AssertionError("GroupLimits(cpu_weight=100).requested is True")
        if GroupLimits(pids_max=10).requested is not True:
            raise AssertionError("GroupLimits(pids_max=10).requested is True")
        if GroupLimits(swap_max_bytes=0).requested is not True:
            raise AssertionError("GroupLimits(swap_max_bytes=0).requested is True")
        if GroupLimits(oom_kill_all=False).requested is not True:
            raise AssertionError("GroupLimits(oom_kill_all=False).requested is True")

    def test_controllers_follow_fields(self) -> None:
        if GroupLimits().controllers != ():
            raise AssertionError("GroupLimits().controllers == ()")
        if GroupLimits(memory_bytes=1).controllers != ("memory",):
            raise AssertionError('GroupLimits(memory_bytes=1).controllers == ("memory…')
        if GroupLimits(swap_max_bytes=0).controllers != ("memory",):
            raise AssertionError('GroupLimits(swap_max_bytes=0).controllers == ("memo…')
        if GroupLimits(oom_kill_all=True).controllers != ("memory",):
            raise AssertionError('GroupLimits(oom_kill_all=True).controllers == ("mem…')
        if GroupLimits(cpu_weight=50).controllers != ("cpu",):
            raise AssertionError('GroupLimits(cpu_weight=50).controllers == ("cpu",)')
        if GroupLimits(pids_max=5).controllers != ("pids",):
            raise AssertionError('GroupLimits(pids_max=5).controllers == ("pids",)')
        every = GroupLimits(memory_bytes=1, cpu_percent=100, pids_max=5)
        if every.controllers != ("cpu", "memory", "pids"):
            raise AssertionError('every.controllers == ("cpu", "memory", "pids")')

    def test_cpu_max_is_quota_over_period(self) -> None:
        if GroupLimits(cpu_percent=100).cpu_max != "100000 100000":
            raise AssertionError('GroupLimits(cpu_percent=100).cpu_max == "100000 100…')
        if GroupLimits(cpu_percent=150).cpu_max != "150000 100000":
            raise AssertionError('GroupLimits(cpu_percent=150).cpu_max == "150000 100…')
        if GroupLimits(cpu_percent=50).cpu_max != "50000 100000":
            raise AssertionError('GroupLimits(cpu_percent=50).cpu_max == "50000 10000…')

    def test_cpu_max_undefined_without_percent(self) -> None:
        with pytest.raises(ValueError, match="cpu_percent is not set"):
            _ = GroupLimits(cpu_weight=100).cpu_max


class TestProfileValidation:
    """cgroup_*-лимиты без cgroup_base — ошибка конфига, не сюрприз в рантайме."""

    @staticmethod
    def _profile(**overrides: object) -> SandboxProfile:
        fields: dict[str, object] = {
            "host": {
                "mounting": {
                    "mount_wait_sec": 1.0,
                    "mount_poll_sec": 0.05,
                    "shutdown_wait_sec": 1.0,
                    "lock_wait_sec": 10.0,
                    "copy_chunk_bytes": 1048576,
                },
                "binaries": {"dirs": _bin_dirs()},
                "stderr_tail_bytes": 4096,
                "channel_limit_bytes": 67108864,
                "fail_tail_chars": 2000,
                "kill_grace_sec": 5,
                "cgroup_base": "",
            },
            "rootfs": str(ROOTFS_IMAGE),
            "mounts": {
                "tmp": "64M",
                "ro": [],
                "rw": [],
            },
            "isolation": {
                "network": False,
                "env": {},
                "reap_poll_sec": 0.05,
            },
            "limits": {
                "timeout_sec": 5,
                "process_memory_bytes": 1,
                "process_cpu_sec": 1,
                "process_file_bytes": 1,
                "process_open_files": 1,
                "process_oom_score_adj": 0,
            },
            "run": {
                "shell": "/bin/bash",
                "cwd": "",
            },
        }
        return SandboxProfile.model_validate(ProfileFields.merged(fields, overrides))

    def test_group_limits_require_cgroup_base(self) -> None:
        with pytest.raises(ValueError, match="cgroup_base is empty"):
            self._profile(group_cpu_percent=100)

    def test_any_single_group_field_requires_base(self) -> None:
        with pytest.raises(ValueError, match="cgroup_base is empty"):
            self._profile(group_oom_kill_all=True)

    def test_cgroup_base_must_be_absolute(self) -> None:
        with pytest.raises(ValueError, match="must be absolute"):
            self._profile(cgroup_base="relative/path", group_cpu_weight=10)

    def test_absent_group_limits_allow_empty_base(self) -> None:
        profile = self._profile()
        if profile.limits.group_memory_bytes is not None:
            raise AssertionError("profile.limits.group_memory_bytes is None")
        if profile.limits.group_oom_kill_all is not None:
            raise AssertionError("profile.limits.group_oom_kill_all is None")

    def test_zero_is_not_a_switch_anymore(self) -> None:
        """«Выключено» выражается отсутствием параметра, а не нулём."""
        with pytest.raises(ValueError, match="greater than 0"):
            self._profile(cgroup_base="/sys/fs/cgroup/x", group_cpu_percent=0)


_UID = os.getuid()
_DELEGATED_PARENT = f"/sys/fs/cgroup/boba.slice/user-{_UID}.slice/user@{_UID}.service"

needs_delegation = pytest.mark.skipif(
    not os.path.isdir(_DELEGATED_PARENT),
    reason="нет делегированной systemd user-зоны (cgroup v2)",
)


@pytest.fixture
def base():
    path = os.path.join(_DELEGATED_PARENT, f"boba-pytest-{uuid4().hex[:8]}")
    yield path
    CgroupManager._prepared.pop(Path(path), None)
    with contextlib.suppress(OSError):
        os.rmdir(path)


class TestCgroupManager:
    """Живой cgroup: leaf создаётся, лимиты записываются, уборка полная."""

    @staticmethod
    def _knob(leaf: Path, name: str) -> str:
        path = (leaf / name).resolve()
        return path.read_text().strip()

    @needs_delegation
    def test_acquire_writes_limits_and_release_removes(self, base: str) -> None:
        manager = CgroupManager(base)
        limits = GroupLimits(
            memory_bytes=64 * 1024 * 1024,
            cpu_percent=50,
            cpu_weight=25,
            pids_max=17,
            swap_max_bytes=0,
            oom_kill_all=True,
        )
        leaf = manager.acquire(limits)
        try:
            if self._knob(leaf, "memory.max") != str(limits.memory_bytes):
                raise AssertionError('self._knob(leaf, "memory.max") == str(limits.me…')
            if self._knob(leaf, "cpu.max").split() != ["50000", "100000"]:
                raise AssertionError('self._knob(leaf, "cpu.max").split() == ["50000"…')
            if self._knob(leaf, "cpu.weight") != "25":
                raise AssertionError('self._knob(leaf, "cpu.weight") == "25"')
            if self._knob(leaf, "pids.max") != "17":
                raise AssertionError('self._knob(leaf, "pids.max") == "17"')
            if self._knob(leaf, "memory.swap.max") != "0":
                raise AssertionError('self._knob(leaf, "memory.swap.max") == "0"')
            if self._knob(leaf, "memory.oom.group") != "1":
                raise AssertionError('self._knob(leaf, "memory.oom.group") == "1"')
        finally:
            manager.release(leaf)
        if leaf.exists():
            raise AssertionError("not leaf.exists()")

    @needs_delegation
    def test_absent_knobs_stay_at_kernel_defaults(self, base: str) -> None:
        """Заданный вес пишется, незаданные файлы не трогаются."""
        manager = CgroupManager(base)
        leaf = manager.acquire(GroupLimits(cpu_weight=42))
        try:
            if self._knob(leaf, "cpu.weight") != "42":
                raise AssertionError('self._knob(leaf, "cpu.weight") == "42"')
            if self._knob(leaf, "cpu.max").split()[0] != "max":
                raise AssertionError('self._knob(leaf, "cpu.max").split()[0] == "max"')
            # memory-контроллер не запрошен — даже не включался в базе
            if (leaf / "memory.max").exists():
                raise AssertionError('not (leaf / "memory.max").exists()')
        finally:
            manager.release(leaf)

    @needs_delegation
    def test_migration_and_kill(self, base: str) -> None:
        """Ребёнок входит в leaf; release добивает его через cgroup.kill."""
        manager = CgroupManager(base)
        leaf = manager.acquire(GroupLimits(cpu_weight=100))
        procs = leaf / "cgroup.procs"

        def enter() -> None:
            fd = os.open(procs, os.O_WRONLY)
            os.write(fd, b"0")
            os.close(fd)

        try:
            proc = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(60)"],
                preexec_fn=enter,  # noqa: PLW1509
            )
        except subprocess.SubprocessError:
            manager.release(leaf)
            pytest.skip("pytest запущен вне делегированного scope: миграция запрещена")
        try:
            with open(procs) as f:
                if str(proc.pid) not in f.read().split():
                    raise AssertionError("str(proc.pid) in f.read().split()")
        finally:
            manager.release(leaf)
            proc.wait(timeout=10)
        if proc.returncode == 0:
            raise AssertionError("cgroup.kill должен был убить ребёнка")
        if leaf.exists():
            raise AssertionError("not leaf.exists()")


class TestOomScoreAdj:
    """oom_score_adj едет с rlimit'ами и применяется к чужому процессу."""

    def test_apply_to_process_raises_score(self) -> None:
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
        )
        try:
            ResourceLimits(oom_score_adj=700).apply_to_process(proc.pid)
            with open(f"/proc/{proc.pid}/oom_score_adj") as f:
                if f.read().strip() != "700":
                    raise AssertionError('f.read().strip() == "700"')
        finally:
            proc.kill()
            proc.wait()

    def test_zero_means_untouched(self) -> None:
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
        )
        try:
            with open(f"/proc/{proc.pid}/oom_score_adj") as f:
                before = f.read().strip()
            ResourceLimits().apply_to_process(proc.pid)
            with open(f"/proc/{proc.pid}/oom_score_adj") as f:
                after = f.read().strip()
            if before != after:
                raise AssertionError("before == after")
        finally:
            proc.kill()
            proc.wait()


class TestProbe:
    """Probe называет профиль и параметр, чтобы конфиг можно было поправить."""

    def test_skips_profiles_without_group_limits(self) -> None:
        profile = TestProfileValidation._profile()
        CgroupManager.probe_profiles({"default": profile})

    def test_names_profile_on_failure(self) -> None:
        profile = TestProfileValidation._profile(
            cgroup_base="/sys/fs/cgroup/nonexistent/forbidden",
            group_cpu_percent=100,
        )
        CgroupManager._prepared.pop(Path("/sys/fs/cgroup/nonexistent/forbidden"), None)
        with pytest.raises(CgroupError, match="sandbox profile 'broken'"):
            CgroupManager.probe_profiles({"broken": profile})
