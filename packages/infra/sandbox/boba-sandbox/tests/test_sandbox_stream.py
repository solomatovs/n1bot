"""Живой журнал стадии: исходящие каналы пишутся в файлы по ходу её работы."""

from __future__ import annotations

import os
import shutil
import threading
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any, ClassVar

import pydantic
import pytest
from journal_stand import JournalStand
from pydantic import BaseModel, JsonValue

from boba.sandbox import SandboxCaller, SandboxProfile
from boba.sandbox.journal import (
    CallJournal,
    JournalNote,
    JournalWriter,
    StreamJournal,
)
from boba.sandbox.runner import ToolCallContext
from boba.sandbox.workflow import StageDef, StageRegistry
from boba.toolkit.channels import Channel, StreamFormat, StreamKey
from boba.toolkit.launcher import TextCollector
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


_GATE_PAYLOAD = """
import os
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

stream = channels.payload()
stream.write("chunk: start\\n".encode("utf-8"))
stream.flush()

deadline = time.monotonic() + 20.0
while not os.path.exists("/opt/gate/go"):
    if time.monotonic() > deadline:
        raise SystemExit(3)

    time.sleep(0.02)

stream.write("chunk: finish\\n".encode("utf-8"))
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


class SizeListener:
    """Подписчик реестра: длина файла канала на каждую порцию."""

    def __init__(self, journal: StreamJournal, channel: Channel) -> None:
        self._journal = journal
        self._channel = channel
        self.sizes: list[int] = []
        self.wake = threading.Event()

    def on_open(self, call: CallJournal) -> None:
        """Открытие вызова наблюдателю не нужно."""

    def on_data(self, key: StreamKey) -> None:
        if key.channel is not self._channel:
            return

        window = self._journal.slice_at(key, -1)
        if window is None:
            return

        self.sizes.append(window.size)
        self.wake.set()

    def on_close(self, call: CallJournal) -> None:
        """Закрытие вызова наблюдателю не нужно."""


class GateOpener(threading.Thread):
    """Открывает ворота стадии, как только её порция появилась в файле журнала.

    Стадия стоит на воротах и падает по своему таймауту, если файл журнала не
    наполнился по ходу её работы: тест краснеет от записи, отложенной на конец.
    """

    WAIT_SEC: ClassVar[float] = 15.0
    POLL_SEC: ClassVar[float] = 0.02

    def __init__(self, journal: StreamJournal, key: StreamKey, gate: Path) -> None:
        super().__init__(name="gate-opener", daemon=True)
        self._journal = journal
        self._key = key
        self._gate = gate
        self.seen = ""

    def run(self) -> None:
        deadline = time.monotonic() + self.WAIT_SEC

        while time.monotonic() < deadline:
            window = self._journal.slice_at(self._key, 0)

            if window is None:
                time.sleep(self.POLL_SEC)
                continue

            if not window.text:
                time.sleep(self.POLL_SEC)
                continue

            self.seen = window.text
            (self._gate / "go").write_bytes(b"go")

            return


@needs_sandbox
@needs_userns
class TestLiveJournal:
    """Журнал стадии наполняется по ходу работы и несёт ровно свой канал."""

    @staticmethod
    def _gate(tmp_path: Path) -> Path:
        """Каталог ворот: host создаёт в нём файл, стадия его ждёт."""
        gate = tmp_path / "gate"
        gate.mkdir(parents=True, exist_ok=True)

        return gate

    @classmethod
    def _caller(
        cls, tmp_path: Path, script: str, journal: StreamJournal
    ) -> SandboxCaller:
        payload_dir = tmp_path / "payload"
        payload_dir.mkdir(parents=True, exist_ok=True)
        (payload_dir / "main.py").write_text(script, encoding="utf-8")

        fields = dict(_PROFILE_BASE)
        binds = list(HOST_RO_BINDS)
        binds.append(f"{TOOLKIT_SRC}:/opt/src")
        binds.append(f"{SITE_PACKAGES}:/opt/site")
        binds.append(f"{payload_dir}:/opt/payload")
        binds.append(f"{cls._gate(tmp_path)}:/opt/gate")
        fields["ro_binds"] = tuple(binds)

        definition = StageDef(
            contract=StageContract(out=StreamFormat.TEXT, result=Trailer),
            profile=SandboxProfile.model_validate(fields),
            entry=PAYLOAD_ENTRY,
            request=ProbeRequest,
            enrich=_identity_args,
        )

        return SandboxCaller(
            StageRegistry({"probe": definition}), _allow_all, dict, journal
        )

    def test_every_outgoing_channel_lands_in_its_own_file(
        self, tmp_path: Path
    ) -> None:
        journal = JournalStand.journal()
        collector = TextCollector(
            max_chars=1_000_000, limit_rows=None, header_lines=0
        )

        outcome = self._caller(tmp_path, _STREAM_PAYLOAD, journal).call(
            _spec(), sinks={"probe": collector}
        )
        collector.close()

        stdout_text = JournalStand.text_of(outcome, "probe", Channel.TOOL_STDOUT)
        payload_text = JournalStand.text_of(outcome, "probe", Channel.TOOL_PAYLOAD)
        result_text = JournalStand.text_of(outcome, "probe", Channel.TOOL_RESULT)

        assert "человеческая строка" in stdout_text
        assert "кусок данных: старт" in payload_text
        assert "кусок данных: финиш" in payload_text
        assert "кусок данных" not in stdout_text
        assert "bytes_out" not in payload_text
        assert "bytes_out" in result_text

        channels = set()
        for key in outcome.journals:
            channels.add(key.channel)

        assert Channel.TOOL_STDERR in channels
        assert Channel.WRAP_STDOUT in channels
        assert Channel.WRAP_STDERR in channels

        trailer = outcome.trailer("probe", Trailer)
        assert trailer.pages == 1
        assert "кусок данных: старт" in collector.text()

    def test_a_finished_call_leaves_no_channel_hanging(self, tmp_path: Path) -> None:
        """Каждый канал закрыт своим EOF: пометки о срыве в панели быть не должно."""
        journal = JournalStand.journal()

        outcome = self._caller(tmp_path, _STREAM_PAYLOAD, journal).call(_spec())

        assert outcome.journals

        for key in outcome.journals:
            window = journal.slice_at(key, 0)
            assert window is not None, key.rel_log()
            assert window.closed is True, key.rel_log()
            assert window.note == JournalNote.DONE.value, key.rel_log()

    def test_every_chunk_wakes_the_subscriber(self, tmp_path: Path) -> None:
        """Подписчик реестра видит рост файла порция за порцией, а не один итог.

        Что порции доезжают до файла именно во время работы стадии, доказывает
        `test_the_stage_waits_on_its_own_journal_file`: здесь проверяется
        только дробность событий.
        """
        journal = JournalStand.journal()
        listener = SizeListener(journal, Channel.TOOL_PAYLOAD)
        journal.registry.subscribe(listener)

        try:
            self._caller(tmp_path, _STREAM_PAYLOAD, journal).call(_spec())
        finally:
            journal.registry.unsubscribe(listener)

        assert len(listener.sizes) >= 2
        assert listener.sizes == sorted(listener.sizes)
        assert listener.sizes[0] < listener.sizes[-1]

    def test_journal_survives_a_call_without_collectors(
        self, tmp_path: Path
    ) -> None:
        """Продукт никто не собирает: файл журнала всё равно полон."""
        journal = JournalStand.journal()

        outcome = self._caller(tmp_path, _STREAM_PAYLOAD, journal).call(_spec())

        payload_text = JournalStand.text_of(outcome, "probe", Channel.TOOL_PAYLOAD)

        assert "кусок данных: финиш" in payload_text

    def test_the_stage_waits_on_its_own_journal_file(self, tmp_path: Path) -> None:
        """Ворота открывает прочитанный файл: стадия ещё в середине работы."""
        journal = JournalStand.journal()
        key = ToolCallContext.current().key("probe", Channel.TOOL_PAYLOAD)

        opener = GateOpener(JournalStand.journal(), key, self._gate(tmp_path))
        opener.start()

        outcome = self._caller(tmp_path, _GATE_PAYLOAD, journal).call(_spec())
        opener.join(GateOpener.WAIT_SEC)

        assert opener.seen == "chunk: start\n"

        payload_text = JournalStand.text_of(outcome, "probe", Channel.TOOL_PAYLOAD)

        assert payload_text == "chunk: start\nchunk: finish\n"
        assert outcome.trailer("probe", Trailer).pages == 1


@needs_sandbox
@needs_userns
class TestJournalFailureOnALiveStage:
    """Сбой журнала не трогает стадию: продукт доезжает целиком, rc нулевой."""

    def test_an_overflowing_buffer_stops_the_journal_alone(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Буфер writer-потока меньше первой порции: журнал сдаётся пометкой."""
        monkeypatch.setattr(JournalWriter, "BUFFER_BYTES", 4)

        journal = JournalStand.journal()
        collector = TextCollector(
            max_chars=1_000_000, limit_rows=None, header_lines=0
        )

        outcome = TestLiveJournal._caller(tmp_path, _STREAM_PAYLOAD, journal).call(
            _spec(), sinks={"probe": collector}
        )
        collector.close()

        assert "кусок данных: старт" in collector.text()
        assert "кусок данных: финиш" in collector.text()
        assert outcome.trailer("probe", Trailer).pages == 1

        key = outcome.journal_of("probe", Channel.TOOL_PAYLOAD)
        assert key is not None

        window = journal.slice_at(key, 0)
        assert window is not None
        assert window.closed is True
        assert window.note == JournalNote.OVERFLOW.value
        assert window.size == 0

    def test_a_read_only_vault_leaves_the_stage_alone(self, tmp_path: Path) -> None:
        """Файл не открылся: журнала у вызова нет, стадия отработала штатно."""
        thread_dir = JournalStand.root() / JournalStand.USER / JournalStand.THREAD
        thread_dir.mkdir(parents=True)
        os.chmod(thread_dir, 0o500)

        journal = JournalStand.journal()
        collector = TextCollector(
            max_chars=1_000_000, limit_rows=None, header_lines=0
        )

        try:
            outcome = TestLiveJournal._caller(
                tmp_path, _STREAM_PAYLOAD, journal
            ).call(_spec(), sinks={"probe": collector})
        finally:
            os.chmod(thread_dir, 0o700)

        collector.close()

        assert outcome.journals == ()
        assert "кусок данных: финиш" in collector.text()
        assert outcome.trailer("probe", Trailer).pages == 1
