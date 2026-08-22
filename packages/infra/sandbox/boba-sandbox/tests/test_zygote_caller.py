"""ZygoteToolCaller: контракт ToolLauncher поверх зиготы.

Целевые тесты этапа 2: мост конверта, спавнер из профиля, cgroup-leaf,
журнал каналов и контракт ошибок LauncherError.
"""

from __future__ import annotations

import json
import os
import resource
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, ClassVar

import pytest
from fake_channel_tool import ChannelConfig, fx_echo, fx_probe_tmp, fx_warm_state
from pydantic import SecretStr
from zygote_stand import ProfileFields, SandboxStand

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
from boba.toolkit.stream import (
    ChannelSinks,
    Chunk,
    StreamSink,
    ToolChannelsTap,
)
from boba.toolkit.zygote import WarmupCall

REPO = Path(__file__).resolve().parents[5]
SANDBOX = REPO / "build" / "src" / "sandbox"
ROOTFS = SANDBOX / "rootfs"
ROOTFS_IMAGE = SANDBOX / "rootfs.ext4"

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
    stop_wait_sec=5.0,
    call_poll_sec=0.05,
)

SLOW_START = ZygotePolicy(
    start_timeout_sec=60.0,
    max_start_attempts=1,
    restart_backoff_sec=0.05,
    healthy_after_sec=0.5,
    stop_wait_sec=5.0,
    call_poll_sec=0.05,
)
"""Корень образом: старт включает fuse2fs-монтирование rootfs.ext4."""

IMAGE_MOUNTS = {
    "template": "/mnt/workspace.ext4",
    "fuse2fs": "/mnt/fuse2fs",
    "images": "/mnt/images",
}
"""Точки образной обвязки в тестовом профиле, как их объявляет конфиг стенда."""

WARMUP_CALLS = (
    WarmupCall(
        module="fake_channel_tool", hook="warm_cache", config={"greeting": "privet"}
    ),
)
"""Конфиг прогрева фейкового модуля: без него зигота не стартует."""


def _bin_dirs() -> list[str]:
    return SandboxStand.bin_dirs()


def _python_path() -> str:
    return SandboxStand.python_path("/usr/src/infra/sandbox/boba-sandbox/tests")


def _profile(**overrides: Any) -> SandboxProfile:
    site_packages = "/usr/local/lib/python3.11/site-packages"
    raw: dict[str, Any] = {
        "host": {
            "mounting": {
                "mount_wait_sec": 10.0,
                "mount_poll_sec": 0.05,
                "shutdown_wait_sec": 5.0,
                "lock_wait_sec": 10.0,
                "copy_chunk_bytes": 1 << 20,
            },
            "binaries": {"dirs": _bin_dirs()},
            "stderr_tail_bytes": 4096,
            "fail_tail_chars": 2000,
            "kill_grace_sec": 5,
            "cgroup_base": "",
        },
        "rootfs": {
            "dir": str(ROOTFS),
            "image": "",
        },
        "mounts": {
            "ro": (
                f"{SANDBOX / 'third' / 'python'}:/usr/local",
                f"{SANDBOX / 'site'}:{site_packages}",
                f"{REPO / 'packages'}:/usr/src",
            ),
            "rw": (),
            "images": (),
            "image_template": "",
            "tmpfs": ("/tmp:64M",),  # noqa: S108
            "proc": "/proc",
            "dev": "/dev",
            "call_tmpfs": "/tmp",  # noqa: S108
            "setup_ro": (),
            "setup_rw": (),
        },
        "isolation": {
            "network": False,
            "env": {
                "PATH": "/usr/local/bin:/usr/bin:/bin",
                "PYTHONPATH": _python_path(),
                "HOME": "/tmp",  # noqa: S108
                "LANG": "C.UTF-8",
            },
            "max_processes": 256,
            "reap_poll_sec": 0.05,
        },
        "limits": {
            "timeout_sec": 60,
            "process_memory_bytes": 2 * 1024 * 1024 * 1024,
            "process_cpu_sec": 60,
            "process_file_bytes": 64 * 1024 * 1024,
            "process_open_files": 1024,
            "process_oom_score_adj": 0,
        },
        "run": {
            "shell": "/bin/bash",
            "cwd": "/tmp",  # noqa: S108
        },
    }
    return SandboxProfile.model_validate(ProfileFields.merged(raw, overrides))


class ChannelRecorder(StreamSink):
    """StreamSink-приёмник канала в тестах."""

    def __init__(self) -> None:
        self.data = bytearray()

    def feed(self, data: Chunk) -> None:
        self.data.extend(data)

    def feed_text(self, text: str) -> None:
        self.feed(text.encode("utf-8"))

    def text(self) -> str:
        return bytes(self.data).decode("utf-8")


class RecordingSinks(ChannelSinks):
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
        spawner = ZygoteSpawner(profile, ["fake_channel_tool"], FAST)
        supervisor = ZygoteSupervisor(
            "fx",
            spawner.spawn,
            FAST,
            stderr_tail_bytes=profile.host.stderr_tail_bytes,
            warmup_calls=WARMUP_CALLS,
            modules=["fake_channel_tool"],
            root=spawner.root_label(),
        )
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

    def test_secret_never_travels_in_argv(self) -> None:
        """Секрет конфига едет stdin'ом: в argv его нет ни на одном пути."""
        command = _command("ping")

        if "zc-s3cret" in " ".join(command.argv):
            raise AssertionError(f"секрет в argv: {command.argv}")

        if b"zc-s3cret" not in command.stdin:
            raise AssertionError("секрет не доехал stdin'ом")

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

        state = json.loads(outcome.reply.content)
        if state["markers"] != ["solo"]:
            raise AssertionError(f"чужие файлы в /tmp: {state}")

        if state["init"] != "python3" or state["pid"] > 8:
            raise AssertionError(f"тело не в своём pid ns: {state}")

        if state["userns_max"] != "0":
            raise AssertionError(f"вложенные userns не закрыты: {state}")


class TestSpawner:
    def test_rootfs_image_is_accepted(self) -> None:
        """Корень образом — штатный профиль: зигота монтирует его сама."""
        profile = _profile(
            rootfs={"dir": "", "image": str(ROOTFS_IMAGE), "mount": "/tmp/boba-rootfs"},  # noqa: S108
        )

        spawner = ZygoteSpawner(profile, ["fake_channel_tool"], FAST)

        if not spawner.mounts_rootfs:
            raise AssertionError("профиль с образом корня требует цепочки лаунчера")

    def test_missing_tmpfs_is_refused(self) -> None:
        """Приватный /tmp ребёнка требует tmpfs-размера в профиле."""
        profile = _profile(tmpfs=())

        with pytest.raises(LauncherError, match="tmpfs"):
            ZygoteSpawner(profile, ["fake_channel_tool"], FAST)


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
                group_memory_bytes=512 * 1024 * 1024,
                group_pids_max=64,
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
            "fx-reg", profile, ["fake_channel_tool"], FAST, warmup_calls=WARMUP_CALLS
        )
        second = ZygoteRegistry.obtain(
            "fx-reg", profile, ["fake_channel_tool"], FAST, warmup_calls=WARMUP_CALLS
        )

        if first is not second:
            raise AssertionError("повторный obtain должен вернуть тот же супервизор")

        if first.state is not ZygoteState.READY:
            raise AssertionError(f"state={first.state}")

    def test_stop_all_stops_and_next_obtain_restarts(self) -> None:
        profile = _profile()

        first = ZygoteRegistry.obtain(
            "fx-reg", profile, ["fake_channel_tool"], FAST, warmup_calls=WARMUP_CALLS
        )
        ZygoteRegistry.stop_all()

        if first.state is not ZygoteState.STOPPED:
            raise AssertionError(f"state={first.state}")

        second = ZygoteRegistry.obtain(
            "fx-reg", profile, ["fake_channel_tool"], FAST, warmup_calls=WARMUP_CALLS
        )

        if second is first:
            raise AssertionError("после stop_all нужен новый супервизор")

        if second.state is not ZygoteState.READY:
            raise AssertionError(f"state={second.state}")


class TestWarmup:
    """Прогрев модуля: исполняется в зиготе до ready, дети видят результат."""

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
            warmup_calls=WARMUP_CALLS,
        )
        caller = ZygoteToolCaller("fx-warm", supervisor, profile)

        state = self._call_warm_state(caller)
        if state != "warmed:privet":
            raise AssertionError(f"кэш прогрева не унаследован: {state!r}")

    def test_missing_config_fails_the_start(self) -> None:
        """Молчаливой деградации нет: без конфига хука зигота не поднимается."""
        profile = _profile()

        with pytest.raises(LauncherError, match="not ready"):
            ZygoteRegistry.obtain("fx-plain", profile, ["fake_channel_tool"], FAST)


def _mkfs_template(tmp_path: Path) -> str:
    """Шаблон workspace-образа: пустой ext4 на 8 МБ."""
    mkfs = shutil.which("mkfs.ext4")
    if mkfs is None:
        pytest.skip("mkfs.ext4 недоступен")

    template = tmp_path / "workspace.ext4"
    subprocess.run(  # noqa: S603
        [mkfs, "-q", "-F", str(template), "8m"], check=True, capture_output=True
    )
    return str(template)


def _image_profile(tmp_path: Path, **overrides: Any) -> SandboxProfile:
    """Профиль с rw-образом: шаблон, fuse2fs и каталог образов — явные бинды.

    Точки под tmpfs /mnt: на read-only корне bwrap точку не создаст.
    """
    template = _mkfs_template(tmp_path)
    images = tmp_path / "ws"
    images.mkdir(exist_ok=True)

    fuse2fs = SandboxStand.fuse2fs()

    base = _profile()
    ro_binds: list[str] = []
    for spec in base.mounts.ro:
        ro_binds.append(f"{spec.host}:{spec.target}")

    ro_binds.append(f"{template}:{IMAGE_MOUNTS['template']}")
    ro_binds.append(f"{fuse2fs}:{IMAGE_MOUNTS['fuse2fs']}")

    raw: dict[str, Any] = {
        "ro": tuple(ro_binds),
        "rw": (f"{images}:{IMAGE_MOUNTS['images']}",),
        "images": (),
        "workspace": {
            "template": template,
            "images": str(images),
            "mount": "/workspace",
        },
        "image_template": template,
        "tmpfs": ("/tmp:64M", "/mnt:1M"),  # noqa: S108
        "cwd": "/workspace",
    }
    raw.update(overrides)

    return _profile(**raw)


needs_mkfs = pytest.mark.skipif(
    shutil.which("mkfs.ext4") is None or shutil.which("fuse2fs") is None,
    reason="нет mkfs.ext4/fuse2fs для образа workspace",
)


@needs_mkfs
class TestWorkspaceImages:
    """rw-образ монтирует ребёнок в своём namespace: зигота одна на всех."""

    def teardown_method(self) -> None:
        ZygoteRegistry.stop_all()

    def _caller(self, tmp_path: Path, user_id: str) -> ZygoteToolCaller:
        profile = _image_profile(tmp_path)
        supervisor = ZygoteRegistry.obtain(
            "fx-ws", profile, ["fake_channel_tool"], FAST, warmup_calls=WARMUP_CALLS
        )
        return ZygoteToolCaller(
            "fx-ws", supervisor, profile, lambda: {"user_id": user_id}
        )

    def _workspace_listing(self, caller: ZygoteToolCaller) -> str:
        outcome = caller.run_tool(_command("workspace"))
        if not isinstance(outcome.reply, ReplyOk):
            raise AssertionError(f"reply={outcome.reply}")

        return outcome.reply.artifact.model_dump_json()

    def test_image_created_from_template_and_persists(self, tmp_path: Path) -> None:
        caller = self._caller(tmp_path, "7")

        first = self._workspace_listing(caller)
        if "fx-probe.txt" not in first:
            raise AssertionError(f"запись в образ не видна: {first}")

        outcome = caller.run_tool(_command("workspace"))
        if "sandbox-mount" in outcome.run.stderr:
            raise AssertionError(f"кадры обвязки в tool_stderr: {outcome.run.stderr!r}")

        image = tmp_path / "ws" / "7.ext4"
        if not image.exists():
            raise AssertionError("образ пользователя не создан из шаблона")

        # второй вызов — новый ребёнок и новое монтирование: запись пережила
        # размонтирование, значит fuse2fs был погашен штатно
        second = self._workspace_listing(caller)
        if "fx-probe.txt" not in second:
            raise AssertionError(f"запись не пережила размонтирование: {second}")

    def test_users_get_separate_images(self, tmp_path: Path) -> None:
        seven = self._caller(tmp_path, "7")
        eight = self._caller(tmp_path, "8")

        self._workspace_listing(seven)
        listing_eight = self._workspace_listing(eight)

        images = sorted(p.name for p in (tmp_path / "ws").glob("*.ext4"))
        if images != ["7.ext4", "8.ext4"]:
            raise AssertionError(f"образы по пользователям: {images}")

        if "workspace:fx-probe.txt" not in listing_eight:
            raise AssertionError(f"второй пользователь видит чужое: {listing_eight}")

    def test_parallel_calls_on_one_image_are_serialized(self, tmp_path: Path) -> None:
        caller = self._caller(tmp_path, "7")

        with ThreadPoolExecutor(3) as pool:
            listings = list(
                pool.map(lambda _: self._workspace_listing(caller), range(3))
            )

        for listing in listings:
            if "fx-probe.txt" not in listing:
                raise AssertionError(f"параллельный вызов сорвался: {listing}")


needs_rootfs_image = pytest.mark.skipif(
    not ROOTFS_IMAGE.exists() or shutil.which("fuse2fs") is None,
    reason="нет rootfs.ext4 или fuse2fs (собрать: make sandbox-image)",
)


@needs_mkfs
@needs_rootfs_image
class TestImageRootfs:
    """Корень образом: зигота монтирует rootfs.ext4 сама, хост чист."""

    def teardown_method(self) -> None:
        ZygoteRegistry.stop_all()

    def _caller(self, name: str, tmp_path: Path) -> ZygoteToolCaller:
        profile = _profile(
            rootfs={"dir": "", "image": str(ROOTFS_IMAGE), "mount": "/tmp/boba-rootfs"},  # noqa: S108
        )
        return self._on_profile(name, profile)

    def _image_caller(self, name: str, tmp_path: Path) -> ZygoteToolCaller:
        profile = _image_profile(
            tmp_path,
            rootfs={"dir": "", "image": str(ROOTFS_IMAGE), "mount": "/tmp/boba-rootfs"},  # noqa: S108
        )
        return self._on_profile(name, profile)

    def _on_profile(self, name: str, profile: SandboxProfile) -> ZygoteToolCaller:
        supervisor = ZygoteRegistry.obtain(
            name, profile, ["fake_channel_tool"], SLOW_START, warmup_calls=WARMUP_CALLS
        )
        return ZygoteToolCaller(name, supervisor, profile, lambda: {"user_id": "7"})

    def _mounts_of_host(self) -> set[str]:
        targets: set[str] = set()
        with open("/proc/self/mountinfo") as mountinfo:
            for line in mountinfo:
                targets.add(line.split()[4])

        return targets

    def test_zygote_serves_calls_from_the_image_root(self, tmp_path: Path) -> None:
        before = self._mounts_of_host()
        caller = self._caller("fx-img", tmp_path)

        outcome = caller.run_tool(_command("ping"))

        if not isinstance(outcome.reply, ReplyOk):
            raise AssertionError(f"reply={outcome.reply}")

        if "ping|zc-s3cret" not in outcome.reply.artifact.model_dump_json():
            raise AssertionError("тело не отработало на корне из образа")

        if self._mounts_of_host() != before:
            raise AssertionError("зигота смонтировала корень на хосте")

    def test_children_stay_isolated_on_the_image_root(self, tmp_path: Path) -> None:
        caller = self._caller("fx-img-iso", tmp_path)

        address = ToolAddress(module="fake_channel_tool", name="fx_probe_tmp")
        schema = ToolArgv.schema_of(FX_PROBE)
        command = ToolArgv.render(address, schema, {"marker": "img.txt"})

        outcome = caller.run_tool(command)
        if not isinstance(outcome.reply, ReplyOk):
            raise AssertionError(f"reply={outcome.reply}")

        state = json.loads(outcome.reply.content)

        if state["markers"] != ["img.txt"]:
            raise AssertionError(f"частный /tmp вызова: {state}")

        if state["cap_eff"] != "0000000000000000":
            raise AssertionError(f"capabilities не сброшены: {state}")

        if state["userns_max"] != "0":
            raise AssertionError(f"вложенные userns не закрыты: {state}")

    def test_workspace_image_works_on_the_image_root(self, tmp_path: Path) -> None:
        caller = self._image_caller("fx-img-ws", tmp_path)

        first = caller.call_text("echo hello > note.txt; pwd", stdin="")
        if first.result.exit_code != 0:
            raise AssertionError(f"rc={first.result.exit_code}: {first.result.stderr}")

        if "/workspace" not in first.result.stdout:
            raise AssertionError(f"cwd не workspace: {first.result.stdout!r}")

        second = caller.call_text("cat /workspace/note.txt", stdin="")
        if second.result.stdout.strip() != "hello":
            raise AssertionError(f"файл не пережил вызов: {second.result.stdout!r}")


@needs_mkfs
class TestShell:
    """call_text через зиготу: bash-команда в изолированном ребёнке с образом."""

    def teardown_method(self) -> None:
        ZygoteRegistry.stop_all()

    def _caller(self, tmp_path: Path, user_id: str = "7") -> ZygoteToolCaller:
        profile = _image_profile(tmp_path, timeout_sec=5)
        supervisor = ZygoteRegistry.obtain("fx-sh", profile, (), FAST)
        return ZygoteToolCaller(
            "fx-sh", supervisor, profile, lambda: {"user_id": user_id}
        )

    def test_stdout_stderr_stdin_and_exit_code(self, tmp_path: Path) -> None:
        caller = self._caller(tmp_path)

        outcome = caller.call_text(
            "cat; echo out-line; echo err-line >&2; exit 3", stdin="from-stdin\n"
        )

        if outcome.result.exit_code != 3:
            raise AssertionError(f"rc={outcome.result.exit_code}")

        if "from-stdin" not in outcome.result.stdout:
            raise AssertionError(f"stdin не дошёл: {outcome.result.stdout!r}")

        if "out-line" not in outcome.result.stdout:
            raise AssertionError(f"stdout={outcome.result.stdout!r}")

        if "err-line" not in outcome.result.stderr:
            raise AssertionError(f"stderr={outcome.result.stderr!r}")

        # кадры монтирования — голос обвязки, не команды: их забирает релей
        if "sandbox-mount" in outcome.result.stderr:
            raise AssertionError(
                f"кадры обвязки в stderr команды: {outcome.result.stderr!r}"
            )

    def test_workspace_persists_between_commands(self, tmp_path: Path) -> None:
        caller = self._caller(tmp_path)

        first = caller.call_text("pwd; echo hello > note.txt; ls", stdin="")
        if first.result.exit_code != 0:
            raise AssertionError(f"rc={first.result.exit_code}: {first.result.stderr}")

        if "/workspace" not in first.result.stdout:
            raise AssertionError(f"cwd не workspace: {first.result.stdout!r}")

        second = caller.call_text("cat /workspace/note.txt", stdin="")
        if second.result.stdout.strip() != "hello":
            raise AssertionError(f"файл не пережил вызов: {second.result.stdout!r}")

    def test_timeout_kills_command(self, tmp_path: Path) -> None:
        caller = self._caller(tmp_path)

        outcome = caller.call_text("sleep 30", stdin="")

        if not outcome.result.timed_out:
            raise AssertionError("timed_out должен быть выставлен")

        if outcome.succeeded:
            raise AssertionError("убитая по таймауту команда не успешна")

    def test_command_runs_isolated_without_capabilities(self, tmp_path: Path) -> None:
        caller = self._caller(tmp_path)

        # bash — ребёнок исполнителя (тот ещё гасит fuse2fs после команды),
        # поэтому init своего pid ns — python-исполнитель, а bash рядом с ним
        outcome = caller.call_text(
            "echo init=$(cat /proc/1/comm); echo procs=$(ls /proc | grep -c '^[0-9]');"
            " grep CapEff /proc/self/status",
            stdin="",
        )

        stdout = outcome.result.stdout
        if "init=python3" not in stdout:
            raise AssertionError(f"init pid ns — не исполнитель: {stdout!r}")

        procs = int(stdout.split("procs=")[1].split()[0])
        if procs > 6:
            raise AssertionError(f"в pid ns видны чужие процессы: {stdout!r}")

        if "0000000000000000" not in stdout:
            raise AssertionError(f"capabilities не сброшены: {stdout!r}")


class TestProcessCap:
    """max_processes — общий потолок задач зиготы и всех её детей."""

    def teardown_method(self) -> None:
        ZygoteRegistry.stop_all()

    def _caller(self, name: str, tmp_path: Path, processes: int | None) -> Any:
        profile = _image_profile(tmp_path, timeout_sec=10, max_processes=processes)
        supervisor = ZygoteRegistry.obtain(name, profile, (), FAST)
        return ZygoteToolCaller(name, supervisor, profile, lambda: {"user_id": "7"})

    def test_limit_is_inherited_by_calls(self, tmp_path: Path) -> None:
        caller = self._caller("fx-nproc", tmp_path, 64)

        outcome = caller.call_text("ulimit -u", stdin="")

        if outcome.result.stdout.strip() != "64":
            raise AssertionError(f"лимит не унаследован: {outcome.result.stdout!r}")

    def test_absent_limit_keeps_the_inherited_one(self, tmp_path: Path) -> None:
        """Без настройки зигота лимит не трогает: у детей он хостовый."""
        caller = self._caller("fx-nproc-off", tmp_path, None)
        inherited, _ = resource.getrlimit(resource.RLIMIT_NPROC)

        outcome = caller.call_text("ulimit -u", stdin="")

        if outcome.result.stdout.strip() != str(inherited):
            raise AssertionError(f"лимит подменён без настройки: {outcome.result!r}")

    def test_fork_beyond_the_cap_is_refused(self, tmp_path: Path) -> None:
        """Потолок общий: вызов не может наплодить задач сверх него."""
        caller = self._caller("fx-nproc-cap", tmp_path, 12)

        outcome = caller.call_text(
            "for i in $(seq 1 60); do sleep 5 & done; wait", stdin=""
        )

        stderr = outcome.result.stderr
        if "Resource temporarily unavailable" not in stderr:
            raise AssertionError(f"fork-бомба не упёрлась в потолок: {stderr!r}")
