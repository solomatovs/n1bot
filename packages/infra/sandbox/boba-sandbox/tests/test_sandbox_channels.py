"""Канальный контракт на реальном bwrap: разделение каналов, квитанции, уборка.

Путь исполнения один — WorkflowRunner: одиночный вызов здесь тоже вырожденный
граф из одного узла.
"""

from __future__ import annotations

import json
import logging
import os
import shlex
import shutil
import subprocess
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import ExitStack
from enum import StrEnum
from pathlib import Path
from typing import Any

import pydantic
import pytest
from journal_stand import JournalStand
from pydantic import BaseModel, ConfigDict, JsonValue

from boba.cancellation import (
    ToolStopped,
    TurnCancellation,
    current_cancellation,
    turn_cancellation,
)
from boba.sandbox.argv import ChannelArgv
from boba.sandbox.profile import SandboxProfile
from boba.sandbox.runner import (
    ChannelPump,
    ChannelSink,
    SandboxChannels,
)
from boba.sandbox.workflow import StageDef, StageRegistry, WorkflowRunner
from boba.toolkit.channels import Channel, StreamFormat
from boba.toolkit.launcher import EmptyTrailer, PayloadFailureError
from boba.toolkit.workflow import (
    StageContract,
    StageSpec,
    WorkflowError,
    WorkflowOutcome,
    WorkflowSpec,
)
from boba.workspace.launcher import FUSE_DEVICE

REPO = Path(__file__).resolve().parents[5]
TOOLKIT_SRC = REPO / "packages" / "core" / "boba-toolkit" / "src"
SITE_PACKAGES = Path(pydantic.__file__).resolve().parents[1]

HOST_RO_BINDS: tuple[str, ...] = ("/usr", "/bin", "/sbin", "/lib", "/lib64")

PAYLOAD_ENTRY: tuple[str, ...] = ("python3.11", "/opt/payload/main.py")
PAYLOAD_COMMAND = shlex.join(PAYLOAD_ENTRY)
STAGE_TOOL = "smoke"
STAGE_ID = "smoke"

needs_bwrap = pytest.mark.skipif(
    shutil.which("bwrap") is None, reason="bwrap не установлен"
)
needs_userns = pytest.mark.skipif(
    os.geteuid() == 0, reason="под root userns ведёт себя иначе"
)
needs_fuse = pytest.mark.skipif(
    shutil.which("bwrap") is None
    or shutil.which("fuse2fs") is None
    or shutil.which("mkfs.ext4") is None
    or not os.path.exists(FUSE_DEVICE),
    reason="нужны bwrap, fuse2fs, mkfs.ext4 и /dev/fuse",
)


class Marker(StrEnum):
    """Уникальные строки-пробы для поиска по /proc."""

    PROFILE = "profile-marker-3f9c71"
    SECRET = "secret-cred-a51d0e"
    CANCEL = "cancel-marker-77b2e4"
    TIMEOUT = "timeout-marker-90cd15"


def _bin_dirs() -> list[str]:
    """В тестах каталоги берутся из PATH; в проде их задаёт конфиг."""
    dirs: list[str] = []

    for entry in os.environ.get("PATH", "").split(os.pathsep):
        if not entry.startswith("/"):
            continue

        dirs.append(entry)

    return dirs


_PROFILE_BASE: dict[str, Any] = {
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
    "tmpfs": ("/tmp:64M",),  # noqa: S108
    "network": False,
    "env_set": {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "PYTHONPATH": "/opt/src:/opt/site",
        "HOME": "/tmp",  # noqa: S108
        "LANG": "C.UTF-8",
        "PROFILE_MARKER": Marker.PROFILE.value,
    },
    "timeout_sec": 30,
    "max_memory_bytes": 512 * 1024 * 1024,
    "max_cpu_sec": 30,
    "max_file_size_bytes": 64 * 1024 * 1024,
    "max_open_files": 1024,
    "max_processes": 256,
    "max_output_bytes": 256 * 1024,
    "cgroup_base": "",
    "oom_score_adj": 0,
    "cwd": "/tmp",  # noqa: S108
}


_SMOKE_PAYLOAD = """
import logging
import sys

from pydantic import BaseModel, ConfigDict, JsonValue

from boba.toolkit.payload import PayloadChannels, PayloadLogging


class Request(BaseModel):
    note: str


class Trailer(BaseModel):
    stdin_text: str
    note: str


PayloadLogging.setup()
channels = PayloadChannels.open()
request = channels.args(Request)

stdin_text = channels.stdin().read().decode("utf-8")

print("human note")
print("raw stderr line", file=sys.stderr)
logging.getLogger("smoke").warning("log frame message")

stream = channels.payload()
stream.write(b"line-1\\nline-2\\n")

channels.write_result(Trailer(stdin_text=stdin_text, note=request.note))
sys.exit(int(channels.exit_code()))
"""


_FAILURE_PAYLOAD = """
import sys

from pydantic import BaseModel, ConfigDict, JsonValue

from boba.toolkit.payload import PayloadChannels, PayloadLogging


class Request(BaseModel):
    pass


PayloadLogging.setup()
channels = PayloadChannels.open()
channels.args(Request)
channels.write_error("smoke_failure", "the operation failed as expected")
sys.exit(1)
"""


_ECHO_PAYLOAD = """
import sys

from pydantic import BaseModel, ConfigDict, JsonValue

from boba.toolkit.payload import PayloadChannels, PayloadLogging


class Request(BaseModel):
    note: str


class Trailer(BaseModel):
    note: str


PayloadLogging.setup()
channels = PayloadChannels.open()
request = channels.args(Request)
channels.write_result(Trailer(note=request.note))
sys.exit(int(channels.exit_code()))
"""


_WAIT_PAYLOAD = """
import os
import sys
import time

from pydantic import BaseModel, ConfigDict, JsonValue

from boba.toolkit.payload import PayloadChannels, PayloadLogging


class Request(BaseModel):
    ready_path: str
    stop_path: str
    secret: str


class Trailer(BaseModel):
    marker_env: str
    secret_seen: str


PayloadLogging.setup()
channels = PayloadChannels.open()
request = channels.args(Request)

with open(request.ready_path, "w", encoding="utf-8") as ready:
    ready.write("ready")

deadline = time.monotonic() + 20.0
while time.monotonic() < deadline:
    if os.path.exists(request.stop_path):
        break
    time.sleep(0.05)

channels.write_result(
    Trailer(
        marker_env=os.environ.get("PROFILE_MARKER", ""),
        secret_seen=request.secret,
    )
)
sys.exit(int(channels.exit_code()))
"""


_IMAGE_PAYLOAD = """
import os
import sys
import time
from pathlib import Path

from pydantic import BaseModel, ConfigDict, JsonValue

from boba.toolkit.payload import PayloadChannels, PayloadLogging


class Request(BaseModel):
    ready_path: str
    stop_path: str


class Trailer(BaseModel):
    marker_env: str
    workspace_file: str


PayloadLogging.setup()
channels = PayloadChannels.open()
request = channels.args(Request)

Path("/workspace/from-stage.txt").write_text("written by stage", encoding="utf-8")

with open(request.ready_path, "w", encoding="utf-8") as ready:
    ready.write("ready")

deadline = time.monotonic() + 20.0
while time.monotonic() < deadline:
    if os.path.exists(request.stop_path):
        break
    time.sleep(0.05)

stream = channels.payload()
stream.write(b"image-data\\n")

channels.write_result(
    Trailer(
        marker_env=os.environ.get("PROFILE_MARKER", ""),
        workspace_file=Path("/workspace/from-stage.txt").read_text(encoding="utf-8"),
    )
)
sys.exit(int(channels.exit_code()))
"""


class SmokeTrailer(BaseModel):
    """Трейлер smoke-payload'а: эхо stdin и аргумента."""

    stdin_text: str
    note: str


class EchoTrailer(BaseModel):
    """Трейлер echo-payload'а без канала данных."""

    note: str


class WaitTrailer(BaseModel):
    """Трейлер ждущего payload'а: env-проба и секрет из tool_args."""

    marker_env: str
    secret_seen: str


class ImageTrailer(BaseModel):
    """Трейлер стадии с образом: env-проба и файл из /workspace."""

    marker_env: str
    workspace_file: str


class _CollectSink(ChannelSink):
    """Приёмник канала: копит байты и отмечает закрытие."""

    def __init__(self) -> None:
        self.data = bytearray()
        self.closed = False

    def feed(self, data: bytes) -> None:
        self.data.extend(data)

    def close(self) -> None:
        self.closed = True

    def text(self) -> str:
        return bytes(self.data).decode("utf-8")


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

    subprocess.run(  # noqa: S603
        [mkfs, "-F", "-q", "-O", "^has_journal", "-m", "0", str(path)],
        check=True,
    )

    return path


def _write_payload(tmp_path: Path, script: str) -> Path:
    payload_dir = tmp_path / "payload"
    payload_dir.mkdir(parents=True, exist_ok=True)
    (payload_dir / "main.py").write_text(script, encoding="utf-8")

    return payload_dir


def _profile(payload_dir: Path, **kw: Any) -> SandboxProfile:
    fields = dict(_PROFILE_BASE)

    binds = list(HOST_RO_BINDS)
    binds.append(f"{TOOLKIT_SRC}:/opt/src")
    binds.append(f"{SITE_PACKAGES}:/opt/site")
    binds.append(f"{payload_dir}:/opt/payload")
    fields["ro_binds"] = tuple(binds)

    fields.update(kw)

    return SandboxProfile.model_validate(fields)


class AnyRequest(BaseModel):
    """Запрос тестового узла: пользовательские поля проходят насквозь."""

    model_config = ConfigDict(extra="allow")


def _identity_args(args: Mapping[str, JsonValue], /) -> Mapping[str, JsonValue]:
    return dict(args)


def _allow_all(tool: str, /) -> bool:
    return True


def _image_path_vars() -> dict[str, str]:
    return {"user_id": "7", "thread_id": "t1"}


def _contract(
    *,
    out: StreamFormat | None,
    result: type[BaseModel],
) -> StageContract:
    return StageContract(out=out, result=result)


def _run_stage(  # noqa: PLR0913
    profile: SandboxProfile,
    *,
    contract: StageContract,
    args: Mapping[str, JsonValue],
    stdin: str | None,
    payload: ChannelSink | None,
    entry: tuple[str, ...] = PAYLOAD_ENTRY,
    path_vars: Callable[[], Mapping[str, str]] = dict,
) -> WorkflowOutcome:
    """Вырожденный граф из одного узла: тот же путь, что у графа из десяти."""
    definition = StageDef(
        contract=contract,
        profile=profile,
        entry=entry,
        request=AnyRequest,
        enrich=_identity_args,
    )
    registry = StageRegistry({STAGE_TOOL: definition})
    runner = WorkflowRunner(registry, _allow_all, path_vars, JournalStand.journal())

    spec = WorkflowSpec(
        nodes=[StageSpec(id=STAGE_ID, tool=STAGE_TOOL, args=args, stdin=stdin)]
    )

    collectors: dict[str, ChannelSink] = {}
    if payload is not None:
        collectors[STAGE_ID] = payload

    return runner.run(spec, JournalStand.context(), collectors)


def _proc_files(name: str) -> Iterator[bytes]:
    """Читаемые файлы /proc/PID/{name} всех процессов; чужие молча пропускаются."""
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue

        try:
            yield (Path("/proc") / entry / name).read_bytes()
        except OSError:
            continue


def _seen_in_proc(name: str, needle: bytes) -> bool:
    for blob in _proc_files(name):
        if needle in blob:
            return True

    return False


def _wait_for_file(path: Path, timeout_sec: float = 15.0) -> None:
    deadline = time.monotonic() + timeout_sec

    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(0.05)

    raise AssertionError(f"file did not appear in time: {path}")


def _wait_for_stage(needle: bytes, timeout_sec: float = 15.0) -> None:
    deadline = time.monotonic() + timeout_sec

    while time.monotonic() < deadline:
        if _seen_in_proc("cmdline", needle):
            return
        time.sleep(0.05)

    raise AssertionError(f"stage was not seen in /proc cmdline: {needle!r}")


def _assert_stage_gone(needle: bytes, timeout_sec: float = 10.0) -> None:
    deadline = time.monotonic() + timeout_sec

    while time.monotonic() < deadline:
        if not _seen_in_proc("cmdline", needle):
            return
        time.sleep(0.1)

    raise AssertionError(f"stage processes are still alive: {needle!r}")


def _open_fd_count() -> int:
    return len(os.listdir("/proc/self/fd"))


def _assert_fds_restored(baseline: int, timeout_sec: float = 5.0) -> None:
    deadline = time.monotonic() + timeout_sec

    while time.monotonic() < deadline:
        if _open_fd_count() <= baseline:
            return
        time.sleep(0.05)

    raise AssertionError(f"descriptors leaked: was {baseline}, now {_open_fd_count()}")


@needs_bwrap
@needs_userns
class TestChannelSeparation:
    """Каналы одного запуска не смешиваются: каждый несёт ровно своё."""

    def test_every_channel_carries_only_its_content(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        payload_dir = _write_payload(tmp_path, _SMOKE_PAYLOAD)
        profile = _profile(payload_dir)

        payload_sink = _CollectSink()

        with caplog.at_level(logging.WARNING, logger="boba.sandbox.runner"):
            outcome = _run_stage(
                profile,
                contract=_contract(out=StreamFormat.TEXT, result=SmokeTrailer),
                args={"note": "hello"},
                stdin="12345",
                payload=payload_sink,
            )

        stdout_text = JournalStand.text_of(outcome, STAGE_ID, Channel.TOOL_STDOUT)

        assert bytes(payload_sink.data) == b"line-1\nline-2\n"
        assert stdout_text == "human note\n"

        trailer = outcome.trailer(STAGE_ID, SmokeTrailer)
        assert trailer == SmokeTrailer(stdin_text="12345", note="hello")

        relayed: list[str] = []
        for record in caplog.records:
            if "log frame message" in record.getMessage():
                relayed.append(record.getMessage())

        assert relayed, "log frame must reach the app logger via relay"

        assert "log frame message" not in stdout_text
        assert "raw stderr line" not in stdout_text
        assert "sandbox-log:" not in stdout_text
        assert b"log frame message" not in bytes(payload_sink.data)
        assert b"raw stderr line" not in bytes(payload_sink.data)

        assert payload_sink.closed

    def test_expected_failure_travels_as_error_envelope(self, tmp_path: Path) -> None:
        payload_dir = _write_payload(tmp_path, _FAILURE_PAYLOAD)
        profile = _profile(payload_dir)

        with pytest.raises(PayloadFailureError) as failure:
            _run_stage(
                profile,
                contract=_contract(out=None, result=EmptyTrailer),
                args={},
                stdin=None,
                payload=None,
            )

        assert failure.value.kind == "smoke_failure"
        assert "the operation failed as expected" in str(failure.value)

    def test_request_off_the_model_is_invalid_request(self, tmp_path: Path) -> None:
        """Запрос не по модели payload'а — ожидаемый отказ, а не трейсбек."""
        payload_dir = _write_payload(tmp_path, _ECHO_PAYLOAD)
        profile = _profile(payload_dir)

        with pytest.raises(PayloadFailureError) as failure:
            _run_stage(
                profile,
                contract=_contract(out=None, result=EchoTrailer),
                args={},
                stdin=None,
                payload=None,
            )

        assert failure.value.kind == "invalid_request"
        assert "Traceback" not in str(failure.value)

    def test_stage_without_payload_channel_reports_zero_bytes(
        self, tmp_path: Path
    ) -> None:
        payload_dir = _write_payload(tmp_path, _ECHO_PAYLOAD)
        profile = _profile(payload_dir)

        outcome = _run_stage(
            profile,
            contract=_contract(out=None, result=EchoTrailer),
            args={"note": "no-stream"},
            stdin=None,
            payload=None,
        )

        assert outcome.trailer(STAGE_ID, EchoTrailer).note == "no-stream"


@needs_bwrap
@needs_userns
class TestWrapChannels:
    """wrap-каналы — голос обвязки: молчат при успехе, объясняют провал настройки."""

    @staticmethod
    def _pump_raw(
        profile: SandboxProfile,
        command: str,
        *,
        args: bytes,
        stdin: bytes,
    ) -> tuple[int, dict[Channel, _CollectSink]]:
        """Низкоуровневый прогон стадии с собственными приёмниками всех каналов."""
        wanted = [
            Channel.WRAP_ARGS,
            Channel.TOOL_ARGS,
            Channel.TOOL_STDIN,
            Channel.TOOL_STDOUT,
            Channel.TOOL_STDERR,
            Channel.TOOL_PAYLOAD,
            Channel.TOOL_RESULT,
            Channel.WRAP_STDOUT,
            Channel.WRAP_STDERR,
        ]

        collectors: dict[Channel, _CollectSink] = {}
        for channel in wanted:
            if channel.writes_in:
                continue
            collectors[channel] = _CollectSink()

        with ExitStack() as stack:
            channels = stack.enter_context(SandboxChannels(wanted))

            env = dict(profile.env_set) | channels.child_env()
            built = ChannelArgv.build(
                profile,
                command,
                env=env,
                wrap_args_fd=channels.child_fd(Channel.WRAP_ARGS),
                redirect_prefix=channels.redirect_prefix(),
            )

            proc = subprocess.Popen(  # noqa: S603
                list(built.argv),
                shell=False,
                stdin=channels.child_fd(Channel.TOOL_STDIN),
                stdout=channels.child_fd(Channel.WRAP_STDOUT),
                stderr=channels.child_fd(Channel.WRAP_STDERR),
                bufsize=0,
                close_fds=True,
                pass_fds=channels.child_fds(),
                cwd="/",
                env=dict(os.environ),
            )
            stack.callback(ChannelPump.reap, proc)

            channels.close_child_side()

            feeds: dict[Channel, bytes] = {
                Channel.WRAP_ARGS: built.wrap_args,
                Channel.TOOL_ARGS: args,
                Channel.TOOL_STDIN: stdin,
            }

            pump = stack.enter_context(ChannelPump())
            pump.add(
                "raw",
                proc=proc,
                channels=channels,
                sinks=collectors,
                feeds=feeds,
                timeout_sec=30.0,
                on_timeout=proc.kill,
            )
            pump.run(current_cancellation())

        exit_code = proc.returncode
        assert exit_code is not None

        return exit_code, collectors

    def test_wrap_channels_silent_on_success(self, tmp_path: Path) -> None:
        payload_dir = _write_payload(tmp_path, _SMOKE_PAYLOAD)
        profile = _profile(payload_dir)

        exit_code, collected = self._pump_raw(
            profile,
            PAYLOAD_COMMAND,
            args=json.dumps({"note": "quiet"}).encode("utf-8"),
            stdin=b"abc",
        )

        assert exit_code == 0
        assert bytes(collected[Channel.WRAP_STDOUT].data) == b""
        assert bytes(collected[Channel.WRAP_STDERR].data) == b""

        assert bytes(collected[Channel.TOOL_PAYLOAD].data) == b"line-1\nline-2\n"
        assert bytes(collected[Channel.TOOL_STDOUT].data) == b"human note\n"

        envelope = json.loads(collected[Channel.TOOL_RESULT].text())
        assert envelope["bytes_out"] == len(b"line-1\nline-2\n")
        assert envelope["data"] == {"stdin_text": "abc", "note": "quiet"}

    def test_wrap_stderr_explains_sandbox_setup_failure(self, tmp_path: Path) -> None:
        payload_dir = _write_payload(tmp_path, _SMOKE_PAYLOAD)
        profile = _profile(payload_dir, rootfs="/nonexistent-rootfs-3f9c71")

        exit_code, collected = self._pump_raw(
            profile,
            PAYLOAD_COMMAND,
            args=b"{}",
            stdin=b"",
        )

        assert exit_code != 0
        assert bytes(collected[Channel.WRAP_STDERR].data)

        assert bytes(collected[Channel.TOOL_STDOUT].data) == b""
        assert bytes(collected[Channel.TOOL_STDERR].data) == b""
        assert bytes(collected[Channel.TOOL_PAYLOAD].data) == b""
        assert bytes(collected[Channel.TOOL_RESULT].data) == b""


@needs_bwrap
@needs_userns
class TestArgsFdAndSecrets:
    """Профиль уезжает --args FD, секреты — только каналом tool_args."""

    @staticmethod
    def _run_waiting_stage(
        tmp_path: Path,
        secret: str,
    ) -> tuple[WaitTrailer, bool, bool, bool]:
        """Прогон ждущего payload'а со сканом /proc в момент жизни стадии.

        Возвращает итог стадии и факты скана: стадия видна в cmdline,
        профильная env-проба в cmdline, секрет в cmdline либо environ.
        """
        sync = tmp_path / "sync"
        sync.mkdir(exist_ok=True)

        payload_dir = _write_payload(tmp_path, _WAIT_PAYLOAD)
        profile = _profile(payload_dir, rw_binds=(f"{sync}:/sync",))

        args: dict[str, JsonValue] = {
            "ready_path": "/sync/ready",
            "stop_path": "/sync/stop",
            "secret": secret,
        }

        results: list[WaitTrailer] = []
        errors: list[BaseException] = []

        def work() -> None:
            try:
                outcome = _run_stage(
                    profile,
                    contract=_contract(out=None, result=WaitTrailer),
                    args=args,
                    stdin=None,
                    payload=None,
                )
                results.append(outcome.trailer(STAGE_ID, WaitTrailer))
            except BaseException as exc:
                errors.append(exc)

        thread = threading.Thread(target=work)
        thread.start()

        try:
            _wait_for_file(sync / "ready")

            stage_seen = _seen_in_proc("cmdline", b"/opt/payload/main.py")
            profile_in_cmdline = _seen_in_proc("cmdline", Marker.PROFILE.encode())
            secret_leaked = _seen_in_proc("cmdline", secret.encode())
            if not secret_leaked:
                secret_leaked = _seen_in_proc("environ", secret.encode())
        finally:
            (sync / "stop").write_text("stop", encoding="utf-8")
            thread.join(timeout=30)

        assert not thread.is_alive()
        assert not errors, f"stage failed: {errors}"

        return results[0], stage_seen, profile_in_cmdline, secret_leaked

    def test_profile_travels_by_fd_and_env_arrives(self, tmp_path: Path) -> None:
        trailer, stage_seen, profile_in_cmdline, _leak = self._run_waiting_stage(
            tmp_path, Marker.SECRET.value
        )

        assert stage_seen, "scan must observe the running stage"
        assert not profile_in_cmdline, "profile must not be visible in ps"
        assert trailer.marker_env == Marker.PROFILE.value

    def test_secret_travels_only_via_tool_args(self, tmp_path: Path) -> None:
        trailer, stage_seen, _profile_seen, secret_leaked = self._run_waiting_stage(
            tmp_path, Marker.SECRET.value
        )

        assert stage_seen, "scan must observe the running stage"
        assert not secret_leaked, "secret must not appear in argv or environ"
        assert trailer.secret_seen == Marker.SECRET.value


@needs_fuse
@needs_userns
class TestImageChain:
    """Профиль с образом: wrap_args_inner доезжает, профиль не светится в ps."""

    @staticmethod
    def _image_profile(tmp_path: Path, template: Path, script: str) -> SandboxProfile:
        sync = tmp_path / "sync"
        sync.mkdir(exist_ok=True)

        payload_dir = _write_payload(tmp_path, script)

        return _profile(
            payload_dir,
            rw_binds=(f"{sync}:/sync",),
            rw_images=(f"{tmp_path}/ws/{{user_id}}.ext4:/workspace",),
            image_template=str(template),
            timeout_sec=60,
        )

    def test_wrap_args_inner_feeds_nested_bwrap(
        self, tmp_path: Path, template: Path
    ) -> None:
        profile = self._image_profile(tmp_path, template, _IMAGE_PAYLOAD)

        args: dict[str, JsonValue] = {
            "ready_path": "/sync/ready",
            "stop_path": "/sync/stop",
        }

        payload_sink = _CollectSink()
        results: list[ImageTrailer] = []
        errors: list[BaseException] = []

        def work() -> None:
            try:
                outcome = _run_stage(
                    profile,
                    contract=_contract(out=StreamFormat.TEXT, result=ImageTrailer),
                    args=args,
                    stdin=None,
                    payload=payload_sink,
                    path_vars=_image_path_vars,
                )
                results.append(outcome.trailer(STAGE_ID, ImageTrailer))
            except BaseException as exc:
                errors.append(exc)

        thread = threading.Thread(target=work)
        thread.start()

        sync = tmp_path / "sync"
        try:
            _wait_for_file(sync / "ready", timeout_sec=30.0)

            stage_seen = _seen_in_proc("cmdline", b"/opt/payload/main.py")
            profile_in_cmdline = _seen_in_proc("cmdline", Marker.PROFILE.encode())
        finally:
            (sync / "stop").write_text("stop", encoding="utf-8")
            thread.join(timeout=60)

        assert not thread.is_alive()
        assert not errors, f"stage failed: {errors}"

        assert stage_seen, "scan must observe the running stage"
        assert not profile_in_cmdline, "profile must not be visible on any chain step"

        trailer = results[0]
        assert trailer.marker_env == Marker.PROFILE.value
        assert trailer.workspace_file == "written by stage"

        assert bytes(payload_sink.data) == b"image-data\n"

        assert (tmp_path / "ws" / "7.ext4").is_file()

    def test_mount_failure_reaches_wrap_stderr(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.ext4"
        bad.write_bytes(b"not an ext4 image")

        profile = self._image_profile(tmp_path, bad, _ECHO_PAYLOAD)

        with pytest.raises(WorkflowError) as failure:
            _run_stage(
                profile,
                contract=_contract(out=None, result=EchoTrailer),
                args={"note": "never"},
                stdin=None,
                payload=None,
                path_vars=_image_path_vars,
            )

        # канальный режим: лаунчер пишет в wrap_stderr без маркеров, текст доезжает
        message = str(failure.value)
        assert "image not mounted" in message
        assert "fuse2fs" in message


@needs_bwrap
@needs_userns
class TestStageShutdown:
    """Отмена и таймаут: насос просыпается сразу, ресурсы освобождаются."""

    def test_cancel_wakes_pump_and_leaves_nothing(self, tmp_path: Path) -> None:
        payload_dir = _write_payload(tmp_path, _ECHO_PAYLOAD)
        profile = _profile(payload_dir)

        entry = ("sh", "-c", f"echo {Marker.CANCEL} && sleep 300")

        baseline = _open_fd_count()

        cancels: list[TurnCancellation] = []
        errors: list[BaseException] = []

        def work() -> None:
            with turn_cancellation() as cancellation:
                cancels.append(cancellation)
                try:
                    _run_stage(
                        profile,
                        contract=_contract(out=None, result=EmptyTrailer),
                        args={},
                        stdin=None,
                        payload=None,
                        entry=entry,
                    )
                except BaseException as exc:
                    errors.append(exc)

        thread = threading.Thread(target=work)
        thread.start()

        _wait_for_stage(Marker.CANCEL.encode())

        started = time.monotonic()
        cancels[0].cancel()
        thread.join(timeout=15)
        elapsed = time.monotonic() - started

        assert not thread.is_alive()
        woken = f"cancel must wake the pump immediately, took {elapsed:.2f}s"
        assert elapsed < 5.0, woken

        assert len(errors) == 1
        assert isinstance(errors[0], ToolStopped)

        _assert_stage_gone(Marker.CANCEL.encode())
        _assert_fds_restored(baseline)

    def test_stage_timeout_kills_and_cleans_up(self, tmp_path: Path) -> None:
        payload_dir = _write_payload(tmp_path, _ECHO_PAYLOAD)
        profile = _profile(payload_dir, timeout_sec=1)

        entry = ("sh", "-c", f"echo {Marker.TIMEOUT} && sleep 30")

        baseline = _open_fd_count()
        started = time.monotonic()

        with pytest.raises(WorkflowError, match="timed out"):
            _run_stage(
                profile,
                contract=_contract(out=None, result=EmptyTrailer),
                args={},
                stdin=None,
                payload=None,
                entry=entry,
            )

        elapsed = time.monotonic() - started
        fired = f"deadline must fire around timeout_sec, took {elapsed:.2f}s"
        assert elapsed < 20.0, fired

        _assert_stage_gone(Marker.TIMEOUT.encode())
        _assert_fds_restored(baseline)
