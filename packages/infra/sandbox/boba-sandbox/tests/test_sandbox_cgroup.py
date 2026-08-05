"""Групповые лимиты: cgroup v2 leaf на запуск; тесты идут в зоне user@<uid>.service.
Миграция в leaf работает, только если pytest запущен внутри зоны (systemd-run)."""

from __future__ import annotations

import contextlib
import os
import subprocess
import sys
from uuid import uuid4

import pytest

from boba.sandbox.cgroup import CgroupError, CgroupManager, GroupLimits
from boba.sandbox.profile import SandboxProfile
from boba.workspace.launcher import ResourceLimits


@pytest.fixture(autouse=True)
def chainlit_context() -> None:
    pass


class TestGroupLimits:
    """Чистая логика: контроллеры, формат cpu.max, признак «запрошено»."""

    def test_not_requested_when_absent(self) -> None:
        assert GroupLimits().requested is False

    def test_requested_by_any_field(self) -> None:
        assert GroupLimits(memory_bytes=1).requested is True
        assert GroupLimits(cpu_percent=100).requested is True
        assert GroupLimits(cpu_weight=100).requested is True
        assert GroupLimits(pids_max=10).requested is True
        assert GroupLimits(swap_max_bytes=0).requested is True
        assert GroupLimits(oom_kill_all=False).requested is True

    def test_controllers_follow_fields(self) -> None:
        assert GroupLimits().controllers == ()
        assert GroupLimits(memory_bytes=1).controllers == ("memory",)
        assert GroupLimits(swap_max_bytes=0).controllers == ("memory",)
        assert GroupLimits(oom_kill_all=True).controllers == ("memory",)
        assert GroupLimits(cpu_weight=50).controllers == ("cpu",)
        assert GroupLimits(pids_max=5).controllers == ("pids",)
        every = GroupLimits(memory_bytes=1, cpu_percent=100, pids_max=5)
        assert every.controllers == ("cpu", "memory", "pids")

    def test_cpu_max_is_quota_over_period(self) -> None:
        assert GroupLimits(cpu_percent=100).cpu_max == "100000 100000"
        assert GroupLimits(cpu_percent=150).cpu_max == "150000 100000"
        assert GroupLimits(cpu_percent=50).cpu_max == "50000 100000"

    def test_cpu_max_undefined_without_percent(self) -> None:
        with pytest.raises(ValueError, match="cpu_percent is not set"):
            _ = GroupLimits(cpu_weight=100).cpu_max


class TestProfileValidation:
    """cgroup_*-лимиты без cgroup_base — ошибка конфига, не сюрприз в рантайме."""

    @staticmethod
    def _profile(**overrides: object) -> SandboxProfile:
        fields: dict[str, object] = {
            "rootfs": "",
            "ro_binds": [],
            "rw_binds": [],
            "rw_images": [],
            "image_template": "",
            "launcher": {
                "mount_wait_sec": 1.0,
                "mount_poll_sec": 0.05,
                "shutdown_wait_sec": 1.0,
                "copy_chunk_bytes": 1048576,
            },
            "tmpfs": [],
            "network": False,
            "env_set": {},
            "timeout_sec": 5,
            "max_memory_bytes": 1,
            "max_cpu_sec": 1,
            "max_file_size_bytes": 1,
            "max_open_files": 1,
            "max_processes": 1,
            "max_output_bytes": 1024,
            "cgroup_base": "",
            "oom_score_adj": 0,
            "cwd": "",
        }
        fields.update(overrides)
        return SandboxProfile.model_validate(fields)

    def test_group_limits_require_cgroup_base(self) -> None:
        with pytest.raises(ValueError, match="cgroup_base is empty"):
            self._profile(cgroup_cpu_percent=100)

    def test_any_single_group_field_requires_base(self) -> None:
        with pytest.raises(ValueError, match="cgroup_base is empty"):
            self._profile(cgroup_oom_kill_all=True)

    def test_cgroup_base_must_be_absolute(self) -> None:
        with pytest.raises(ValueError, match="must be absolute"):
            self._profile(cgroup_base="relative/path", cgroup_cpu_weight=10)

    def test_absent_group_limits_allow_empty_base(self) -> None:
        profile = self._profile()
        assert profile.cgroup_memory_bytes is None
        assert profile.cgroup_oom_kill_all is None

    def test_zero_is_not_a_switch_anymore(self) -> None:
        """«Выключено» выражается отсутствием параметра, а не нулём."""
        with pytest.raises(ValueError, match="greater than 0"):
            self._profile(cgroup_base="/sys/fs/cgroup/x", cgroup_cpu_percent=0)


_UID = os.getuid()
_DELEGATED_PARENT = (
    f"/sys/fs/cgroup/user.slice/user-{_UID}.slice/user@{_UID}.service"
)

needs_delegation = pytest.mark.skipif(
    not os.path.isdir(_DELEGATED_PARENT),
    reason="нет делегированной systemd user-зоны (cgroup v2)",
)


@pytest.fixture
def base():
    path = os.path.join(_DELEGATED_PARENT, f"boba-pytest-{uuid4().hex[:8]}")
    yield path
    CgroupManager._prepared.pop(path, None)
    with contextlib.suppress(OSError):
        os.rmdir(path)


class TestCgroupManager:
    """Живой cgroup: leaf создаётся, лимиты записываются, уборка полная."""

    @staticmethod
    def _knob(leaf: str, name: str) -> str:
        with open(os.path.join(leaf, name)) as f:
            return f.read().strip()

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
            assert self._knob(leaf, "memory.max") == str(limits.memory_bytes)
            assert self._knob(leaf, "cpu.max").split() == ["50000", "100000"]
            assert self._knob(leaf, "cpu.weight") == "25"
            assert self._knob(leaf, "pids.max") == "17"
            assert self._knob(leaf, "memory.swap.max") == "0"
            assert self._knob(leaf, "memory.oom.group") == "1"
        finally:
            manager.release(leaf)
        assert not os.path.exists(leaf)

    @needs_delegation
    def test_absent_knobs_stay_at_kernel_defaults(self, base: str) -> None:
        """Заданный вес пишется, незаданные файлы не трогаются."""
        manager = CgroupManager(base)
        leaf = manager.acquire(GroupLimits(cpu_weight=42))
        try:
            assert self._knob(leaf, "cpu.weight") == "42"
            assert self._knob(leaf, "cpu.max").split()[0] == "max"
            # memory-контроллер не запрошен — даже не включался в базе
            assert not os.path.exists(os.path.join(leaf, "memory.max"))
        finally:
            manager.release(leaf)

    @needs_delegation
    def test_migration_and_kill(self, base: str) -> None:
        """Ребёнок входит в leaf; release добивает его через cgroup.kill."""
        manager = CgroupManager(base)
        leaf = manager.acquire(GroupLimits(cpu_weight=100))
        procs = os.path.join(leaf, "cgroup.procs")

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
                assert str(proc.pid) in f.read().split()
        finally:
            manager.release(leaf)
            proc.wait(timeout=10)
        assert proc.returncode != 0, "cgroup.kill должен был убить ребёнка"
        assert not os.path.exists(leaf)


class TestOomScoreAdj:
    """oom_score_adj едет с rlimit'ами и применяется к чужому процессу."""

    def test_apply_to_process_raises_score(self) -> None:
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
        )
        try:
            ResourceLimits(oom_score_adj=700).apply_to_process(proc.pid)
            with open(f"/proc/{proc.pid}/oom_score_adj") as f:
                assert f.read().strip() == "700"
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
            assert before == after
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
            cgroup_cpu_percent=100,
        )
        CgroupManager._prepared.pop("/sys/fs/cgroup/nonexistent/forbidden", None)
        with pytest.raises(CgroupError, match="sandbox profile 'broken'"):
            CgroupManager.probe_profiles({"broken": profile})
