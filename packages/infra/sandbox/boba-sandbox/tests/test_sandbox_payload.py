"""Канальный контракт payload'а: конверт tool_result и вырожденный граф каллера."""

from __future__ import annotations

import os
import shutil
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pydantic
import pytest
from journal_stand import JournalStand
from pydantic import BaseModel, JsonValue

from boba.sandbox import SandboxCaller, SandboxPayloadError, SandboxProfile
from boba.sandbox.runner import ResultSink
from boba.sandbox.workflow import StageDef, StageRegistry
from boba.toolkit.channels import StreamFormat
from boba.toolkit.launcher import (
    LauncherError,
    PayloadFailureError,
    TextCollector,
)
from boba.toolkit.workflow import StageContract, WorkflowSpec

REPO = Path(__file__).resolve().parents[5]
TOOLKIT_SRC = REPO / "packages" / "core" / "boba-toolkit" / "src"
SITE_PACKAGES = Path(pydantic.__file__).resolve().parents[1]

HOST_RO_BINDS: tuple[str, ...] = ("/usr", "/bin", "/sbin", "/lib", "/lib64")

PAYLOAD_ENTRY: tuple[str, ...] = ("python3.11", "/opt/payload/main.py")

needs_sandbox = pytest.mark.skipif(
    shutil.which("bwrap") is None, reason="bwrap не установлен"
)
needs_userns = pytest.mark.skipif(
    os.geteuid() == 0, reason="под root userns ведёт себя иначе"
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
        "lock_wait_sec": 10.0,
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


class Answer(BaseModel):
    """Схема data конверта в тестах."""

    text: str
    pages: int


class Trailer(BaseModel):
    """Квитанция потокового payload'а: данные уехали каналом."""

    pages: int


class TestResultSink:
    """Всё, что отклоняется от контракта конверта, — ошибка, а не частичный итог."""

    @staticmethod
    def _sink(schema: type[Answer] = Answer) -> ResultSink[Answer]:
        return ResultSink("doc", schema)

    def test_success_envelope_is_parsed(self) -> None:
        sink = self._sink()
        envelope = '{"bytes_out": 0, "data": {"text": "привет", "pages": 2}}\n'
        sink.feed(envelope.encode("utf-8"))

        answer = sink.data()
        assert (answer.text, answer.pages) == ("привет", 2)

    def test_envelope_split_across_reads(self) -> None:
        """Границы чтения из пайпа не совпадают с границами конверта."""
        sink = self._sink()
        raw = b'{"bytes_out": 0, "data": {"text": "ok", "pages": 1}}'
        for start in range(0, len(raw), 7):
            sink.feed(raw[start : start + 7])

        assert sink.data().pages == 1

    def test_failure_envelope_names_kind_and_message(self) -> None:
        sink = self._sink()
        sink.feed(
            b'{"error": {"kind": "bad_format", "message": "not readable"}}\n'
        )

        failure = sink.failure()
        assert failure is not None
        assert (failure.kind, failure.message) == ("bad_format", "not readable")

    def test_success_over_failure_envelope_is_error(self) -> None:
        sink = self._sink()
        sink.feed(b'{"error": {"kind": "k", "message": "m"}}\n')

        with pytest.raises(LauncherError, match="error envelope"):
            sink.success()

    def test_empty_result_is_error(self) -> None:
        sink = self._sink()

        assert sink.received is False
        with pytest.raises(LauncherError, match="empty"):
            sink.success()

    def test_broken_json_is_error(self) -> None:
        sink = self._sink()
        sink.feed(b"{broken\n")

        with pytest.raises(LauncherError, match="not valid JSON"):
            sink.success()

    def test_two_envelopes_are_error(self) -> None:
        sink = self._sink()
        sink.feed(b'{"bytes_out": 0, "data": {}}\n{"bytes_out": 0, "data": {}}\n')

        with pytest.raises(LauncherError, match="not valid JSON"):
            sink.success()

    def test_non_object_envelope_is_error(self) -> None:
        sink = self._sink()
        sink.feed(b'["list"]\n')

        with pytest.raises(LauncherError, match="JSON object"):
            sink.success()

    def test_envelope_without_contract_fields_is_error(self) -> None:
        """Голый JSON без bytes_out/error — не конверт."""
        sink = self._sink()
        sink.feed(b'{"text": "ok", "pages": 1}\n')

        with pytest.raises(LauncherError, match="does not match contract"):
            sink.success()

    def test_data_schema_mismatch_is_error(self) -> None:
        sink = self._sink()
        sink.feed(b'{"bytes_out": 0, "data": {"text": "ok"}}\n')

        with pytest.raises(LauncherError, match="does not match Answer"):
            sink.data()

    def test_oversized_result_is_error(self) -> None:
        sink = self._sink()

        with pytest.raises(LauncherError, match="exceeds"):
            sink.feed(b"x" * (ResultSink.MAX_BYTES + 1))


_STREAM_PAYLOAD = """
from pathlib import Path

from pydantic import BaseModel

from boba.toolkit.payload import PayloadChannels, PayloadLogging


class Request(BaseModel):
    path: str


class Trailer(BaseModel):
    pages: int


PayloadLogging.setup()
channels = PayloadChannels.open()
request = channels.args(Request)

text = Path(request.path).read_text(encoding="utf-8")

stream = channels.payload()
stream.write(text.encode("utf-8"))
stream.flush()

channels.write_result(Trailer(pages=1))
raise SystemExit(int(channels.exit_code()))
"""


_READONLY_PAYLOAD = """
from pathlib import Path

from pydantic import BaseModel

from boba.toolkit.payload import PayloadChannels, PayloadLogging


class Request(BaseModel):
    path: str


class Trailer(BaseModel):
    pages: int


PayloadLogging.setup()
channels = PayloadChannels.open()
channels.args(Request)

stream = channels.payload()
try:
    Path("/opt/payload/main.py").write_text("взлом")
    stream.write(b"rewritten")
except OSError as e:
    stream.write(type(e).__name__.encode("utf-8"))
stream.flush()

channels.write_result(Trailer(pages=0))
raise SystemExit(int(channels.exit_code()))
"""


_FAILURE_PAYLOAD = """
from pydantic import BaseModel

from boba.toolkit.payload import PayloadChannels, PayloadLogging


class Request(BaseModel):
    path: str


PayloadLogging.setup()
channels = PayloadChannels.open()
channels.args(Request)
channels.write_error("bad_format", "формат .md не читается")
raise SystemExit(1)
"""


_CRASH_PAYLOAD = """
raise SystemExit('нет такого файла')
"""


_SLEEP_PAYLOAD = """
import time

time.sleep(30)
"""


class ProbeRequest(BaseModel):
    """Запрос тестового узла."""

    path: str = ""


def _identity_args(args: Mapping[str, JsonValue], /) -> Mapping[str, JsonValue]:
    return dict(args)


def _allow_all(tool: str, /) -> bool:
    return True


def _spec(args: Mapping[str, JsonValue] | None = None) -> WorkflowSpec:
    if args is None:
        args = {}

    return WorkflowSpec.model_validate(
        {"nodes": [{"id": "probe", "tool": "probe", "args": dict(args)}]}
    )


@needs_sandbox
@needs_userns
class TestDegenerateGraphCall:
    """Payload реально исполняется в песочнице; итог собирается из каналов."""

    @staticmethod
    def _caller(
        tmp_path: Path, script: str, **profile_kw: Any
    ) -> SandboxCaller:
        payload_dir = tmp_path / "payload"
        payload_dir.mkdir(parents=True, exist_ok=True)
        (payload_dir / "main.py").write_text(script, encoding="utf-8")

        fields = dict(_PROFILE_BASE)
        binds = list(HOST_RO_BINDS)
        binds.append(f"{TOOLKIT_SRC}:/opt/src")
        binds.append(f"{SITE_PACKAGES}:/opt/site")
        binds.append(f"{payload_dir}:/opt/payload")
        fields["ro_binds"] = tuple(binds)
        fields.update(profile_kw)

        definition = StageDef(
            contract=StageContract(out=StreamFormat.TEXT, result=Trailer),
            profile=SandboxProfile.model_validate(fields),
            entry=PAYLOAD_ENTRY,
            request=ProbeRequest,
            enrich=_identity_args,
        )

        return SandboxCaller(
            StageRegistry({"probe": definition}),
            _allow_all,
            dict,
            JournalStand.journal(),
        )

    def test_request_and_stream_travel_through_channels(
        self, tmp_path: Path
    ) -> None:
        doc = tmp_path / "payload" / "doc.txt"
        doc.parent.mkdir(parents=True, exist_ok=True)
        doc.write_text("содержимое", encoding="utf-8")

        caller = self._caller(tmp_path, _STREAM_PAYLOAD)
        collector = TextCollector(
            max_chars=1_000_000, limit_rows=None, header_lines=0
        )

        outcome = caller.call(
            _spec({"path": "/opt/payload/doc.txt"}), sinks={"probe": collector}
        )
        collector.close()

        trailer = outcome.trailer("probe", Trailer)
        assert (collector.text(), trailer.pages) == ("содержимое", 1)
        assert outcome.outcome_of("probe").exit_code == 0

    def test_payload_is_read_only_inside(self, tmp_path: Path) -> None:
        caller = self._caller(tmp_path, _READONLY_PAYLOAD)
        collector = TextCollector(
            max_chars=1_000_000, limit_rows=None, header_lines=0
        )

        caller.call(_spec(), sinks={"probe": collector})
        collector.close()

        assert collector.text() == "OSError"

    def test_declared_failure_travels_as_error_envelope(
        self, tmp_path: Path
    ) -> None:
        caller = self._caller(tmp_path, _FAILURE_PAYLOAD)

        with pytest.raises(PayloadFailureError) as failure:
            caller.call(_spec())

        assert failure.value.kind == "bad_format"
        assert str(failure.value) == "формат .md не читается"

    def test_payload_crash_is_reported(self, tmp_path: Path) -> None:
        caller = self._caller(tmp_path, _CRASH_PAYLOAD)

        with pytest.raises(SandboxPayloadError, match="нет такого файла"):
            caller.call(_spec())

    def test_payload_timeout_is_reported(self, tmp_path: Path) -> None:
        caller = self._caller(tmp_path, _SLEEP_PAYLOAD, timeout_sec=1)

        with pytest.raises(SandboxPayloadError, match="timed out"):
            caller.call(_spec())

    def test_unknown_tool_is_reported(self, tmp_path: Path) -> None:
        caller = self._caller(tmp_path, _STREAM_PAYLOAD)
        spec = WorkflowSpec.model_validate(
            {"nodes": [{"id": "x", "tool": "ghost", "args": {}}]}
        )

        with pytest.raises(SandboxPayloadError, match="unknown workflow tool"):
            caller.call(spec)
