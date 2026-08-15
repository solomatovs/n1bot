"""Канальный запуск инструмента в настоящем bwrap: конверт, каналы, секреты.

Покрыты оба пути: прямой bwrap и цепочка «bwrap -> лаунчер -> bwrap» с
монтированием ext4-образа — дескрипторы каналов проезжают её насквозь.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
from fake_channel_tool import ChannelConfig, fx_echo
from pydantic import SecretStr

from boba.sandbox import SandboxCaller, SandboxProfile
from boba.sandbox.caller import SandboxPayloadError
from boba.toolkit.channels import ToolChannel
from boba.toolkit.entry import (
    ReplyError,
    ReplyOk,
    ToolAddress,
    ToolArgv,
    ToolCommand,
    ToolMain,
)
from boba.toolkit.stream import StreamSink, ToolChannelsTap

REPO = Path(__file__).resolve().parents[5]
SANDBOX = REPO / "build" / "src" / "sandbox"
ROOTFS = SANDBOX / "rootfs"

SRC_PACKAGES = (
    "core/boba-cancellation",
    "core/boba-toolkit",
)

needs_sandbox = pytest.mark.skipif(
    shutil.which("bwrap") is None or not (ROOTFS / "bin" / "sh").exists(),
    reason="нет bwrap или артефактов песочницы (собрать: make deps)",
)
needs_userns = pytest.mark.skipif(
    os.geteuid() == 0, reason="под root user namespace ведёт себя иначе"
)
needs_mkfs = pytest.mark.skipif(
    shutil.which("mkfs.ext4") is None or shutil.which("fuse2fs") is None,
    reason="нет mkfs.ext4/fuse2fs для образа workspace",
)

CFG = ChannelConfig(token=SecretStr("fx-s3cret"))

FX = ToolMain.toolset(fx_echo)[0]


def _bin_dirs() -> list[str]:
    dirs: list[str] = []
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        if entry.startswith("/"):
            dirs.append(entry)
    return dirs


def _python_path() -> str:
    # tests лежат внутри packages, смонтированного в /usr/src, — модуль
    # fake_channel_tool доступен без отдельного бинда
    parts = ["/usr/src/infra/sandbox/boba-sandbox/tests"]
    for name in SRC_PACKAGES:
        parts.append(f"/usr/src/{name}/src")
    return ":".join(parts)


def _profile(**overrides: Any) -> SandboxProfile:
    site_packages = "/usr/local/lib/python3.11/site-packages"
    raw: dict[str, Any] = {
        "rootfs": str(ROOTFS),
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


@pytest.fixture(autouse=True)
def chainlit_context() -> None:
    pass


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
        self.sinks: dict[ToolChannel, ChannelRecorder] = {}

    def sink_of(self, channel: ToolChannel) -> StreamSink:
        return self.sinks.setdefault(channel, ChannelRecorder())

    def text_of(self, channel: ToolChannel) -> str:
        recorder = self.sinks.get(channel)
        if recorder is None:
            return ""
        return recorder.text()


def _command(text: str) -> ToolCommand:
    return ToolArgv.render(
        ToolAddress.of(FX),
        ToolArgv.schema_of(FX),
        {"text": text, "cfg": CFG},
    )


@needs_sandbox
class TestRunToolInSandbox:
    def test_envelope_and_channels_are_separated(self) -> None:
        caller = SandboxCaller("fx", _profile(), dict)

        sinks = RecordingSinks()
        ToolChannelsTap.set(sinks)
        try:
            outcome = caller.run_tool(_command("hello"))
        finally:
            ToolChannelsTap.set(None)

        reply = outcome.reply
        assert isinstance(reply, ReplyOk)
        assert reply.content == "hello|fx-s3cret"
        assert reply.artifact.kind == "text"

        # болтовня тела ушла своими каналами, конверт остался чистым
        assert "noise on stdout" in sinks.text_of(ToolChannel.STDOUT)
        assert "noise on stderr" in sinks.text_of(ToolChannel.STDERR)
        assert "noise" not in reply.content

    def test_expected_error_arrives_as_error_reply(self) -> None:
        caller = SandboxCaller("fx", _profile(), dict)

        outcome = caller.run_tool(_command("boom"))

        reply = outcome.reply
        assert isinstance(reply, ReplyError)
        assert reply.kind == "fx_down"
        assert "fx backend is down" in reply.message

    def test_secret_is_absent_from_argv(self) -> None:
        command = _command("hello")

        assert "fx-s3cret" not in " ".join(command.argv)
        assert b"fx-s3cret" in command.stdin

    def test_broken_command_reports_missing_envelope(self) -> None:
        caller = SandboxCaller("fx", _profile(), dict)

        command = ToolCommand(argv=("python3", "-c", "raise SystemExit(3)"), stdin=b"")

        with pytest.raises(SandboxPayloadError) as caught:
            caller.run_tool(command)

        assert "no envelope" in str(caught.value)


@pytest.fixture(scope="module")
def ext4_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Шаблон образа на 64 МиБ: разрежённый, поэтому создаётся мгновенно."""
    path = tmp_path_factory.mktemp("chain") / "template.ext4"
    with path.open("wb") as f:
        f.truncate(64 * 1024 * 1024)

    mkfs = shutil.which("mkfs.ext4")
    assert mkfs is not None
    subprocess.run(  # noqa: S603
        [mkfs, "-q", "-F", str(path)],
        check=True,
        capture_output=True,
    )
    return path


@needs_sandbox
@needs_userns
@needs_mkfs
class TestRunToolThroughChain:
    """Цепочка «bwrap -> лаунчер -> bwrap»: каналы проезжают насквозь."""

    def _chain_caller(
        self, tmp_path: Path, ext4_template: Path
    ) -> SandboxCaller:
        image = tmp_path / "ws" / "chain-user.ext4"
        profile = _profile(
            rw_images=(f"{image}:/workspace",),
            image_template=str(ext4_template),
            cwd="/workspace",
        )
        return SandboxCaller("fx-chain", profile, dict)

    def test_envelope_survives_the_chain(
        self, tmp_path: Path, ext4_template: Path
    ) -> None:
        caller = self._chain_caller(tmp_path, ext4_template)

        sinks = RecordingSinks()
        ToolChannelsTap.set(sinks)
        try:
            outcome = caller.run_tool(_command("hello"))
        finally:
            ToolChannelsTap.set(None)

        reply = outcome.reply
        assert isinstance(reply, ReplyOk)
        assert reply.content == "hello|fx-s3cret"

        # болтовня тела разъехалась по каналам и через две ступени bwrap
        assert "noise on stdout" in sinks.text_of(ToolChannel.STDOUT)
        assert "noise on stderr" in sinks.text_of(ToolChannel.STDERR)
        assert "noise" not in reply.content

    def test_workspace_image_is_mounted_for_the_tool(
        self, tmp_path: Path, ext4_template: Path
    ) -> None:
        caller = self._chain_caller(tmp_path, ext4_template)

        outcome = caller.run_tool(_command("workspace"))

        reply = outcome.reply
        assert isinstance(reply, ReplyOk)
        assert reply.content.startswith("workspace:")
        assert "fx-probe.txt" in reply.content

    def test_expected_error_survives_the_chain(
        self, tmp_path: Path, ext4_template: Path
    ) -> None:
        caller = self._chain_caller(tmp_path, ext4_template)

        outcome = caller.run_tool(_command("boom"))

        reply = outcome.reply
        assert isinstance(reply, ReplyError)
        assert reply.kind == "fx_down"
