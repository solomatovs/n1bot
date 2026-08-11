"""Временный тап живого вывода (этап 3): канал кормит окно по ходу работы стадии."""

from __future__ import annotations

import os
import shutil
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pydantic
import pytest
from pydantic import BaseModel, JsonValue

from boba.sandbox import SandboxCaller, SandboxProfile
from boba.sandbox.caller import TapChannelSink
from boba.sandbox.workflow import StageDef, StageRegistry
from boba.toolkit.channels import StreamFormat
from boba.toolkit.launcher import TextCollector
from boba.toolkit.stream import ToolStreamBuffer, ToolStreamTap
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


class Trailer(BaseModel):
    """Квитанция тестового узла."""

    pages: int


class ProbeRequest(BaseModel):
    """Запрос тестового узла: полей нет."""


_STREAM_PAYLOAD = """
import time

from pydantic import BaseModel

from boba.toolkit.payload import PayloadChannels, PayloadLogging


class Request(BaseModel):
    pass


class Trailer(BaseModel):
    pages: int


PayloadLogging.setup()
channels = PayloadChannels.open()
channels.args(Request)

print("человеческая строка", flush=True)

stream = channels.payload()
stream.write("кусок данных: старт\\n".encode("utf-8"))
stream.flush()

time.sleep(0.3)

stream.write("кусок данных: финиш\\n".encode("utf-8"))
stream.flush()

channels.write_result(Trailer(pages=1))
raise SystemExit(int(channels.exit_code()))
"""


def _identity_args(args: Mapping[str, JsonValue], /) -> Mapping[str, JsonValue]:
    return dict(args)


def _allow_all(tool: str, /) -> bool:
    return True


def _spec() -> WorkflowSpec:
    return WorkflowSpec.model_validate(
        {"nodes": [{"id": "probe", "tool": "probe", "args": {}}]}
    )


def _window(window_bytes: int = 64 * 1024) -> ToolStreamBuffer:
    def wake() -> None:
        pass

    return ToolStreamBuffer(window_bytes, wake)


@pytest.fixture(autouse=True)
def clean_tap() -> Any:
    yield
    ToolStreamTap.set(None)


class TestTapAdapter:
    """TapChannelSink: байты канала уезжают в StreamSink, закрытие — не его дело."""

    def test_bytes_reach_the_sink(self) -> None:
        buffer = _window()
        adapter = TapChannelSink(buffer)

        adapter.feed("привет".encode("utf-8"))
        adapter.close()

        window = buffer.snapshot()
        assert "привет" in window.text
        assert window.closed is False


@needs_sandbox
@needs_userns
class TestLiveStreamTap:
    """Окно видит tool_stdout и tool_payload стадии, протокола в нём нет."""

    @staticmethod
    def _caller(tmp_path: Path, script: str) -> SandboxCaller:
        payload_dir = tmp_path / "payload"
        payload_dir.mkdir(parents=True, exist_ok=True)
        (payload_dir / "main.py").write_text(script, encoding="utf-8")

        fields = dict(_PROFILE_BASE)
        binds = list(HOST_RO_BINDS)
        binds.append(f"{TOOLKIT_SRC}:/opt/src")
        binds.append(f"{SITE_PACKAGES}:/opt/site")
        binds.append(f"{payload_dir}:/opt/payload")
        fields["ro_binds"] = tuple(binds)

        definition = StageDef(
            contract=StageContract(
                accepts=frozenset(), out=StreamFormat.TEXT, result=Trailer
            ),
            profile=SandboxProfile.model_validate(fields),
            entry=PAYLOAD_ENTRY,
            request=ProbeRequest,
            enrich=_identity_args,
        )

        return SandboxCaller(
            StageRegistry({"probe": definition}), _allow_all, dict
        )

    def test_window_gets_stdout_and_payload_and_result_is_kept(
        self, tmp_path: Path
    ) -> None:
        buffer = _window()
        ToolStreamTap.set(buffer)
        collector = TextCollector(
            max_chars=1_000_000, limit_rows=None, header_lines=0
        )

        outcome = self._caller(tmp_path, _STREAM_PAYLOAD).call(
            _spec(), sinks={"probe": collector}
        )
        collector.close()

        window = buffer.snapshot()
        assert "человеческая строка" in window.text
        assert "кусок данных: старт" in window.text
        assert "кусок данных: финиш" in window.text
        assert "bytes_out" not in window.text

        trailer = outcome.trailer("probe", Trailer)
        assert trailer.pages == 1
        assert "кусок данных: старт" in collector.text()

    def test_without_tap_nothing_changes(self, tmp_path: Path) -> None:
        ToolStreamTap.set(None)
        collector = TextCollector(
            max_chars=1_000_000, limit_rows=None, header_lines=0
        )

        outcome = self._caller(tmp_path, _STREAM_PAYLOAD).call(
            _spec(), sinks={"probe": collector}
        )
        collector.close()

        assert outcome.trailer("probe", Trailer).pages == 1
        assert "кусок данных: финиш" in collector.text()

    def test_window_fills_while_the_stage_runs(self, tmp_path: Path) -> None:
        """Пробуждения приходят по ходу стадии, а не одним махом в конце."""
        sizes: list[int] = []
        holder: list[ToolStreamBuffer] = []

        def wake() -> None:
            sizes.append(len(holder[0].snapshot().text))

        buffer = ToolStreamBuffer(64 * 1024, wake)
        holder.append(buffer)
        ToolStreamTap.set(buffer)

        self._caller(tmp_path, _STREAM_PAYLOAD).call(_spec())

        assert len(sizes) >= 2
        assert sizes == sorted(sizes)
        assert sizes[0] < sizes[-1]
