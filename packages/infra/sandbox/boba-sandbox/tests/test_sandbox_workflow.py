"""Этап 2 графа стадий: валидация, цепочка, веер, SIGPIPE, таймаут, отмена,
mount-группы с per-stage cgroup и per-stage сетью — на реальном bwrap."""

from __future__ import annotations

import os
import resource
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pydantic
import pytest
from journal_stand import JournalStand
from pydantic import BaseModel, ConfigDict, JsonValue

from boba.cancellation import ToolStopped, TurnCancellation, turn_cancellation
from boba.sandbox.cgroup import CgroupError, CgroupManager, GroupLimits
from boba.sandbox.profile import SandboxProfile
from boba.sandbox.workflow import (
    GroupIds,
    StageDef,
    StageRegistry,
    WorkflowRunner,
)
from boba.toolkit.channels import Channel, StreamFormat
from boba.toolkit.workflow import (
    EmptyTrailer,
    StageContract,
    WorkflowError,
    WorkflowSpec,
)
from boba.workspace.launcher import FUSE_DEVICE

REPO = Path(__file__).resolve().parents[5]
TOOLKIT_SRC = REPO / "packages" / "core" / "boba-toolkit" / "src"
SITE_PACKAGES = Path(pydantic.__file__).resolve().parents[1]

HOST_RO_BINDS: tuple[str, ...] = ("/usr", "/bin", "/sbin", "/lib", "/lib64")

PAYLOAD_ENTRY: tuple[str, ...] = ("python3.11", "/opt/payload/main.py")
PAYLOAD_NEEDLE = b"/opt/payload/main.py"

CHUNK = 64 * 1024

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

_UID = os.getuid()
_DELEGATED_PARENT = f"/sys/fs/cgroup/user.slice/user-{_UID}.slice/user@{_UID}.service"

needs_delegation = pytest.mark.skipif(
    not os.path.isdir(_DELEGATED_PARENT),
    reason="нет делегированной systemd user-зоны (cgroup v2)",
)


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
        "lock_wait_sec": 15.0,
        "copy_chunk_bytes": 1 << 20,
    },
    "tmpfs": ("/tmp:64M",),  # noqa: S108
    "network": False,
    "env_set": {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "PYTHONPATH": "/opt/src:/opt/site",
        "HOME": "/tmp",  # noqa: S108
        "LANG": "C.UTF-8",
    },
    "timeout_sec": 60,
    "max_memory_bytes": 512 * 1024 * 1024,
    "max_cpu_sec": 60,
    "max_file_size_bytes": 64 * 1024 * 1024,
    "max_open_files": 1024,
    "max_processes": 256,
    "max_output_bytes": 256 * 1024,
    "cgroup_base": "",
    "oom_score_adj": 0,
    "cwd": "/tmp",  # noqa: S108
}


_SRC_PAYLOAD = """
from boba.toolkit.payload import (
    PayloadChannels,
    PayloadLogging,
    PayloadOutputClosedError,
)
from pydantic import BaseModel


class Request(BaseModel):
    chunks: int


class Trailer(BaseModel):
    chunks: int


PayloadLogging.setup()
channels = PayloadChannels.open()
request = channels.args(Request)

stream = channels.payload()
block = b"x" * 65536

done = 0
try:
    for _ in range(request.chunks):
        stream.write(block)
        done += 1
    stream.flush()
except PayloadOutputClosedError:
    pass

channels.write_result(Trailer(chunks=done))
raise SystemExit(int(channels.exit_code()))
"""


_SINK_PAYLOAD = """
from boba.toolkit.payload import PayloadChannels, PayloadLogging
from pydantic import BaseModel


class Request(BaseModel):
    pass


class Trailer(BaseModel):
    got: int


PayloadLogging.setup()
channels = PayloadChannels.open()
channels.args(Request)

stream = channels.stdin()
total = 0
while True:
    block = stream.read(65536)
    if not block:
        break
    total += len(block)

channels.write_result(Trailer(got=total))
raise SystemExit(int(channels.exit_code()))
"""


_HEAD_PAYLOAD = """
from boba.toolkit.payload import PayloadChannels, PayloadLogging
from pydantic import BaseModel


class Request(BaseModel):
    pass


class Trailer(BaseModel):
    got: int


PayloadLogging.setup()
channels = PayloadChannels.open()
channels.args(Request)

stream = channels.stdin()
block = stream.read(65536)
stream.close()

channels.write_result(Trailer(got=len(block)))
raise SystemExit(int(channels.exit_code()))
"""


_HEAD_FAIL_PAYLOAD = """
from pydantic import BaseModel

from boba.toolkit.payload import PayloadChannels, PayloadLogging


class Request(BaseModel):
    pass


PayloadLogging.setup()
channels = PayloadChannels.open()
channels.args(Request)

stream = channels.stdin()
stream.read(65536)
stream.close()

raise SystemExit(3)
"""


_BAD_SINK_PAYLOAD = """
from pydantic import BaseModel

from boba.toolkit.payload import PayloadChannels, PayloadLogging


class Request(BaseModel):
    pass


PayloadLogging.setup()
channels = PayloadChannels.open()
channels.args(Request)
channels.stdin().read(4096)

raise SystemExit(2)
"""


_BINARY_SRC_PAYLOAD = """
import hashlib

from boba.toolkit.payload import PayloadChannels, PayloadLogging
from pydantic import BaseModel


class Request(BaseModel):
    frames: int


class Trailer(BaseModel):
    digest: str
    size: int


PayloadLogging.setup()
channels = PayloadChannels.open()
request = channels.args(Request)

# каждый кадр на байт короче предыдущего: многобайтовая последовательность
# гуляет по фазе и рвётся на границе порции насоса (READ_BYTES = 65536)
emoji = "\\U0001F600".encode("utf-8")
filler = bytes(range(256))

stream = channels.payload()
digest = hashlib.sha256()
size = 0

for index in range(request.frames):
    head = (filler * 256)[: 65534 - index]
    frame = head + emoji
    stream.write(frame)
    digest.update(frame)
    size += len(frame)

stream.flush()

channels.write_result(Trailer(digest=digest.hexdigest(), size=size))
raise SystemExit(int(channels.exit_code()))
"""


_BINARY_SINK_PAYLOAD = """
import hashlib

from boba.toolkit.payload import PayloadChannels, PayloadLogging
from pydantic import BaseModel


class Request(BaseModel):
    pass


class Trailer(BaseModel):
    digest: str
    size: int
    utf8: bool


PayloadLogging.setup()
channels = PayloadChannels.open()
channels.args(Request)

stream = channels.stdin()
digest = hashlib.sha256()
body = bytearray()

while True:
    block = stream.read(65536)
    if not block:
        break
    digest.update(block)
    body += block

utf8 = True
try:
    bytes(body).decode("utf-8")
except UnicodeDecodeError:
    utf8 = False

channels.write_result(
    Trailer(digest=digest.hexdigest(), size=len(body), utf8=utf8)
)
raise SystemExit(int(channels.exit_code()))
"""


_JSON_SINK_PAYLOAD = """
import json

from boba.toolkit.payload import PayloadChannels, PayloadLogging
from pydantic import BaseModel


class Request(BaseModel):
    pass


PayloadLogging.setup()
channels = PayloadChannels.open()
channels.args(Request)

stream = channels.stdin()
body = bytearray()
while True:
    block = stream.read(65536)
    if not block:
        break
    body += block

try:
    json.loads(bytes(body))
except (UnicodeDecodeError, ValueError):
    raise SystemExit(4)

raise SystemExit(0)
"""


_LIAR_PAYLOAD = """
import os

from pydantic import BaseModel

from boba.toolkit.channels import Channel
from boba.toolkit.payload import PayloadChannels, PayloadLogging


class Request(BaseModel):
    pass


PayloadLogging.setup()
channels = PayloadChannels.open()
channels.args(Request)

stream = channels.payload()
stream.write(b"x" * 65536)
stream.flush()

fd = int(os.environ[Channel.TOOL_RESULT.env_name])
os.write(fd, b'{"bytes_out": 1, "data": {"chunks": 1}}' + b"\\n")
os.close(fd)
raise SystemExit(0)
"""


_NAP_PAYLOAD = """
import os
import time

from boba.toolkit.payload import PayloadChannels, PayloadLogging
from pydantic import BaseModel


class Request(BaseModel):
    ready_path: str
    stop_path: str
    hold_sec: float


class Trailer(BaseModel):
    ok: bool


PayloadLogging.setup()
channels = PayloadChannels.open()
request = channels.args(Request)

with open(request.ready_path, "w", encoding="utf-8") as ready:
    ready.write("ready")

deadline = time.monotonic() + request.hold_sec
while time.monotonic() < deadline:
    if os.path.exists(request.stop_path):
        break
    time.sleep(0.05)

channels.write_result(Trailer(ok=True))
raise SystemExit(int(channels.exit_code()))
"""


_GROUP_WRITER_PAYLOAD = """
from pathlib import Path

from boba.toolkit.payload import PayloadChannels, PayloadLogging
from pydantic import BaseModel


class Request(BaseModel):
    chunks: int


class Trailer(BaseModel):
    sent: int


PayloadLogging.setup()
channels = PayloadChannels.open()
request = channels.args(Request)

Path("/workspace/marker.txt").write_text("shared-workspace-proof", encoding="utf-8")

stream = channels.payload()
block = b"y" * 65536
count = request.chunks
for _ in range(count):
    stream.write(block)
stream.flush()

channels.write_result(Trailer(sent=count * 65536))
raise SystemExit(int(channels.exit_code()))
"""


_GROUP_READER_PAYLOAD = """
from pathlib import Path

from boba.toolkit.payload import PayloadChannels, PayloadLogging
from pydantic import BaseModel


class Request(BaseModel):
    pass


class Trailer(BaseModel):
    got: int
    marker: str


PayloadLogging.setup()
channels = PayloadChannels.open()
channels.args(Request)

stream = channels.stdin()
total = 0
while True:
    block = stream.read(65536)
    if not block:
        break
    total += len(block)

marker = Path("/workspace/marker.txt").read_text(encoding="utf-8")

channels.write_result(Trailer(got=total, marker=marker))
raise SystemExit(int(channels.exit_code()))
"""


_IFACE_PAYLOAD = """
import socket

from boba.toolkit.payload import PayloadChannels, PayloadLogging
from pydantic import BaseModel


class Request(BaseModel):
    pass


class Trailer(BaseModel):
    ifaces: int


PayloadLogging.setup()
channels = PayloadChannels.open()
channels.args(Request)

channels.write_result(Trailer(ifaces=len(socket.if_nameindex())))
raise SystemExit(int(channels.exit_code()))
"""


_OK_PAYLOAD = """
from boba.toolkit.payload import PayloadChannels, PayloadLogging
from pydantic import BaseModel


class Request(BaseModel):
    pass


class Trailer(BaseModel):
    ok: bool


PayloadLogging.setup()
channels = PayloadChannels.open()
channels.args(Request)

channels.write_result(Trailer(ok=True))
raise SystemExit(int(channels.exit_code()))
"""


_WS_WRITE_PAYLOAD = """
from pathlib import Path

from boba.toolkit.payload import PayloadChannels, PayloadLogging
from pydantic import BaseModel


class Request(BaseModel):
    pass


class Trailer(BaseModel):
    ok: bool


PayloadLogging.setup()
channels = PayloadChannels.open()
channels.args(Request)

Path("/workspace/single.txt").write_text("single", encoding="utf-8")

channels.write_result(Trailer(ok=True))
raise SystemExit(int(channels.exit_code()))
"""


_HOG_PAYLOAD = """
from boba.toolkit.payload import PayloadChannels, PayloadLogging
from pydantic import BaseModel


class Request(BaseModel):
    pass


class Trailer(BaseModel):
    ok: bool


PayloadLogging.setup()
channels = PayloadChannels.open()
channels.args(Request)

hog = []
for _ in range(192):
    hog.append(bytearray(1024 * 1024))

channels.write_result(Trailer(ok=True))
raise SystemExit(int(channels.exit_code()))
"""


class SrcTrailer(BaseModel):
    """Квитанция источника: сколько блоков ушло в tool_payload."""

    chunks: int


class SinkTrailer(BaseModel):
    """Квитанция приёмника: объём прочитанного входа."""

    got: int


class HeadTrailer(BaseModel):
    """Квитанция head-потребителя: сколько байт прочитано до закрытия."""

    got: int


class OkTrailer(BaseModel):
    """Квитанция стадии без данных: только факт успеха."""

    ok: bool


class BinarySrcTrailer(BaseModel):
    """Квитанция бинарного источника: отпечаток и объём отданного потока."""

    digest: str
    size: int


class BinarySinkTrailer(BaseModel):
    """Квитанция бинарного приёмника: отпечаток входа и его декодируемость."""

    digest: str
    size: int
    utf8: bool


class SentTrailer(BaseModel):
    """Квитанция группового писателя: объём отданного потока."""

    sent: int


class GotMarkerTrailer(BaseModel):
    """Квитанция группового читателя: объём потока и файл общего workspace."""

    got: int
    marker: str


class IfaceTrailer(BaseModel):
    """Квитанция сетевой пробы: число видимых интерфейсов."""

    ifaces: int


class AnyRequest(BaseModel):
    """Запрос тестового узла: любые пользовательские поля проходят насквозь."""

    model_config = ConfigDict(extra="allow")


def _identity_args(args: Mapping[str, JsonValue], /) -> Mapping[str, JsonValue]:
    return dict(args)


def _allow_all(tool: str, /) -> bool:
    return True


def _deny_all(tool: str, /) -> bool:
    return False


def _write_payload(root: Path, name: str, script: str) -> Path:
    payload_dir = root / "payloads" / name
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


def _stage_def(
    payload_dir: Path,
    contract: StageContract,
    **profile_kw: Any,
) -> StageDef:
    return StageDef(
        contract=contract,
        profile=_profile(payload_dir, **profile_kw),
        entry=PAYLOAD_ENTRY,
        request=AnyRequest,
        enrich=_identity_args,
    )


def _src_def(root: Path, **profile_kw: Any) -> StageDef:
    contract = StageContract(out=StreamFormat.TEXT, result=SrcTrailer)

    return _stage_def(_write_payload(root, "src", _SRC_PAYLOAD), contract, **profile_kw)


def _sink_def(root: Path, **profile_kw: Any) -> StageDef:
    contract = StageContract(out=None, result=SinkTrailer)

    return _stage_def(
        _write_payload(root, "sink", _SINK_PAYLOAD), contract, **profile_kw
    )


def _ok_def(root: Path, **profile_kw: Any) -> StageDef:
    """Узел, который вход не читает вовсе: ни stdin(), ни payload()."""
    contract = StageContract(out=None, result=OkTrailer)

    return _stage_def(_write_payload(root, "ok", _OK_PAYLOAD), contract, **profile_kw)


def _runner(registry: StageRegistry) -> WorkflowRunner:
    return WorkflowRunner(registry, _allow_all, dict, JournalStand.journal())


def _proc_files(name: str) -> list[bytes]:
    """Читаемые файлы /proc/PID/{name} всех процессов; чужие молча пропускаются."""
    blobs: list[bytes] = []

    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue

        try:
            blobs.append((Path("/proc") / entry / name).read_bytes())
        except OSError:
            continue

    return blobs


def _seen_in_proc(name: str, needle: bytes) -> bool:
    return any(needle in blob for blob in _proc_files(name))


def _wait_for_file(path: Path, timeout_sec: float = 20.0) -> None:
    deadline = time.monotonic() + timeout_sec

    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(0.05)

    raise AssertionError(f"file did not appear in time: {path}")


def _assert_stages_gone(timeout_sec: float = 10.0) -> None:
    deadline = time.monotonic() + timeout_sec

    while time.monotonic() < deadline:
        if not _seen_in_proc("cmdline", PAYLOAD_NEEDLE):
            return
        time.sleep(0.1)

    raise AssertionError("stage processes are still alive")


def _open_fd_count() -> int:
    return len(os.listdir("/proc/self/fd"))


def _assert_fds_restored(baseline: int, timeout_sec: float = 5.0) -> None:
    deadline = time.monotonic() + timeout_sec

    while time.monotonic() < deadline:
        if _open_fd_count() <= baseline:
            return
        time.sleep(0.05)

    raise AssertionError(f"descriptors leaked: was {baseline}, now {_open_fd_count()}")


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


class TestWorkflowValidation:
    """Невалидная спека отвергается до старта какой-либо стадии."""

    def test_cycle_rejected(self) -> None:
        with pytest.raises(WorkflowError, match="cycle"):
            WorkflowSpec.parse(
                {
                    "nodes": [
                        {"id": "a", "tool": "src", "args": {}},
                        {"id": "b", "tool": "sink", "args": {}},
                    ],
                    "edges": [
                        {"src": "a", "dst": "b"},
                        {"src": "b", "dst": "a"},
                    ],
                }
            )

    def test_two_incoming_edges_rejected(self) -> None:
        with pytest.raises(WorkflowError, match="more than one incoming"):
            WorkflowSpec.parse(
                {
                    "nodes": [
                        {"id": "a", "tool": "src", "args": {}},
                        {"id": "b", "tool": "src", "args": {}},
                        {"id": "c", "tool": "sink", "args": {}},
                    ],
                    "edges": [
                        {"src": "a", "dst": "c"},
                        {"src": "b", "dst": "c"},
                    ],
                }
            )

    def test_dot_in_stage_id_rejected(self) -> None:
        """Точка в id ломает разбор имён файлов журнала — отвергается на границе."""
        with pytest.raises(WorkflowError, match="unsafe characters"):
            WorkflowSpec.parse(
                {"nodes": [{"id": "a.b", "tool": "src", "args": {}}]}
            )

    def test_unknown_tool_rejected(self, tmp_path: Path) -> None:
        runner = _runner(StageRegistry({"src": _src_def(tmp_path)}))

        spec = WorkflowSpec.parse(
            {"nodes": [{"id": "a", "tool": "nope", "args": {}}]}
        )

        with pytest.raises(WorkflowError, match="unknown workflow tool"):
            runner.run(spec, JournalStand.context())

    def test_denied_tool_rejected(self, tmp_path: Path) -> None:
        """Deny by default: без права на инструмент граф не стартует."""
        registry = StageRegistry({"src": _src_def(tmp_path)})
        runner = WorkflowRunner(registry, _deny_all, dict, JournalStand.journal())

        spec = WorkflowSpec.parse(
            {"nodes": [{"id": "a", "tool": "src", "args": {"chunks": 1}}]}
        )

        with pytest.raises(WorkflowError, match="not allowed"):
            runner.run(spec, JournalStand.context())

    def test_fd_budget_checked_before_pipes(self, tmp_path: Path) -> None:
        """Нехватка RLIMIT_NOFILE — внятная ошибка валидации, не сбой os.pipe()."""
        registry = StageRegistry(
            {"src": _src_def(tmp_path), "sink": _sink_def(tmp_path)}
        )
        runner = _runner(registry)

        spec = WorkflowSpec.parse(
            {
                "nodes": [
                    {"id": "a", "tool": "src", "args": {"chunks": 1}},
                    {"id": "b", "tool": "sink", "args": {}},
                ],
                "edges": [{"src": "a", "dst": "b"}],
            }
        )

        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        cap = min(hard, _open_fd_count() + 40)

        resource.setrlimit(resource.RLIMIT_NOFILE, (cap, hard))
        try:
            with pytest.raises(WorkflowError, match="RLIMIT_NOFILE"):
                runner.run(spec, JournalStand.context())
        finally:
            resource.setrlimit(resource.RLIMIT_NOFILE, (soft, hard))


@needs_bwrap
@needs_userns
class TestChainAndFanout:
    """Цепочка и веер: потоковость, квитанции всех стадий, сбой потребителя."""

    def test_chain_streams_and_collects_all_receipts(self, tmp_path: Path) -> None:
        """8 MiB через буфер ребра 1 MiB проходят только потоковой перекачкой."""
        registry = StageRegistry(
            {"src": _src_def(tmp_path), "sink": _sink_def(tmp_path)}
        )
        runner = _runner(registry)

        baseline = _open_fd_count()

        spec = WorkflowSpec.parse(
            {
                "nodes": [
                    {"id": "a", "tool": "src", "args": {"chunks": 128}},
                    {"id": "b", "tool": "sink", "args": {}},
                ],
                "edges": [{"src": "a", "dst": "b"}],
            }
        )
        outcome = runner.run(spec, JournalStand.context())

        a = outcome.trailer("a", SrcTrailer)
        b = outcome.trailer("b", SinkTrailer)

        assert a.chunks == 128
        assert b.got == 128 * CHUNK

        assert outcome.outcome_of("a").exit_code == 0
        assert outcome.outcome_of("b").exit_code == 0

        # промежуточный поток в итог не попадает: только квитанции стадий
        assert set(outcome.trailers) == {"a", "b"}

        _assert_fds_restored(baseline)

    def test_edge_from_stage_without_product_is_empty(self, tmp_path: Path) -> None:
        """Узел без объявленного продукта тоже кормит ребро: приёмник видит EOF."""
        registry = StageRegistry({"sink": _sink_def(tmp_path)})
        runner = _runner(registry)

        baseline = _open_fd_count()

        spec = WorkflowSpec.parse(
            {
                "nodes": [
                    {"id": "a", "tool": "sink", "args": {}, "stdin": "seed"},
                    {"id": "b", "tool": "sink", "args": {}},
                ],
                "edges": [{"src": "a", "dst": "b"}],
            }
        )
        outcome = runner.run(spec, JournalStand.context())

        assert outcome.trailer("a", SinkTrailer).got == len("seed")
        assert outcome.trailer("b", SinkTrailer).got == 0

        assert outcome.outcome_of("a").exit_code == 0
        assert outcome.outcome_of("b").exit_code == 0

        _assert_fds_restored(baseline)

    def test_edge_into_stage_that_never_reads_is_a_success(
        self, tmp_path: Path
    ) -> None:
        """Ребро ведёт в любой узел: непрочитанный вход — не сбой графа.

        Источник упирается в буфер ребра и уходит по SIGPIPE; здоровый
        потребитель делает rc 141 успехом по общему правилу.
        """
        registry = StageRegistry({"src": _src_def(tmp_path), "ok": _ok_def(tmp_path)})
        runner = _runner(registry)

        baseline = _open_fd_count()

        spec = WorkflowSpec.parse(
            {
                "nodes": [
                    {"id": "a", "tool": "src", "args": {"chunks": 128}},
                    {"id": "b", "tool": "ok", "args": {}},
                ],
                "edges": [{"src": "a", "dst": "b"}],
            }
        )
        outcome = runner.run(spec, JournalStand.context())

        assert outcome.trailer("b", OkTrailer).ok is True

        assert outcome.outcome_of("a").exit_code == 141
        assert outcome.outcome_of("b").exit_code == 0

        _assert_fds_restored(baseline)

    def test_stdin_literal_is_allowed_for_any_stage(self, tmp_path: Path) -> None:
        """Литерал stdin подаётся и узлу, который поток не читает."""
        registry = StageRegistry({"ok": _ok_def(tmp_path)})
        runner = _runner(registry)

        baseline = _open_fd_count()

        spec = WorkflowSpec.parse(
            {"nodes": [{"id": "a", "tool": "ok", "args": {}, "stdin": "seed"}]}
        )
        outcome = runner.run(spec, JournalStand.context())

        assert outcome.trailer("a", OkTrailer).ok is True
        assert outcome.outcome_of("a").exit_code == 0

        _assert_fds_restored(baseline)

    def test_fanout_feeds_every_consumer(self, tmp_path: Path) -> None:
        registry = StageRegistry(
            {"src": _src_def(tmp_path), "sink": _sink_def(tmp_path)}
        )
        runner = _runner(registry)

        baseline = _open_fd_count()

        spec = WorkflowSpec.parse(
            {
                "nodes": [
                    {"id": "a", "tool": "src", "args": {"chunks": 32}},
                    {"id": "b", "tool": "sink", "args": {}},
                    {"id": "c", "tool": "sink", "args": {}},
                ],
                "edges": [
                    {"src": "a", "dst": "b"},
                    {"src": "a", "dst": "c"},
                ],
            }
        )
        outcome = runner.run(spec, JournalStand.context())

        # источник отработал один раз: одна квитанция на весь веер
        assert outcome.trailer("a", SrcTrailer).chunks == 32
        assert outcome.trailer("b", SinkTrailer).got == 32 * CHUNK
        assert outcome.trailer("c", SinkTrailer).got == 32 * CHUNK

        _assert_fds_restored(baseline)

    def test_failing_consumer_fails_graph(self, tmp_path: Path) -> None:
        bad_contract = StageContract(out=None, result=EmptyTrailer)
        bad_dir = _write_payload(tmp_path, "bad_sink", _BAD_SINK_PAYLOAD)
        registry = StageRegistry(
            {
                "src": _src_def(tmp_path),
                "sink": _sink_def(tmp_path),
                "bad_sink": _stage_def(bad_dir, bad_contract),
            }
        )
        runner = _runner(registry)

        baseline = _open_fd_count()

        spec = WorkflowSpec.parse(
            {
                "nodes": [
                    {"id": "a", "tool": "src", "args": {"chunks": 64}},
                    {"id": "b", "tool": "sink", "args": {}},
                    {"id": "c", "tool": "bad_sink", "args": {}},
                ],
                "edges": [
                    {"src": "a", "dst": "b"},
                    {"src": "a", "dst": "c"},
                ],
            }
        )

        with pytest.raises(WorkflowError, match="stage c exited with code 2"):
            runner.run(spec, JournalStand.context())

        _assert_stages_gone()
        _assert_fds_restored(baseline)

    def test_fanout_survives_one_departed_consumer(self, tmp_path: Path) -> None:
        """EPIPE источнику — только после ухода всех потребителей веера: второй
        потребитель дочитывает весь поток, источник заканчивает rc 0."""
        head_contract = StageContract(out=None, result=HeadTrailer)
        head_dir = _write_payload(tmp_path, "head", _HEAD_PAYLOAD)
        registry = StageRegistry(
            {
                "src": _src_def(tmp_path),
                "head": _stage_def(head_dir, head_contract),
                "sink": _sink_def(tmp_path),
            }
        )
        runner = _runner(registry)

        baseline = _open_fd_count()

        spec = WorkflowSpec.parse(
            {
                "nodes": [
                    {"id": "a", "tool": "src", "args": {"chunks": 64}},
                    {"id": "h", "tool": "head", "args": {}},
                    {"id": "b", "tool": "sink", "args": {}},
                ],
                "edges": [
                    {"src": "a", "dst": "h"},
                    {"src": "a", "dst": "b"},
                ],
            }
        )
        outcome = runner.run(spec, JournalStand.context())

        assert outcome.outcome_of("a").exit_code == 0
        assert outcome.trailer("a", SrcTrailer).chunks == 64
        assert outcome.trailer("b", SinkTrailer).got == 64 * CHUNK
        assert outcome.trailer("h", HeadTrailer).got == CHUNK

        _assert_fds_restored(baseline)

    def test_bytes_out_mismatch_fails_stage(self, tmp_path: Path) -> None:
        """Конверт врёт про bytes_out при rc 0 — сверка с насосом валит граф."""
        liar_contract = StageContract(out=StreamFormat.TEXT, result=SrcTrailer)
        liar_dir = _write_payload(tmp_path, "liar", _LIAR_PAYLOAD)
        registry = StageRegistry(
            {
                "liar": _stage_def(liar_dir, liar_contract),
                "sink": _sink_def(tmp_path),
            }
        )
        runner = _runner(registry)

        baseline = _open_fd_count()

        spec = WorkflowSpec.parse(
            {
                "nodes": [
                    {"id": "a", "tool": "liar", "args": {}},
                    {"id": "b", "tool": "sink", "args": {}},
                ],
                "edges": [{"src": "a", "dst": "b"}],
            }
        )

        with pytest.raises(WorkflowError, match="bytes_out mismatch"):
            runner.run(spec, JournalStand.context())

        _assert_stages_gone()
        _assert_fds_restored(baseline)


@needs_bwrap
@needs_userns
class TestByteTransparency:
    """По ребру текут сырые байты: путь данных ничего не декодирует."""

    @staticmethod
    def _binary_src(tmp_path: Path) -> StageDef:
        contract = StageContract(out=StreamFormat.BYTES, result=BinarySrcTrailer)
        payload_dir = _write_payload(tmp_path, "bin_src", _BINARY_SRC_PAYLOAD)

        return _stage_def(payload_dir, contract)

    def test_binary_stream_survives_the_edge_byte_for_byte(
        self, tmp_path: Path
    ) -> None:
        """Поток с 0x80..0xFF и рваным utf-8 приходит с тем же sha256."""
        sink_contract = StageContract(out=None, result=BinarySinkTrailer)
        sink_dir = _write_payload(tmp_path, "bin_sink", _BINARY_SINK_PAYLOAD)
        registry = StageRegistry(
            {
                "bin_src": self._binary_src(tmp_path),
                "bin_sink": _stage_def(sink_dir, sink_contract),
            }
        )
        runner = _runner(registry)

        baseline = _open_fd_count()

        spec = WorkflowSpec.parse(
            {
                "nodes": [
                    {"id": "a", "tool": "bin_src", "args": {"frames": 32}},
                    {"id": "b", "tool": "bin_sink", "args": {}},
                ],
                "edges": [{"src": "a", "dst": "b"}],
            }
        )
        outcome = runner.run(spec, JournalStand.context())

        sent = outcome.trailer("a", BinarySrcTrailer)
        got = outcome.trailer("b", BinarySinkTrailer)

        assert got.size == sent.size
        assert got.digest == sent.digest

        # данные заведомо ломаются любым decode: поток дошёл сырым
        assert got.utf8 is False

        _assert_fds_restored(baseline)

    def test_garbage_format_fails_the_stage_and_the_graph(self, tmp_path: Path) -> None:
        """Мусорный для приёмника поток отвергается рантаймом, а не валидацией."""
        json_contract = StageContract(out=None, result=EmptyTrailer)
        json_dir = _write_payload(tmp_path, "json_sink", _JSON_SINK_PAYLOAD)
        registry = StageRegistry(
            {
                "bin_src": self._binary_src(tmp_path),
                "json_sink": _stage_def(json_dir, json_contract),
            }
        )
        runner = _runner(registry)

        baseline = _open_fd_count()

        spec = WorkflowSpec.parse(
            {
                "nodes": [
                    {"id": "a", "tool": "bin_src", "args": {"frames": 2}},
                    {"id": "b", "tool": "json_sink", "args": {}},
                ],
                "edges": [{"src": "a", "dst": "b"}],
            }
        )

        with pytest.raises(WorkflowError, match="stage b exited with code 4"):
            runner.run(spec, JournalStand.context())

        _assert_stages_gone()
        _assert_fds_restored(baseline)


@needs_bwrap
@needs_userns
class TestSigpipeRule:
    """rc 141 источника: успех при ушедшем здоровом потребителе, иначе ошибка."""

    @staticmethod
    def _head_registry(tmp_path: Path, script: str, name: str) -> StageRegistry:
        contract = StageContract(out=None, result=HeadTrailer)
        payload_dir = _write_payload(tmp_path, name, script)

        return StageRegistry(
            {
                "src": _src_def(tmp_path),
                name: _stage_def(payload_dir, contract),
            }
        )

    def test_head_consumer_makes_141_success(self, tmp_path: Path) -> None:
        """Потребитель-head закрыл вход и вышел rc 0: источник 141 успешен."""
        runner = _runner(self._head_registry(tmp_path, _HEAD_PAYLOAD, "head"))

        baseline = _open_fd_count()

        spec = WorkflowSpec.parse(
            {
                "nodes": [
                    {"id": "a", "tool": "src", "args": {"chunks": 128}},
                    {"id": "h", "tool": "head", "args": {}},
                ],
                "edges": [{"src": "a", "dst": "h"}],
            }
        )
        outcome = runner.run(spec, JournalStand.context())

        assert outcome.outcome_of("a").exit_code == 141
        assert outcome.outcome_of("h").exit_code == 0

        # квитанция источника записана несмотря на EPIPE канала данных
        src = outcome.trailer("a", SrcTrailer)
        assert 1 <= src.chunks <= 128

        assert outcome.trailer("h", HeadTrailer).got == CHUNK

        _assert_fds_restored(baseline)

    def test_141_with_failed_consumer_is_error(self, tmp_path: Path) -> None:
        """Виновник — потребитель с настоящим rc, а не источник с законным 141."""
        runner = _runner(
            self._head_registry(tmp_path, _HEAD_FAIL_PAYLOAD, "head_fail")
        )

        baseline = _open_fd_count()

        spec = WorkflowSpec.parse(
            {
                "nodes": [
                    {"id": "a", "tool": "src", "args": {"chunks": 128}},
                    {"id": "hf", "tool": "head_fail", "args": {}},
                ],
                "edges": [{"src": "a", "dst": "hf"}],
            }
        )

        with pytest.raises(WorkflowError, match=r"stage hf exited with code 3"):
            runner.run(spec, JournalStand.context())

        _assert_stages_gone()
        _assert_fds_restored(baseline)


@needs_bwrap
@needs_userns
class TestGraphShutdown:
    """Таймаут и отмена: граф валится нужной причиной, ресурсы освобождаются."""

    @staticmethod
    def _nap_registry(tmp_path: Path, sync: Path) -> StageRegistry:
        contract = StageContract(out=None, result=OkTrailer)
        payload_dir = _write_payload(tmp_path, "nap", _NAP_PAYLOAD)
        binds = (f"{sync}:/sync",)

        return StageRegistry(
            {
                "nap_short": _stage_def(
                    payload_dir, contract, rw_binds=binds, timeout_sec=1
                ),
                "nap_long": _stage_def(
                    payload_dir, contract, rw_binds=binds, timeout_sec=60
                ),
            }
        )

    @staticmethod
    def _nap_args(name: str, hold_sec: float) -> dict[str, JsonValue]:
        return {
            "ready_path": f"/sync/{name}.ready",
            "stop_path": "/sync/never",
            "hold_sec": hold_sec,
        }

    def test_stage_timeout_fails_graph_with_its_cause(self, tmp_path: Path) -> None:
        sync = tmp_path / "sync"
        sync.mkdir(exist_ok=True)

        runner = _runner(self._nap_registry(tmp_path, sync))

        baseline = _open_fd_count()
        started = time.monotonic()

        spec = WorkflowSpec.parse(
            {
                "nodes": [
                    {
                        "id": "peer",
                        "tool": "nap_long",
                        "args": self._nap_args("peer", 120.0),
                    },
                    {
                        "id": "slow",
                        "tool": "nap_short",
                        "args": self._nap_args("slow", 120.0),
                    },
                ],
            }
        )

        with pytest.raises(WorkflowError) as failure:
            runner.run(spec, JournalStand.context())

        elapsed = time.monotonic() - started
        assert elapsed < 30.0, f"deadline must fire in about 1s, took {elapsed:.2f}s"

        message = str(failure.value)
        assert "workflow stage slow timed out" in message
        assert "workflow stage peer" not in message, "cascaded stage must not be blamed"

        _assert_stages_gone()
        _assert_fds_restored(baseline)

    def test_cancel_kills_all_stages(self, tmp_path: Path) -> None:
        sync = tmp_path / "sync"
        sync.mkdir(exist_ok=True)

        runner = _runner(self._nap_registry(tmp_path, sync))

        spec = WorkflowSpec.parse(
            {
                "nodes": [
                    {
                        "id": "n1",
                        "tool": "nap_long",
                        "args": self._nap_args("n1", 120.0),
                    },
                    {
                        "id": "n2",
                        "tool": "nap_long",
                        "args": self._nap_args("n2", 120.0),
                    },
                ],
            }
        )

        baseline = _open_fd_count()

        cancels: list[TurnCancellation] = []
        errors: list[BaseException] = []

        def work() -> None:
            with turn_cancellation() as cancellation:
                cancels.append(cancellation)
                try:
                    runner.run(spec, JournalStand.context())
                except BaseException as exc:
                    errors.append(exc)

        thread = threading.Thread(target=work)
        thread.start()

        try:
            _wait_for_file(sync / "n1.ready")
            _wait_for_file(sync / "n2.ready")
        finally:
            started = time.monotonic()
            cancels[0].cancel()
            thread.join(timeout=15)

        elapsed = time.monotonic() - started
        assert not thread.is_alive()
        assert elapsed < 5.0, f"cancel must wake the pump now, took {elapsed:.2f}s"

        assert len(errors) == 1
        assert isinstance(errors[0], ToolStopped)

        _assert_stages_gone()
        _assert_fds_restored(baseline)


@needs_fuse
@needs_userns
class TestMountGroup:
    """Mount-группа: общий /workspace, стрим без диска, wrap_result, сеть и flock."""

    @staticmethod
    def _image_kw(tmp_path: Path, template: Path, **extra: Any) -> dict[str, Any]:
        kw: dict[str, Any] = {
            "rw_images": (f"{tmp_path}/ws/shared.ext4:/workspace",),
            "image_template": str(template),
        }
        kw.update(extra)

        return kw

    def test_group_streams_over_shared_workspace(
        self, tmp_path: Path, template: Path
    ) -> None:
        """4 MiB между стадиями группы текут ребром одновременно на одном
        монтировании: сериализация на flock дала бы дедлок, не результат."""
        writer_contract = StageContract(
            out=StreamFormat.TEXT, result=SentTrailer
        )
        reader_contract = StageContract(
            out=None, result=GotMarkerTrailer
        )
        image_kw = self._image_kw(tmp_path, template)
        registry = StageRegistry(
            {
                "gw": _stage_def(
                    _write_payload(tmp_path, "gw", _GROUP_WRITER_PAYLOAD),
                    writer_contract,
                    **image_kw,
                ),
                "gr": _stage_def(
                    _write_payload(tmp_path, "gr", _GROUP_READER_PAYLOAD),
                    reader_contract,
                    **image_kw,
                ),
            }
        )
        runner = _runner(registry)

        baseline = _open_fd_count()

        spec = WorkflowSpec.parse(
            {
                "nodes": [
                    {"id": "w", "tool": "gw", "args": {"chunks": 64}},
                    {"id": "r", "tool": "gr", "args": {}},
                ],
                "edges": [{"src": "w", "dst": "r"}],
            }
        )
        outcome = runner.run(spec, JournalStand.context())

        # rc обеих стадий приехали строками wrap_result, иначе итог не собрался бы
        assert outcome.outcome_of("w").exit_code == 0
        assert outcome.outcome_of("r").exit_code == 0

        assert outcome.trailer("w", SentTrailer).sent == 64 * CHUNK

        reader = outcome.trailer("r", GotMarkerTrailer)
        assert reader.got == 64 * CHUNK
        assert reader.marker == "shared-workspace-proof"

        # wrap-каналы группы стадиям не принадлежат: их журнал идёт под id группы
        group_channels: set[Channel] = set()
        stages: set[str] = set()
        for key in outcome.journals:
            stages.add(key.stage)
            if key.stage.startswith(GroupIds.PREFIX):
                group_channels.add(key.channel)

        assert stages == {"w", "r", "g1"}
        assert Channel.WRAP_RESULT in group_channels
        assert Channel.WRAP_STDERR in group_channels

        _assert_fds_restored(baseline)

    def test_mixed_network_isolation_per_stage(
        self, tmp_path: Path, template: Path
    ) -> None:
        """Сеть группы — OR стадий, изоляция остаётся per-stage: безсетевой
        стадии вложенный bwrap добавляет --unshare-net."""
        contract = StageContract(out=None, result=IfaceTrailer)
        payload_dir = _write_payload(tmp_path, "iface", _IFACE_PAYLOAD)
        registry = StageRegistry(
            {
                "probe_net": _stage_def(
                    payload_dir,
                    contract,
                    **self._image_kw(tmp_path, template, network=True),
                ),
                "probe_dark": _stage_def(
                    payload_dir,
                    contract,
                    **self._image_kw(tmp_path, template, network=False),
                ),
            }
        )
        runner = _runner(registry)

        spec = WorkflowSpec.parse(
            {
                "nodes": [
                    {"id": "net", "tool": "probe_net", "args": {}},
                    {"id": "dark", "tool": "probe_dark", "args": {}},
                ],
            }
        )
        outcome = runner.run(spec, JournalStand.context())

        dark = outcome.trailer("dark", IfaceTrailer)
        net = outcome.trailer("net", IfaceTrailer)

        assert dark.ifaces == 1, "netless stage must see only its own loopback"
        assert net.ifaces >= 2, "networked stage must see the shared network"

    def test_single_call_serializes_with_group_on_flock(
        self, tmp_path: Path, template: Path
    ) -> None:
        """Одиночный вызов на том же образе ждёт flock, пока группа жива."""
        sync = tmp_path / "sync"
        sync.mkdir(exist_ok=True)

        nap_contract = StageContract(out=None, result=OkTrailer)
        ok_contract = StageContract(out=None, result=OkTrailer)
        image_kw = self._image_kw(tmp_path, template, rw_binds=(f"{sync}:/sync",))
        registry = StageRegistry(
            {
                "hold": _stage_def(
                    _write_payload(tmp_path, "hold", _NAP_PAYLOAD),
                    nap_contract,
                    **image_kw,
                ),
                "quick": _stage_def(
                    _write_payload(tmp_path, "quick", _OK_PAYLOAD),
                    ok_contract,
                    **image_kw,
                ),
            }
        )
        runner = _runner(registry)

        single_registry = StageRegistry(
            {
                "single": _stage_def(
                    _write_payload(tmp_path, "single", _WS_WRITE_PAYLOAD),
                    ok_contract,
                    **self._image_kw(tmp_path, template),
                )
            }
        )
        single_runner = _runner(single_registry)
        single_spec = WorkflowSpec.parse(
            {"nodes": [{"id": "single", "tool": "single", "args": {}}]}
        )

        spec = WorkflowSpec.parse(
            {
                "nodes": [
                    {
                        "id": "hold",
                        "tool": "hold",
                        "args": {
                            "ready_path": "/sync/hold.ready",
                            "stop_path": "/sync/go",
                            "hold_sec": 60.0,
                        },
                    },
                    {"id": "quick", "tool": "quick", "args": {}},
                ],
            }
        )

        finish_times: dict[str, float] = {}
        errors: list[BaseException] = []

        def run_group() -> None:
            try:
                runner.run(spec, JournalStand.context())
                finish_times["group"] = time.monotonic()
            except BaseException as exc:
                errors.append(exc)

        def run_single() -> None:
            try:
                single_runner.run(single_spec, JournalStand.context())
                finish_times["single"] = time.monotonic()
            except BaseException as exc:
                errors.append(exc)

        group_thread = threading.Thread(target=run_group)
        group_thread.start()

        single_thread = threading.Thread(target=run_single)
        try:
            _wait_for_file(sync / "hold.ready", timeout_sec=30.0)

            single_thread.start()
            time.sleep(1.0)

            assert single_thread.is_alive(), "single call must wait on the image flock"
        finally:
            (sync / "go").write_text("go", encoding="utf-8")
            group_thread.join(timeout=60)
            single_thread.join(timeout=60)

        assert not group_thread.is_alive()
        assert not single_thread.is_alive()
        assert not errors, f"runs failed: {errors}"

        assert finish_times["single"] >= finish_times["group"]


@needs_fuse
@needs_userns
@needs_delegation
class TestMountGroupCgroup:
    """Per-stage cgroup внутри группы: OOM ловит виновная стадия, не соседи."""

    @staticmethod
    def _require_migration(base: str) -> None:
        """Миграция в делегированный leaf возможна; иначе тест не о чем."""
        manager = CgroupManager(base)

        try:
            leaf = manager.acquire(GroupLimits(cpu_weight=100))
        except CgroupError as exc:
            pytest.skip(f"cgroup delegation unavailable: {exc}")

        procs_path = os.path.join(leaf, "cgroup.procs")

        def enter() -> None:
            fd = os.open(procs_path, os.O_WRONLY)
            os.write(fd, b"0")
            os.close(fd)

        try:
            probe = subprocess.Popen(
                [sys.executable, "-c", "pass"],
                preexec_fn=enter,  # noqa: PLW1509
            )
        except (OSError, subprocess.SubprocessError):
            manager.release(leaf)
            pytest.skip("pytest is outside the delegated scope, migration is forbidden")

        probe.wait(timeout=10)
        manager.release(leaf)

    def test_oom_kills_stage_not_neighbors(
        self, tmp_path: Path, template: Path
    ) -> None:
        base = os.path.join(_DELEGATED_PARENT, f"boba-wf-{os.getpid()}")
        self._require_migration(base)

        sync = tmp_path / "sync"
        sync.mkdir(exist_ok=True)

        contract = StageContract(out=None, result=OkTrailer)
        image_kw: dict[str, Any] = {
            "rw_images": (f"{tmp_path}/ws/shared.ext4:/workspace",),
            "image_template": str(template),
            "max_memory_bytes": 1024 * 1024 * 1024,
        }
        registry = StageRegistry(
            {
                "hog": _stage_def(
                    _write_payload(tmp_path, "hog", _HOG_PAYLOAD),
                    contract,
                    cgroup_base=base,
                    cgroup_memory_bytes=64 * 1024 * 1024,
                    cgroup_swap_max_bytes=0,
                    cgroup_oom_kill_all=True,
                    **image_kw,
                ),
                "calm": _stage_def(
                    _write_payload(tmp_path, "calm", _NAP_PAYLOAD),
                    contract,
                    rw_binds=(f"{sync}:/sync",),
                    **image_kw,
                ),
            }
        )
        runner = _runner(registry)

        baseline = _open_fd_count()

        spec = WorkflowSpec.parse(
            {
                "nodes": [
                    {
                        "id": "calm",
                        "tool": "calm",
                        "args": {
                            "ready_path": "/sync/calm.ready",
                            "stop_path": "/sync/never",
                            "hold_sec": 60.0,
                        },
                    },
                    {"id": "oom", "tool": "hog", "args": {}},
                ],
            }
        )

        with pytest.raises(WorkflowError) as failure:
            runner.run(spec, JournalStand.context())

        message = str(failure.value)
        assert "workflow stage oom exited with code 137" in message
        blamed_neighbor = "workflow stage calm" in message
        assert not blamed_neighbor, "cascaded neighbor must not be blamed"

        _assert_stages_gone()
        _assert_fds_restored(baseline)

        # cgroup-leaf'ы освобождены: в базе не осталось подкаталогов-групп
        leftovers: list[str] = []
        if os.path.isdir(base):
            for entry in os.listdir(base):
                if os.path.isdir(os.path.join(base, entry)):
                    leftovers.append(entry)
        assert leftovers == []
