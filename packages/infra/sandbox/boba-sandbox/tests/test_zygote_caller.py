"""ZygoteToolCaller: контракт ToolLauncher поверх зиготы.

Целевые тесты этапа 2: мост конверта, спавнер из профиля, cgroup-leaf,
журнал каналов и контракт ошибок LauncherError.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, ClassVar

import pytest
from fake_channel_tool import (
    ChannelConfig,
    FxChunkHead,
    fx_echo,
    fx_gated,
    fx_merge,
    fx_probe_tmp,
    fx_stream,
    fx_warm_state,
)
from pydantic import SecretStr

from boba.cancellation import ToolStopped, run_cancellation
from boba.sandbox import SandboxProfile
from boba.sandbox.guest import WarmupCall
from boba.sandbox.zygote import (
    ZygotePolicy,
    ZygoteSpawner,
    ZygoteState,
    ZygoteSupervisor,
    ZygoteToolCaller,
)
from boba.stand.shell import ShellRun
from boba.stand.zygote import ProfileFields, SandboxCgroup, SandboxStand, ZygoteStand
from boba.toolkit.chain import CallAmbient, ChannelFanOut, PipeTee
from boba.toolkit.channels import JournalChannel, ToolChannel
from boba.toolkit.entry import ToolAddress, ToolArgv, ToolMain
from boba.toolkit.frames import ToolFrame
from boba.toolkit.launcher import CollectedCall, LauncherError
from boba.toolkit.protocol import CallGateMode, ReplyError, ReplyOk
from boba.toolkit.stream import (
    ChannelSinks,
    Chunk,
    StreamSink,
)

REPO = Path(__file__).resolve().parents[5]
SANDBOX = REPO / "build" / "src" / "sandbox"
ROOTFS_IMAGE = SANDBOX / "plugins" / "boba-tool-shell" / "rootfs.ext4"

needs_sandbox = pytest.mark.skipif(
    shutil.which("bwrap") is None or not ROOTFS_IMAGE.exists(),
    reason="нет bwrap или артефактов песочницы (собрать: make fetch sandbox)",
)
needs_userns = pytest.mark.skipif(
    os.geteuid() == 0, reason="под root user namespace ведёт себя иначе"
)

pytestmark = [needs_sandbox, needs_userns]

CFG = ChannelConfig(token=SecretStr("zc-s3cret"))

FX_ECHO = ToolMain.toolset(fx_echo)[0]
FX_PROBE = ToolMain.toolset(fx_probe_tmp)[0]
FX_STREAM = ToolMain.toolset(fx_stream)[0]
FX_MERGE = ToolMain.toolset(fx_merge)[0]
FX_GATED = ToolMain.toolset(fx_gated)[0]

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
            "channel_limit_bytes": 67108864,
            "fail_tail_chars": 2000,
            "kill_grace_sec": 5,
            "cgroup_base": "",
        },
        "rootfs": str(ROOTFS_IMAGE),
        "mounts": {
            "ro": (
                f"{SANDBOX / 'third' / 'python'}:/usr/local",
                f"{SANDBOX / 'site'}:{site_packages}",
                f"{REPO / 'packages'}:/usr/src",
            ),
            "rw": (),
            "tmp": "64M",
        },
        "isolation": {
            "network": False,
            "env": {
                "PATH": "/usr/local/bin:/usr/bin:/bin",
                "PYTHONPATH": _python_path(),
                "HOME": "/tmp",  # noqa: S108
                "LANG": "C.UTF-8",
            },
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
    return ToolArgv.render(
        address, schema, {"text": text, "cfg": CFG}, input_counts={}, output_counts={}
    )


def _stream_command(prefix: str) -> Any:
    """ToolCommand потокового инструмента: io-параметр в конфиг не попадает."""
    address = ToolAddress(module="fake_channel_tool", name="fx_stream")
    schema = ToolArgv.schema_of(FX_STREAM)
    return ToolArgv.render(
        address,
        schema,
        {"prefix": prefix, "cfg": CFG},
        input_counts={},
        output_counts={},
    )


@pytest.fixture
def zygote(call_ambient: CallAmbient) -> Any:
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
        return ZygoteToolCaller("fx", supervisor, profile, call_ambient)

    yield make

    for supervisor in born:
        supervisor.stop()


class TestRunTool:
    def test_ok_envelope_with_secret_from_stdin(self, zygote: Any) -> None:
        caller = zygote(_profile())

        outcome = CollectedCall.of(caller, _command("ping"))

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

        if b"zc-s3cret" not in command.config:
            raise AssertionError("секрет не доехал конфигом вызова")

    def test_expected_failure_travels_as_reply_error(self, zygote: Any) -> None:
        caller = zygote(_profile())

        outcome = CollectedCall.of(caller, _command("boom"))

        if not isinstance(outcome.reply, ReplyError):
            raise AssertionError(f"reply={outcome.reply}")

        if outcome.reply.failure.error_kind != "FxDownError":
            raise AssertionError(f"failure={outcome.reply.failure!r}")

    def test_unavailable_zygote_is_launcher_error(self, zygote: Any) -> None:
        """Мёртвая зигота — LauncherError: фронт ловит её по контракту слоя."""
        caller = zygote(_profile())
        caller_supervisor = caller.supervisor
        caller_supervisor.stop()

        with pytest.raises(LauncherError):
            CollectedCall.of(caller, _command("late"))

    def test_timeout_without_envelope_is_launcher_error(self, zygote: Any) -> None:
        caller = zygote(_profile(timeout_sec=2))

        with pytest.raises(LauncherError, match="ended without a result"):
            CollectedCall.of(caller, _command("sleepy"))

    def test_journal_sinks_receive_channels(
        self, call_ambient: CallAmbient, zygote: Any
    ) -> None:
        caller = zygote(_profile())

        sinks = RecordingSinks()
        call_ambient.set_sinks(sinks)
        try:
            outcome = CollectedCall.of(caller, _command("journal"))
        finally:
            call_ambient.set_sinks(None)

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
        command = ToolArgv.render(
            address, schema, {"marker": "solo"}, input_counts={}, output_counts={}
        )

        outcome = CollectedCall.of(caller, command)

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
        """Корень образом — единственный профиль: зигота монтирует его сама."""
        profile = _profile(
            rootfs=str(ROOTFS_IMAGE),
        )

        spawner = ZygoteSpawner(profile, ["fake_channel_tool"], FAST)

        if spawner.root_label() != f"image {ROOTFS_IMAGE}":
            raise AssertionError(f"корень секции не образом: {spawner.root_label()!r}")

    def test_tmp_size_is_required(self) -> None:
        """Приватный /tmp ребёнка требует размера: нулевого tmp не бывает."""
        with pytest.raises(ValueError, match="tmp"):
            _profile(tmp=0)


needs_delegation = SandboxCgroup().required()


@needs_delegation
class TestCgroup:
    def test_leaf_created_and_released(self, zygote: Any) -> None:
        base = SandboxCgroup().child("zygote-test")
        caller = zygote(
            _profile(
                cgroup_base=base,
                group_memory_bytes=512 * 1024 * 1024,
                group_pids_max=64,
            )
        )

        outcome = CollectedCall.of(caller, _command("grouped"))

        if not isinstance(outcome.reply, ReplyOk):
            raise AssertionError(f"reply={outcome.reply}")

        leftovers: list[str] = []
        if os.path.isdir(base):
            for entry in os.listdir(base):
                if not entry.startswith("run-"):
                    continue

                leftovers.append(entry)

        if leftovers:
            raise AssertionError(f"leaf'ы не освобождены: {leftovers}")


class TestRegistry:
    """Реестр супервизоров: один живой процесс на секцию, гашение на shutdown."""

    @pytest.fixture(autouse=True)
    def zygotes_stopped(self, zygote_stand: ZygoteStand) -> Iterator[None]:
        """Зиготы секций гасятся после каждого теста класса."""
        yield
        zygote_stand.stop()

    def test_obtain_reuses_running_supervisor(self, zygote_stand: ZygoteStand) -> None:
        profile = _profile()

        first = zygote_stand.registry().obtain(
            "fx-reg", profile, ["fake_channel_tool"], FAST, warmup_calls=WARMUP_CALLS
        )
        second = zygote_stand.registry().obtain(
            "fx-reg", profile, ["fake_channel_tool"], FAST, warmup_calls=WARMUP_CALLS
        )

        if first is not second:
            raise AssertionError("повторный obtain должен вернуть тот же супервизор")

        if first.state is not ZygoteState.READY:
            raise AssertionError(f"state={first.state}")

    def test_stop_all_stops_and_next_obtain_restarts(
        self, zygote_stand: ZygoteStand
    ) -> None:
        profile = _profile()

        first = zygote_stand.registry().obtain(
            "fx-reg", profile, ["fake_channel_tool"], FAST, warmup_calls=WARMUP_CALLS
        )
        zygote_stand.stop()

        if first.state is not ZygoteState.STOPPED:
            raise AssertionError(f"state={first.state}")

        second = zygote_stand.registry().obtain(
            "fx-reg", profile, ["fake_channel_tool"], FAST, warmup_calls=WARMUP_CALLS
        )

        if second is first:
            raise AssertionError("после stop_all нужен новый супервизор")

        if second.state is not ZygoteState.READY:
            raise AssertionError(f"state={second.state}")


class TestWarmup:
    """Прогрев модуля: исполняется в зиготе до ready, дети видят результат."""

    @pytest.fixture(autouse=True)
    def zygotes_stopped(self, zygote_stand: ZygoteStand) -> Iterator[None]:
        """Зиготы секций гасятся после каждого теста класса."""
        yield
        zygote_stand.stop()

    def _call_warm_state(self, caller: ZygoteToolCaller) -> str:
        address = ToolAddress(module="fake_channel_tool", name="fx_warm_state")
        schema = ToolArgv.schema_of(
            next(t for t in ToolMain.toolset(fx_warm_state) if t)
        )
        outcome = CollectedCall.of(
            caller,
            ToolArgv.render(address, schema, {}, input_counts={}, output_counts={}),
        )

        if not isinstance(outcome.reply, ReplyOk):
            raise AssertionError(f"reply={outcome.reply}")

        return outcome.reply.content

    def test_warmup_runs_before_ready_and_children_inherit(
        self, zygote_stand: ZygoteStand
    ) -> None:
        profile = _profile()
        supervisor = zygote_stand.registry().obtain(
            "fx-warm",
            profile,
            ["fake_channel_tool"],
            FAST,
            warmup_calls=WARMUP_CALLS,
        )
        caller = ZygoteToolCaller("fx-warm", supervisor, profile, CallAmbient())

        state = self._call_warm_state(caller)
        if state != "warmed:privet":
            raise AssertionError(f"кэш прогрева не унаследован: {state!r}")

    def test_missing_config_fails_the_start(self, zygote_stand: ZygoteStand) -> None:
        """Молчаливой деградации нет: без конфига хука зигота не поднимается."""
        profile = _profile()

        with pytest.raises(LauncherError, match="not ready"):
            zygote_stand.registry().obtain(
                "fx-plain", profile, ["fake_channel_tool"], FAST
            )


def _mkfs_template(tmp_path: Path) -> str:
    """Шаблон workspace-образа: пустой ext4 на 8 МБ."""
    mkfs = shutil.which("mkfs.ext4")
    if mkfs is None:
        pytest.skip("mkfs.ext4 недоступен")

    template = tmp_path / "workspace.ext4"
    subprocess.run(
        [mkfs, "-q", "-F", str(template), "8m"], check=True, capture_output=True
    )
    return str(template)


def _image_profile(tmp_path: Path, **overrides: Any) -> SandboxProfile:
    """Профиль с образом workspace: обвязку монтирования ставит профиль."""
    template = _mkfs_template(tmp_path)
    images = tmp_path / "ws"
    images.mkdir(exist_ok=True)

    raw: dict[str, Any] = {
        "workspace": {
            "template": template,
            "mount": f"{images}/{{user_id}}.ext4:/workspace",
        },
        "tmp": "64M",
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

    @pytest.fixture(autouse=True)
    def zygotes_stopped(self, zygote_stand: ZygoteStand) -> Iterator[None]:
        """Зиготы секций гасятся после каждого теста класса."""
        yield
        zygote_stand.stop()

    def _caller(
        self, zygote_stand: ZygoteStand, tmp_path: Path, user_id: str
    ) -> ZygoteToolCaller:
        profile = _image_profile(tmp_path)
        supervisor = zygote_stand.registry().obtain(
            "fx-ws", profile, ["fake_channel_tool"], FAST, warmup_calls=WARMUP_CALLS
        )
        return ZygoteToolCaller(
            "fx-ws", supervisor, profile, CallAmbient(), lambda: {"user_id": user_id}
        )

    def _workspace_listing(self, caller: ZygoteToolCaller) -> str:
        outcome = CollectedCall.of(caller, _command("workspace"))
        if not isinstance(outcome.reply, ReplyOk):
            raise AssertionError(f"reply={outcome.reply}")

        return outcome.reply.artifact.model_dump_json()

    def test_image_created_from_template_and_persists(
        self, zygote_stand: ZygoteStand, tmp_path: Path
    ) -> None:
        caller = self._caller(zygote_stand, tmp_path, "7")

        first = self._workspace_listing(caller)
        if "fx-probe.txt" not in first:
            raise AssertionError(f"запись в образ не видна: {first}")

        outcome = CollectedCall.of(caller, _command("workspace"))
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

    def test_users_get_separate_images(
        self, zygote_stand: ZygoteStand, tmp_path: Path
    ) -> None:
        seven = self._caller(zygote_stand, tmp_path, "7")
        eight = self._caller(zygote_stand, tmp_path, "8")

        self._workspace_listing(seven)
        listing_eight = self._workspace_listing(eight)

        images = sorted(p.name for p in (tmp_path / "ws").glob("*.ext4"))
        if images != ["7.ext4", "8.ext4"]:
            raise AssertionError(f"образы по пользователям: {images}")

        if "workspace:fx-probe.txt" not in listing_eight:
            raise AssertionError(f"второй пользователь видит чужое: {listing_eight}")

    def test_parallel_calls_on_one_image_are_serialized(
        self, zygote_stand: ZygoteStand, tmp_path: Path
    ) -> None:
        caller = self._caller(zygote_stand, tmp_path, "7")

        with ThreadPoolExecutor(3) as pool:
            listings = list(
                pool.map(lambda _: self._workspace_listing(caller), range(3))
            )

        for listing in listings:
            if "fx-probe.txt" not in listing:
                raise AssertionError(f"параллельный вызов сорвался: {listing}")


needs_rootfs_image = pytest.mark.skipif(
    not ROOTFS_IMAGE.exists() or shutil.which("fuse2fs") is None,
    reason="нет rootfs.ext4 или fuse2fs (собрать: make sandbox)",
)


@needs_mkfs
@needs_rootfs_image
class TestImageRootfs:
    """Корень образом: зигота монтирует rootfs.ext4 сама, хост чист."""

    @pytest.fixture(autouse=True)
    def zygotes_stopped(self, zygote_stand: ZygoteStand) -> Iterator[None]:
        """Зиготы секций гасятся после каждого теста класса."""
        yield
        zygote_stand.stop()

    def _caller(
        self, zygote_stand: ZygoteStand, name: str, tmp_path: Path
    ) -> ZygoteToolCaller:
        profile = _profile(
            rootfs=str(ROOTFS_IMAGE),
        )
        return self._on_profile(zygote_stand, name, profile)

    def _image_caller(
        self, zygote_stand: ZygoteStand, name: str, tmp_path: Path
    ) -> ZygoteToolCaller:
        profile = _image_profile(
            tmp_path,
            rootfs=str(ROOTFS_IMAGE),
        )
        return self._on_profile(zygote_stand, name, profile)

    def _on_profile(
        self, zygote_stand: ZygoteStand, name: str, profile: SandboxProfile
    ) -> ZygoteToolCaller:
        supervisor = zygote_stand.registry().obtain(
            name, profile, ["fake_channel_tool"], SLOW_START, warmup_calls=WARMUP_CALLS
        )
        return ZygoteToolCaller(
            name, supervisor, profile, CallAmbient(), lambda: {"user_id": "7"}
        )

    FSTYPE_SEPARATOR: ClassVar[str] = " - "
    FUSE_PREFIX: ClassVar[str] = "fuse"

    def _fuse_mounts_of_host(self) -> set[str]:
        """fuse-монтирования хоста: образ, смонтированный мимо userns зиготы,
        виден здесь. Остальные монтирования хоста меняют соседние наборы и
        контейнеры параллельного прогона."""
        targets: set[str] = set()
        with open("/proc/self/mountinfo") as mountinfo:
            for line in mountinfo:
                fields, _, tail = line.partition(self.FSTYPE_SEPARATOR)
                if not tail.startswith(self.FUSE_PREFIX):
                    continue

                targets.add(fields.split()[4])

        return targets

    def test_zygote_serves_calls_from_the_image_root(
        self, zygote_stand: ZygoteStand, tmp_path: Path
    ) -> None:
        before = self._fuse_mounts_of_host()
        caller = self._caller(zygote_stand, "fx-img", tmp_path)

        outcome = CollectedCall.of(caller, _command("ping"))

        if not isinstance(outcome.reply, ReplyOk):
            raise AssertionError(f"reply={outcome.reply}")

        if "ping|zc-s3cret" not in outcome.reply.artifact.model_dump_json():
            raise AssertionError("тело не отработало на корне из образа")

        if self._fuse_mounts_of_host() != before:
            raise AssertionError("зигота смонтировала корень на хосте")

    def test_children_stay_isolated_on_the_image_root(
        self, zygote_stand: ZygoteStand, tmp_path: Path
    ) -> None:
        caller = self._caller(zygote_stand, "fx-img-iso", tmp_path)

        address = ToolAddress(module="fake_channel_tool", name="fx_probe_tmp")
        schema = ToolArgv.schema_of(FX_PROBE)
        command = ToolArgv.render(
            address, schema, {"marker": "img.txt"}, input_counts={}, output_counts={}
        )

        outcome = CollectedCall.of(caller, command)
        if not isinstance(outcome.reply, ReplyOk):
            raise AssertionError(f"reply={outcome.reply}")

        state = json.loads(outcome.reply.content)

        if state["markers"] != ["img.txt"]:
            raise AssertionError(f"частный /tmp вызова: {state}")

        if state["cap_eff"] != "0000000000000000":
            raise AssertionError(f"capabilities не сброшены: {state}")

        if state["userns_max"] != "0":
            raise AssertionError(f"вложенные userns не закрыты: {state}")

    def test_workspace_image_works_on_the_image_root(
        self, zygote_stand: ZygoteStand, tmp_path: Path
    ) -> None:
        caller = self._image_caller(zygote_stand, "fx-img-ws", tmp_path)

        first = ShellRun.call_text(caller, "echo hello > note.txt; pwd")
        if first.exit_code != 0:
            raise AssertionError(f"rc={first.exit_code}: {first.stderr}")

        if "/workspace" not in first.stdout:
            raise AssertionError(f"cwd не workspace: {first.stdout!r}")

        second = ShellRun.call_text(caller, "cat /workspace/note.txt")
        if second.stdout.strip() != "hello":
            raise AssertionError(f"файл не пережил вызов: {second.stdout!r}")


@needs_mkfs
class TestShell:
    """call_text через зиготу: bash-команда в изолированном ребёнке с образом."""

    @pytest.fixture(autouse=True)
    def zygotes_stopped(self, zygote_stand: ZygoteStand) -> Iterator[None]:
        """Зиготы секций гасятся после каждого теста класса."""
        yield
        zygote_stand.stop()

    def _caller(
        self, zygote_stand: ZygoteStand, tmp_path: Path, user_id: str = "7"
    ) -> ZygoteToolCaller:
        profile = _image_profile(tmp_path, timeout_sec=5)
        supervisor = zygote_stand.registry().obtain("fx-sh", profile, (), FAST)
        return ZygoteToolCaller(
            "fx-sh", supervisor, profile, CallAmbient(), lambda: {"user_id": user_id}
        )

    def test_stdout_stderr_closed_stdin_and_exit_code(
        self, zygote_stand: ZygoteStand, tmp_path: Path
    ) -> None:
        caller = self._caller(zygote_stand, tmp_path)

        outcome = ShellRun.call_text(
            caller, "cat; echo out-line; echo err-line >&2; exit 3"
        )

        if outcome.exit_code != 3:
            raise AssertionError(f"rc={outcome.exit_code}")

        if not outcome.stdout.startswith("out-line"):
            raise AssertionError(f"stdin is not closed: {outcome.stdout!r}")

        if "out-line" not in outcome.stdout:
            raise AssertionError(f"stdout={outcome.stdout!r}")

        if "err-line" not in outcome.stderr:
            raise AssertionError(f"stderr={outcome.stderr!r}")

        # кадры монтирования — голос обвязки, не команды: их забирает релей
        if "sandbox-mount" in outcome.stderr:
            raise AssertionError(f"кадры обвязки в stderr команды: {outcome.stderr!r}")

    def test_workspace_persists_between_commands(
        self, zygote_stand: ZygoteStand, tmp_path: Path
    ) -> None:
        caller = self._caller(zygote_stand, tmp_path)

        first = ShellRun.call_text(caller, "pwd; echo hello > note.txt; ls")
        if first.exit_code != 0:
            raise AssertionError(f"rc={first.exit_code}: {first.stderr}")

        if "/workspace" not in first.stdout:
            raise AssertionError(f"cwd не workspace: {first.stdout!r}")

        second = ShellRun.call_text(caller, "cat /workspace/note.txt")
        if second.stdout.strip() != "hello":
            raise AssertionError(f"файл не пережил вызов: {second.stdout!r}")

    def test_timeout_kills_command(
        self, zygote_stand: ZygoteStand, tmp_path: Path
    ) -> None:
        """Таймаут профиля короче таймаута команды: умирает весь вызов."""
        caller = self._caller(zygote_stand, tmp_path)

        with pytest.raises(LauncherError, match="timeout_sec=5"):
            ShellRun.call_text(caller, "sleep 30")

    def test_command_runs_isolated_without_capabilities(
        self, zygote_stand: ZygoteStand, tmp_path: Path
    ) -> None:
        caller = self._caller(zygote_stand, tmp_path)

        # bash — ребёнок исполнителя (тот ещё гасит fuse2fs после команды),
        # поэтому init своего pid ns — python-исполнитель, а bash рядом с ним
        outcome = ShellRun.call_text(
            caller,
            "echo init=$(cat /proc/1/comm); echo procs=$(ls /proc | grep -c '^[0-9]');"
            " grep CapEff /proc/self/status",
        )

        stdout = outcome.stdout
        if "init=python3" not in stdout:
            raise AssertionError(f"init pid ns — не исполнитель: {stdout!r}")

        procs = int(stdout.split("procs=")[1].split()[0])
        # исполнитель, тело bash-тула, сам bash, subshell и утилиты echo
        if procs > 8:
            raise AssertionError(f"в pid ns видны чужие процессы: {stdout!r}")

        if "0000000000000000" not in stdout:
            raise AssertionError(f"capabilities не сброшены: {stdout!r}")


class TestManyInputs:
    """Порт-список в песочнице: входы после первого едут хвостом SCM_RIGHTS
    и доходят до тела своими дескрипторами."""

    def _merge(self, inputs: int) -> Any:
        address = ToolAddress(module="fake_channel_tool", name="fx_merge")
        schema = ToolArgv.schema_of(FX_MERGE)
        return ToolArgv.render(
            address,
            schema,
            {"cfg": CFG},
            input_counts={"feeds": inputs},
            output_counts={},
        )

    def test_each_input_reaches_its_port(self, zygote: Any) -> None:
        caller = zygote(_profile())

        with caller.open(self._merge(4)) as call:
            entries = call.inputs()
            for index, entry in enumerate(entries):
                entry.send(ToolFrame.of(FxChunkHead(seq=1), f"in{index}".encode()))

            call.done_sending()
            outcome = call.result()

        if not isinstance(outcome.reply, ReplyOk):
            raise AssertionError(f"reply={outcome.reply}")

        if outcome.reply.content != "merged 4:in0;in1;in2;in3|zc-s3cret":
            raise AssertionError(f"content={outcome.reply.content!r}")

    WIRE_FIT_INPUTS = 32
    """Входов, заведомо умещающихся с фиксированными каналами в одно
    сообщение ZygoteWire (MAX_FDS)."""

    BATCHED_INPUTS = 90
    """Входов, заведомо НЕ умещающихся в одно сообщение: хвост дескрипторов
    едет продолжениями FdsBatch (ZygoteWire.send_batched)."""

    def test_many_inputs_fit_one_message(self, zygote: Any) -> None:
        """Десятки входов вызова проходят одним сообщением зиготе."""
        self._roundtrip(zygote, self.WIRE_FIT_INPUTS)

    def test_input_tail_over_one_message_is_batched(self, zygote: Any) -> None:
        """Хвост дескрипторов больше потолка сообщения доезжает пачками:
        каждый вход доходит до тела и читается до EOF."""
        self._roundtrip(zygote, self.BATCHED_INPUTS)

    def _roundtrip(self, zygote: Any, inputs: int) -> None:
        caller = zygote(_profile())

        with caller.open(self._merge(inputs)) as call:
            call.done_sending()
            outcome = call.result()

        if not isinstance(outcome.reply, ReplyOk):
            raise AssertionError(f"reply={outcome.reply}")

        expected = f"merged {inputs}:"
        if not outcome.reply.content.startswith(expected):
            raise AssertionError(f"content={outcome.reply.content!r}")


class TestStreamingCall:
    """Потоковый вызов в песочнице: кадры внутрь и наружу, конверт в конце."""

    def test_frames_answer_frames_and_envelope_closes_call(self, zygote: Any) -> None:
        caller = zygote(_profile())

        with caller.open(_stream_command("re:")) as call:
            call.inputs()[0].send(ToolFrame.of(FxChunkHead(seq=1), b"one"))
            call.inputs()[0].send(ToolFrame.of(FxChunkHead(seq=2), b"two"))
            call.done_sending()

            kinds: list[str] = []
            bodies: list[bytes] = []
            for frame in call.frames():
                kinds.append(frame.kind)
                bodies.append(frame.body)

            outcome = call.result()

        if kinds != ["chunk", "chunk", "done"]:
            raise AssertionError(f"kinds={kinds}")

        if bodies[:2] != [b"re:one", b"re:two"]:
            raise AssertionError(f"bodies={bodies[:2]!r}")

        if not isinstance(outcome.reply, ReplyOk):
            raise AssertionError(f"reply={outcome.reply}")

        if "streamed 2" not in outcome.reply.content:
            raise AssertionError(f"content={outcome.reply.content!r}")

    def test_frames_arrive_before_input_is_closed(self, zygote: Any) -> None:
        caller = zygote(_profile())

        with caller.open(_stream_command("x:")) as call:
            call.inputs()[0].send(ToolFrame.of(FxChunkHead(seq=1), b"early"))

            stream = call.frames()
            first = next(stream)

            if first.body != b"x:early":
                raise AssertionError(f"first={first.body!r}")

            call.done_sending()
            rest = [frame.kind for frame in stream]
            outcome = call.result()

        if rest != ["done"]:
            raise AssertionError(f"rest={rest}")

        if not isinstance(outcome.reply, ReplyOk):
            raise AssertionError(f"reply={outcome.reply}")

    def test_frames_journal_keeps_heads_without_bodies(
        self, call_ambient: CallAmbient, zygote: Any
    ) -> None:
        caller = zygote(_profile())

        sinks = RecordingSinks()
        call_ambient.set_sinks(sinks)
        try:
            with caller.open(_stream_command("j:")) as call:
                call.inputs()[0].send(ToolFrame.of(FxChunkHead(seq=1), b"body-bytes"))
                call.done_sending()
                list(call.frames())
                call.result()
        finally:
            call_ambient.set_sinks(None)

        journal = sinks.text_of(ToolChannel.FRAMES)
        if '"seq":1' not in journal:
            raise AssertionError(f"tool_frames={journal!r}")

        # вход тоже попадает в журнал; заголовки без тел проверяет
        # юнит FrameHeadsSink — здесь тестовый приёмник пишет сырьё
        stdin_journal = sinks.text_of(ToolChannel.STDIN)
        if '"seq":1' not in stdin_journal:
            raise AssertionError(f"tool_stdin={stdin_journal!r}")


class TestFanOutInSandbox:
    """Раздача выхода одного вызова песочницы двум другим через tee: хост
    данные не разбирает, кадры раскодируют только приёмники."""

    def test_one_source_feeds_two_sandboxed_readers(self, zygote: Any) -> None:
        caller = zygote(_profile())

        tapped = caller.open_tap(_stream_command("s:"))
        readers = [
            caller.open(_stream_command("a:")),
            caller.open(_stream_command("b:")),
        ]

        reader_fds: list[int] = []
        for reader in readers:
            reader_fds.append(reader.inputs()[0].take_fd())
            reader.done_sending()

        drained = threading.Event()
        errors: list[str] = []
        fanout = ChannelFanOut(
            "s", tapped.frames_fds[0], reader_fds, PipeTee(), errors.append, drained.set
        )
        fanout.start()

        with tapped.call as source:
            source.inputs()[0].send(ToolFrame.of(FxChunkHead(seq=1), b"data"))
            source.done_sending()
            source_outcome = source.result()

        if not drained.wait(timeout=60):
            raise AssertionError("fan-out did not drain")

        for fd in reader_fds:
            os.close(fd)

        for reader, prefix in zip(readers, (b"a:", b"b:"), strict=True):
            with reader:
                bodies = [frame.body for frame in reader.frames()]
                outcome = reader.result()

            if prefix + b"s:data" not in bodies:
                raise AssertionError(f"reader {prefix!r} got {bodies}")

            if not isinstance(outcome.reply, ReplyOk):
                raise AssertionError(f"reader {prefix!r}: {outcome.reply}")

        if errors:
            raise AssertionError(f"fan-out errors: {errors}")

        if not isinstance(source_outcome.reply, ReplyOk):
            raise AssertionError(f"source={source_outcome.reply}")


class TestGateInSandbox:
    """Барьер группы через зиготу: пайпы gate/verdict доезжают до тела."""

    def _gated(self, gate: CallGateMode) -> Any:
        address = ToolAddress(module="fake_channel_tool", name="fx_gated")
        command = ToolArgv.render(
            address,
            ToolArgv.schema_of(FX_GATED),
            {"cfg": CFG},
            input_counts={},
            output_counts={},
        )
        return command.model_copy(update={"gate": gate})

    def test_auto_gate_lets_a_lone_call_commit(self, zygote: Any) -> None:
        caller = zygote(_profile())

        outcome = CollectedCall.of(caller, self._gated(CallGateMode.AUTO))

        if not isinstance(outcome.reply, ReplyOk):
            raise AssertionError(f"reply={outcome.reply}")

    def test_held_gate_waits_for_the_owner(self, zygote: Any) -> None:
        caller = zygote(_profile())
        arrived = threading.Event()

        with caller.open(self._gated(CallGateMode.HELD)) as call:
            call.gate().claim(arrived.set)
            call.done_sending()

            if not arrived.wait(timeout=30):
                raise AssertionError("body never reached the gate")

            call.gate().release()
            outcome = call.result()

        if not isinstance(outcome.reply, ReplyOk):
            raise AssertionError(f"reply={outcome.reply}")

    def test_refused_gate_fails_the_body(self, zygote: Any) -> None:
        caller = zygote(_profile())
        arrived = threading.Event()

        with caller.open(self._gated(CallGateMode.HELD)) as call:
            call.gate().claim(arrived.set)
            call.done_sending()

            if not arrived.wait(timeout=30):
                raise AssertionError("body never reached the gate")

            call.gate().refuse()
            outcome = call.result()

        if not isinstance(outcome.reply, ReplyError):
            raise AssertionError(f"reply={outcome.reply}")

        if "must not commit" not in outcome.reply.failure.llm_view():
            raise AssertionError(f"failure={outcome.reply.failure!r}")


class TestCallResilience:
    """Срывы потокового вызова в песочнице: отмена, второй читатель, ресурсы."""

    def test_cancel_right_after_open_is_not_lost(self, zygote: Any) -> None:
        """Гонка отмены: cancel сразу после open обязан убить вызов."""
        caller = zygote(_profile())

        with run_cancellation() as cancellation:
            call = caller.open(_stream_command("c:"))
            cancellation.cancel()

            with pytest.raises(ToolStopped):
                call.result()

    def test_open_on_cancelled_run_raises_and_leaks_nothing(self, zygote: Any) -> None:
        """Уже отменённый ход: open падает сразу, проводка прибрана."""
        caller = zygote(_profile())

        before = len(os.listdir("/proc/self/fd"))

        with run_cancellation() as cancellation:
            cancellation.cancel()

            with pytest.raises(ToolStopped):
                caller.open(_stream_command("c:"))

        after = len(os.listdir("/proc/self/fd"))
        if after != before:
            raise AssertionError(f"дескрипторы утекли: {before} -> {after}")

    def test_second_frames_reader_is_refused(self, zygote: Any) -> None:
        caller = zygote(_profile())

        with caller.open(_stream_command("r:")) as call:
            call.frames()

            with pytest.raises(LauncherError, match="already have a reader"):
                call.frames()

            call.done_sending()
            call.result()

    def test_no_fd_leak_across_streaming_calls(self, zygote: Any) -> None:
        """Дескрипторы после серии вызовов — как до неё: успех и отмена."""
        caller = zygote(_profile())

        def one_ok() -> None:
            with caller.open(_stream_command("f:")) as call:
                call.inputs()[0].send(ToolFrame.of(FxChunkHead(seq=1), b"data"))
                call.done_sending()
                list(call.frames())
                call.result()

        def one_closed() -> None:
            call = caller.open(_stream_command("f:"))
            call.close()

            with pytest.raises(ToolStopped):
                call.result()

        one_ok()

        before = len(os.listdir("/proc/self/fd"))
        for _ in range(3):
            one_ok()
            one_closed()

        after = len(os.listdir("/proc/self/fd"))
        if after != before:
            raise AssertionError(f"дескрипторы утекли: {before} -> {after}")
