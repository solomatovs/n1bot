"""ZygoteToolCaller: контракт ToolLauncher поверх зиготы.

Целевые тесты этапа 2: мост конверта, спавнер из профиля, cgroup-leaf,
журнал каналов и контракт ошибок LauncherError.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any, ClassVar

import pytest
from fake_channel_tool import ChannelConfig, fx_echo, fx_probe_tmp, fx_warm_state
from pydantic import SecretStr

from boba.sandbox import SandboxProfile
from boba.sandbox.zygote import (
    ZygotePolicy,
    ZygoteRegistry,
    ZygoteSpawner,
    ZygoteState,
    ZygoteSupervisor,
    ZygoteToolCaller,
)
from boba.toolkit.channels import JournalChannel, ToolChannel
from boba.toolkit.entry import ToolAddress, ToolArgv, ToolMain
from boba.toolkit.launcher import LauncherError
from boba.toolkit.protocol import ReplyError, ReplyOk
from boba.toolkit.stream import StreamSink, ToolChannelsTap

REPO = Path(__file__).resolve().parents[5]
SANDBOX = REPO / "build" / "src" / "sandbox"
ROOTFS = SANDBOX / "rootfs"

needs_sandbox = pytest.mark.skipif(
    shutil.which("bwrap") is None or not (ROOTFS / "bin" / "sh").exists(),
    reason="нет bwrap или артефактов песочницы (собрать: make deps)",
)
needs_userns = pytest.mark.skipif(
    os.geteuid() == 0, reason="под root user namespace ведёт себя иначе"
)

pytestmark = [needs_sandbox, needs_userns]

CFG = ChannelConfig(token=SecretStr("zc-s3cret"))

FX_ECHO = ToolMain.toolset(fx_echo)[0]
FX_PROBE = ToolMain.toolset(fx_probe_tmp)[0]

FAST = ZygotePolicy(
    start_timeout_sec=20.0,
    max_start_attempts=2,
    restart_backoff_sec=0.05,
    healthy_after_sec=0.5,
)

SRC_PACKAGES = (
    "core/boba-cancellation",
    "core/boba-toolkit",
)


def _bin_dirs() -> list[str]:
    dirs: list[str] = []
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        if entry.startswith("/"):
            dirs.append(entry)

    return dirs


def _python_path() -> str:
    parts = ["/usr/src/infra/sandbox/boba-sandbox/tests"]
    for name in SRC_PACKAGES:
        parts.append(f"/usr/src/{name}/src")

    return ":".join(parts)


def _profile(**overrides: Any) -> SandboxProfile:
    site_packages = "/usr/local/lib/python3.11/site-packages"
    raw: dict[str, Any] = {
        "rootfs": str(ROOTFS),
        "rootfs_image": "",
        "ro_binds": (
            f"{SANDBOX / 'third' / 'python'}:/usr/local",
            f"{SANDBOX / 'site'}:{site_packages}",
            f"{REPO / 'packages'}:/usr/src",
        ),
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
        "tmpfs": ("/tmp:64M",),  # noqa: S108
        "network": False,
        "env_set": {
            "PYTHONPATH": _python_path(),
            "HOME": "/tmp",  # noqa: S108
            "LANG": "C.UTF-8",
        },
        "timeout_sec": 60,
        "max_memory_bytes": 2 * 1024 * 1024 * 1024,
        "max_cpu_sec": 60,
        "max_file_size_bytes": 64 * 1024 * 1024,
        "max_open_files": 1024,
        "max_processes": 256,
        "cgroup_base": "",
        "oom_score_adj": 0,
        "cwd": "/tmp",  # noqa: S108
    }
    raw.update(overrides)
    return SandboxProfile.model_validate(raw)


class ChannelRecorder:
    """StreamSink-приёмник канала в тестах."""

    def __init__(self) -> None:
        self.data = bytearray()

    def feed(self, data: bytes) -> None:
        self.data.extend(data)

    def feed_text(self, text: str) -> None:
        self.feed(text.encode("utf-8"))

    def text(self) -> str:
        return bytes(self.data).decode("utf-8")


class RecordingSinks:
    """ChannelSinks: приёмник на канал, как их отдаёт журнал вызова."""

    def __init__(self) -> None:
        self.channels: dict[JournalChannel, ChannelRecorder] = {}

    def sink_of(self, channel: JournalChannel) -> StreamSink:
        return self.channels.setdefault(channel, ChannelRecorder())

    def text_of(self, channel: JournalChannel) -> str:
        recorder = self.channels.get(channel)
        if recorder is None:
            return ""

        return recorder.text()


def _command(text: str) -> Any:
    """ToolCommand для fx_echo — ровно как его строит обёртка запуска."""
    address = ToolAddress(module="fake_channel_tool", name="fx_echo")
    schema = ToolArgv.schema_of(FX_ECHO)
    return ToolArgv.render(address, schema, {"text": text, "cfg": CFG})


@pytest.fixture
def zygote() -> Any:
    born: list[ZygoteSupervisor] = []

    def make(profile: SandboxProfile) -> ZygoteToolCaller:
        spawner = ZygoteSpawner(profile, ["fake_channel_tool"])
        supervisor = ZygoteSupervisor("fx", spawner.spawn, FAST)
        supervisor.start()
        born.append(supervisor)
        return ZygoteToolCaller("fx", supervisor, profile)

    yield make

    for supervisor in born:
        supervisor.stop()


class TestRunTool:
    def test_ok_envelope_with_secret_from_stdin(self, zygote: Any) -> None:
        caller = zygote(_profile())

        outcome = caller.run_tool(_command("ping"))

        if not isinstance(outcome.reply, ReplyOk):
            raise AssertionError(f"reply={outcome.reply}")

        artifact = outcome.reply.artifact
        if "ping|zc-s3cret" not in artifact.model_dump_json():
            raise AssertionError("секрет из stdin-конфига не дошёл до тела")

        if outcome.run.exit_code != 0:
            raise AssertionError(f"rc={outcome.run.exit_code}")

    def test_expected_failure_travels_as_reply_error(self, zygote: Any) -> None:
        caller = zygote(_profile())

        outcome = caller.run_tool(_command("boom"))

        if not isinstance(outcome.reply, ReplyError):
            raise AssertionError(f"reply={outcome.reply}")

        if outcome.reply.kind != "fx_down":
            raise AssertionError(f"kind={outcome.reply.kind}")

    def test_unavailable_zygote_is_launcher_error(self, zygote: Any) -> None:
        """Мёртвая зигота — LauncherError: фронт ловит её по контракту слоя."""
        caller = zygote(_profile())
        caller_supervisor = caller.supervisor
        caller_supervisor.stop()

        with pytest.raises(LauncherError):
            caller.run_tool(_command("late"))

    def test_timeout_without_envelope_is_launcher_error(self, zygote: Any) -> None:
        caller = zygote(_profile(timeout_sec=2))

        with pytest.raises(LauncherError, match="no envelope"):
            caller.run_tool(_command("sleepy"))

    def test_journal_sinks_receive_channels(self, zygote: Any) -> None:
        caller = zygote(_profile())

        sinks = RecordingSinks()
        ToolChannelsTap.set(sinks)
        try:
            outcome = caller.run_tool(_command("journal"))
        finally:
            ToolChannelsTap.set(None)

        if not isinstance(outcome.reply, ReplyOk):
            raise AssertionError(f"reply={outcome.reply}")

        stdout = sinks.text_of(ToolChannel.STDOUT)
        if "noise on stdout" not in stdout:
            raise AssertionError(f"tool_stdout={stdout!r}")

        result = sinks.text_of(ToolChannel.RESULT)
        if '"status":"ok"' not in result:
            raise AssertionError(f"tool_result={result!r}")

    def test_isolated_children_and_userns_denied(self, zygote: Any) -> None:
        """Дети изолированы, вложенные userns закрыты (max_user_namespaces=0)."""
        caller = zygote(_profile())

        address = ToolAddress(module="fake_channel_tool", name="fx_probe_tmp")
        schema = ToolArgv.schema_of(FX_PROBE)
        command = ToolArgv.render(address, schema, {"marker": "solo"})

        outcome = caller.run_tool(command)

        if not isinstance(outcome.reply, ReplyOk):
            raise AssertionError(f"reply={outcome.reply}")

        import json

        state = json.loads(outcome.reply.content)
        if state["markers"] != ["solo"]:
            raise AssertionError(f"чужие файлы в /tmp: {state}")

        if state["pid"] != 1:
            raise AssertionError(f"исполнитель не в своём pid ns: {state}")

        if state["userns_max"] != "0":
            raise AssertionError(f"вложенные userns не закрыты: {state}")


class TestSpawner:
    def test_rootfs_image_is_refused(self) -> None:
        """Спавнер требует премонтированный корень: образ — ошибка сборки."""
        profile = _profile(rootfs="", rootfs_image="/srv/rootfs.ext4")

        with pytest.raises(LauncherError, match="premounted"):
            ZygoteSpawner(profile, ["fake_channel_tool"])

    def test_missing_tmpfs_is_refused(self) -> None:
        """Приватный /tmp ребёнка требует tmpfs-размера в профиле."""
        profile = _profile(tmpfs=())

        with pytest.raises(LauncherError, match="tmpfs"):
            ZygoteSpawner(profile, ["fake_channel_tool"])


class CgroupZone:
    """Делегированная cgroup v2 зона стенда: первая доступная на запись."""

    _UID: ClassVar[int] = os.getuid()

    CANDIDATES: ClassVar[tuple[str, ...]] = (
        f"/sys/fs/cgroup/boba.slice/user-{_UID}.slice/user@{_UID}.service",
        "/sys/fs/cgroup/boba.slice/boba-sandbox",
    )

    @classmethod
    def find(cls) -> str:
        configured = os.environ.get("BOBA_CGROUP_BASE", "")
        candidates = (configured, *cls.CANDIDATES)

        for path in candidates:
            if not path:
                continue

            if os.path.isdir(path) and os.access(path, os.W_OK):
                return path

        return ""


needs_delegation = pytest.mark.skipif(
    not CgroupZone.find(),
    reason="нет делегированной cgroup v2 зоны",
)


@needs_delegation
class TestCgroup:
    def test_leaf_created_and_released(self, zygote: Any) -> None:
        base = os.path.join(CgroupZone.find(), "zygote-test")
        caller = zygote(
            _profile(
                cgroup_base=base,
                cgroup_memory_bytes=512 * 1024 * 1024,
                cgroup_pids_max=64,
            )
        )

        outcome = caller.run_tool(_command("grouped"))

        if not isinstance(outcome.reply, ReplyOk):
            raise AssertionError(f"reply={outcome.reply}")

        leftovers = []
        if os.path.isdir(base):
            leftovers = [d for d in os.listdir(base) if d.startswith("run-")]

        if leftovers:
            raise AssertionError(f"leaf'ы не освобождены: {leftovers}")


class TestRegistry:
    """Реестр супервизоров: один живой процесс на секцию, гашение на shutdown."""

    def teardown_method(self) -> None:
        ZygoteRegistry.stop_all()

    def test_obtain_reuses_running_supervisor(self) -> None:
        profile = _profile()

        first = ZygoteRegistry.obtain(
            "fx-reg", profile, ["fake_channel_tool"], FAST
        )
        second = ZygoteRegistry.obtain(
            "fx-reg", profile, ["fake_channel_tool"], FAST
        )

        if first is not second:
            raise AssertionError("повторный obtain должен вернуть тот же супервизор")

        if first.state is not ZygoteState.READY:
            raise AssertionError(f"state={first.state}")

    def test_stop_all_stops_and_next_obtain_restarts(self) -> None:
        profile = _profile()

        first = ZygoteRegistry.obtain(
            "fx-reg", profile, ["fake_channel_tool"], FAST
        )
        ZygoteRegistry.stop_all()

        if first.state is not ZygoteState.STOPPED:
            raise AssertionError(f"state={first.state}")

        second = ZygoteRegistry.obtain(
            "fx-reg", profile, ["fake_channel_tool"], FAST
        )

        if second is first:
            raise AssertionError("после stop_all нужен новый супервизор")

        if second.state is not ZygoteState.READY:
            raise AssertionError(f"state={second.state}")


class TestWarmup:
    """WARMUP модуля: исполняется в зиготе до ready, дети видят результат."""

    def teardown_method(self) -> None:
        ZygoteRegistry.stop_all()

    def _call_warm_state(self, caller: ZygoteToolCaller) -> str:
        address = ToolAddress(module="fake_channel_tool", name="fx_warm_state")
        schema = ToolArgv.schema_of(
            next(t for t in ToolMain.toolset(fx_warm_state) if t)
        )
        outcome = caller.run_tool(ToolArgv.render(address, schema, {}))

        if not isinstance(outcome.reply, ReplyOk):
            raise AssertionError(f"reply={outcome.reply}")

        return outcome.reply.content

    def test_warmup_runs_before_ready_and_children_inherit(self) -> None:
        profile = _profile()
        supervisor = ZygoteRegistry.obtain(
            "fx-warm",
            profile,
            ["fake_channel_tool"],
            FAST,
            warmup_configs={"fake_channel_tool": {"greeting": "privet"}},
        )
        caller = ZygoteToolCaller("fx-warm", supervisor, profile)

        state = self._call_warm_state(caller)
        if state != "warmed:privet":
            raise AssertionError(f"кэш прогрева не унаследован: {state!r}")

    def test_missing_config_skips_the_hook(self) -> None:
        profile = _profile()
        supervisor = ZygoteRegistry.obtain(
            "fx-plain", profile, ["fake_channel_tool"], FAST
        )
        caller = ZygoteToolCaller("fx-plain", supervisor, profile)

        state = self._call_warm_state(caller)
        if state != "":
            raise AssertionError(f"без конфига прогрев пропускается: {state!r}")
