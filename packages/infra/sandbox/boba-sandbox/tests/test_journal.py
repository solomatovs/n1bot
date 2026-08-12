"""Журнал вывода: имена файлов, запись writer-потоком, окна, учёт и вытеснение."""

from __future__ import annotations

import errno
import os
import stat
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import ClassVar

import pytest
from pydantic import ValidationError

from boba.sandbox import journal as journal_module
from boba.sandbox.journal import (
    CallJournal,
    DirVault,
    JournalError,
    JournalNote,
    JournalRecord,
    JournalWriter,
    StageJournalSinks,
    StreamJournal,
    VaultMode,
)
from boba.sandbox.runner import ToolCallContext
from boba.toolkit.channels import (
    Channel,
    ChannelError,
    JournalFile,
    StreamFormat,
    StreamKey,
)

CONTEXT = ToolCallContext(
    user_id="7", thread_id="t-1", call_id="call-1", tool="bash"
)
KEY = CONTEXT.key("bash", Channel.TOOL_PAYLOAD)

WAIT_SEC = 5.0
POLL_SEC = 0.01


def _journal(tmp_path: Path, reserve_bytes: int = 0) -> StreamJournal:
    return StreamJournal(DirVault(str(tmp_path / "vault")), reserve_bytes)


def _await(check: Callable[[], bool]) -> None:
    """Ожидание условия writer-потока: запись асинхронна по устройству."""
    deadline = time.monotonic() + WAIT_SEC

    while time.monotonic() < deadline:
        if check():
            return

        time.sleep(POLL_SEC)

    raise AssertionError("journal writer did not reach the expected state")


def _written(
    call: CallJournal,
    stage: str,
    channel: Channel,
    body: bytes,
    out: StreamFormat | None = None,
) -> None:
    """Канал стадии, записанный целиком; out — формат продукта её узла."""
    sink = call.sink(stage, channel, out)
    sink.feed(body)
    sink.close()


class TestKey:
    """Имя файла собирается и разбирается одним компонентом."""

    def test_name_carries_stage_and_channel(self) -> None:
        assert KEY.rel_log() == "t-1/call-1.bash.tool_payload.log"
        assert KEY.rel_meta() == "t-1/call-1.bash.tool_payload.meta.json"
        assert KEY.call_prefix == "t-1/call-1."

    def test_name_is_parsed_back(self) -> None:
        parsed = StreamKey.of_file("7", "t-1", "call-1.bash.tool_payload.log")

        assert parsed == KEY

    def test_sidecar_name_is_parsed_back(self) -> None:
        parsed = StreamKey.of_file("7", "t-1", "call-1.bash.tool_payload.meta.json")

        assert parsed == KEY

    def test_dot_in_call_id_is_refused(self) -> None:
        """Точка внутри call_id сделала бы имя неразложимым."""
        with pytest.raises(ValidationError):
            StreamKey(
                user_id="7",
                thread_id="t-1",
                call_id="call_uKx7pB2qX.0",
                stage="bash",
                channel=Channel.TOOL_STDOUT,
            )

    def test_dot_in_stage_is_refused(self) -> None:
        """Точка внутри стадии развалила бы разбор имени на сегменты."""
        with pytest.raises(ValidationError):
            StreamKey(
                user_id="7",
                thread_id="t-1",
                call_id="call-1",
                stage="pg.copy",
                channel=Channel.TOOL_STDOUT,
            )

    def test_traversal_is_refused(self) -> None:
        with pytest.raises(ValidationError):
            StreamKey(
                user_id="7",
                thread_id="t",
                call_id="../../etc/passwd",
                stage="bash",
                channel=Channel.TOOL_STDOUT,
            )

    def test_foreign_name_is_refused(self) -> None:
        with pytest.raises(ChannelError):
            StreamKey.of_file("7", "t-1", "call-1.log")

    def test_unknown_channel_is_refused(self) -> None:
        with pytest.raises(ChannelError):
            StreamKey.of_file("7", "t-1", "call-1.bash.nope.log")


class TestCallJournal:
    """Вызов пишет каждый канал в свой файл и закрывает их пометкой."""

    def test_every_channel_gets_its_own_file(self, tmp_path: Path) -> None:
        journal = _journal(tmp_path)
        call = journal.open(CONTEXT)

        _written(call, "bash", Channel.TOOL_STDOUT, b"human note\n")
        _written(call, "bash", Channel.TOOL_PAYLOAD, b"line-1\nline-2\n")
        _written(call, "g1", Channel.WRAP_STDERR, b"bwrap said\n")

        call.close(JournalNote.ABORTED.value)

        root = tmp_path / "vault" / "7" / "t-1"
        names = sorted(entry.name for entry in root.iterdir())

        assert "call-1.bash.tool_stdout.log" in names
        assert "call-1.bash.tool_payload.log" in names
        assert "call-1.g1.wrap_stderr.log" in names

        stdout_key = CONTEXT.key("bash", Channel.TOOL_STDOUT)
        window = journal.slice_at(stdout_key, 0)
        assert window is not None
        assert window.text == "human note\n"
        assert window.closed is True
        assert window.note == ""

    def test_keys_report_open_channels(self, tmp_path: Path) -> None:
        journal = _journal(tmp_path)
        call = journal.open(CONTEXT)

        _written(call, "bash", Channel.TOOL_STDOUT, b"a")
        _written(call, "bash", Channel.TOOL_RESULT, b"{}")

        call.close(JournalNote.ABORTED.value)

        channels = sorted(key.channel.value for key in call.journals())

        assert channels == ["tool_result", "tool_stdout"]

    def test_unfinished_channel_is_closed_with_a_note(self, tmp_path: Path) -> None:
        journal = _journal(tmp_path)
        call = journal.open(CONTEXT)

        sink = call.sink("bash", Channel.TOOL_PAYLOAD, None)
        sink.feed(b"half")

        call.close(JournalNote.ABORTED.value)

        window = journal.slice_at(KEY, 0)
        assert window is not None
        assert window.text == "half"
        assert window.closed is True
        assert window.note == JournalNote.ABORTED.value

    def test_missing_journal_reads_as_none(self, tmp_path: Path) -> None:
        journal = _journal(tmp_path)

        assert journal.slice_at(KEY, 0) is None

    def test_feed_after_close_is_ignored(self, tmp_path: Path) -> None:
        """Канал закрыт своим EOF: опоздавшие байты файл не трогают."""
        journal = _journal(tmp_path)
        call = journal.open(CONTEXT)

        sink = call.sink("bash", Channel.TOOL_PAYLOAD, None)
        sink.feed(b"until")
        sink.close()
        sink.feed(b" after")

        call.close(JournalNote.ABORTED.value)

        window = journal.slice_at(KEY, 0)
        assert window is not None
        assert window.text == "until"
        assert window.note == JournalNote.DONE.value

    def test_lost_meta_reads_as_closed(self, tmp_path: Path) -> None:
        """Сайдкар потерян: журнал читается как закрытый без итога."""
        journal = _journal(tmp_path)
        call = journal.open(CONTEXT)
        _written(call, "bash", Channel.TOOL_PAYLOAD, b"data")
        call.close(JournalNote.ABORTED.value)

        root = tmp_path / "vault" / "7"
        os.remove(root / KEY.rel_meta())

        window = journal.slice_at(KEY, 0)
        assert window is not None
        assert window.text == "data"
        assert window.closed is True
        assert window.note == ""

    def test_head_is_limited(self, tmp_path: Path) -> None:
        """Голова для модели режется по запрошенному потолку, а не по файлу."""
        journal = _journal(tmp_path)
        call = journal.open(CONTEXT)
        _written(call, "bash", Channel.TOOL_PAYLOAD, b"0123456789\n" * 10)
        call.close(JournalNote.ABORTED.value)

        head = journal.head(KEY, 25)

        assert head is not None
        assert head.text == "0123456789\n0123456789\n"
        assert head.size == 110


class TestJournalledChannels:
    """Журналируются все исходящие каналы стадии и только они."""

    OUTGOING: ClassVar[tuple[str, ...]] = (
        "call-1.bash.tool_payload.log",
        "call-1.bash.tool_result.log",
        "call-1.bash.tool_stderr.log",
        "call-1.bash.tool_stdout.log",
        "call-1.bash.wrap_result.log",
        "call-1.bash.wrap_stderr.log",
        "call-1.bash.wrap_stdout.log",
    )

    def test_incoming_channels_never_reach_the_disk(self, tmp_path: Path) -> None:
        """В tool_args едут креды: их файла в томе быть не должно."""
        journal = _journal(tmp_path)
        call = journal.open(CONTEXT)

        sinks = StageJournalSinks.of(call, "bash", list(Channel), StreamFormat.CSV)
        for channel, sink in sinks.items():
            sink.feed(f"{channel.value} body".encode())
            sink.close()

        call.close(JournalNote.ABORTED.value)

        root = tmp_path / "vault" / "7" / "t-1"
        logs: list[str] = []
        for entry in root.iterdir():
            if not entry.name.endswith(".log"):
                continue

            logs.append(entry.name)

        assert sorted(logs) == list(self.OUTGOING)

        journalled: list[str] = []
        for channel in sinks:
            journalled.append(channel.value)

        assert Channel.TOOL_ARGS.value not in journalled
        assert Channel.WRAP_ARGS.value not in journalled
        assert Channel.WRAP_ARGS_INNER.value not in journalled
        assert Channel.TOOL_STDIN.value not in journalled


class TestVanishingFiles:
    """Обход тома идёт по живому каталогу: снесённый файл не валит учёт."""

    def test_a_file_removed_mid_listing_is_skipped(self, tmp_path: Path) -> None:
        """Вытеснение сносит файлы прямо во время обхода — учёт это переживает."""
        journal = _journal(tmp_path)
        call = journal.open(CONTEXT)

        _written(call, "bash", Channel.TOOL_STDOUT, b"x" * 10)
        _written(call, "bash", Channel.TOOL_PAYLOAD, b"y" * 20)
        _written(call, "bash", Channel.TOOL_RESULT, b"{}")

        call.close(JournalNote.ABORTED.value)

        root = tmp_path / "vault" / "7"
        path = str(root / "t-1")

        files = journal._journal_files("7", "t-1", path)
        first = next(files)

        survivor = os.path.join(root, first.rel)
        for entry in os.listdir(path):
            name = os.path.join(path, entry)
            if name == survivor:
                continue

            os.remove(name)

        seen: list[str] = []
        for entry in files:
            seen.append(entry.rel)

        assert seen == []
        assert journal.usage("7").threads[0].bytes_used == os.stat(survivor).st_size

    CALLS: ClassVar[int] = 60
    """Столько вызовов в треде: обход успевает застать уборку соседей."""

    def test_usage_survives_files_vanishing_under_it(self, tmp_path: Path) -> None:
        """Учёт и листинг идут по живому тому: уборка соседей их не валит."""
        journal = _journal(tmp_path)

        for index in range(self.CALLS):
            context = ToolCallContext(
                user_id="7", thread_id="t-1", call_id=f"c-{index}", tool="bash"
            )
            call = journal.open(context)
            _written(call, "bash", Channel.TOOL_PAYLOAD, b"z" * 64)
            _written(call, "bash", Channel.TOOL_STDOUT, b"z" * 64)
            call.close(JournalNote.ABORTED.value)

        path = tmp_path / "vault" / "7" / "t-1"
        stopped = threading.Event()

        def sweep() -> None:
            for entry in sorted(path.iterdir()):
                if stopped.is_set():
                    return

                try:
                    entry.unlink()
                except FileNotFoundError:
                    continue

        sweeper = threading.Thread(target=sweep, name="sweeper", daemon=True)
        sweeper.start()

        while sweeper.is_alive():
            journal.usage("7")
            journal.keys_of("7", "t-1", "c-0")

        stopped.set()
        sweeper.join(WAIT_SEC)

        assert journal.usage("7").threads[0].bytes_used == 0

    def test_a_sidecar_draft_is_counted_and_evicted(self, tmp_path: Path) -> None:
        """Черновик сайдкара после падения процесса — часть вызова, не мусор."""
        journal = _journal(tmp_path, reserve_bytes=2**60)

        old = ToolCallContext(
            user_id="7", thread_id="t-1", call_id="c-old", tool="bash"
        )
        call = journal.open(old)
        _written(call, "bash", Channel.TOOL_PAYLOAD, b"data")
        call.close(JournalNote.ABORTED.value)

        root = tmp_path / "vault" / "7"
        old_meta = old.key("bash", Channel.TOOL_PAYLOAD).rel_meta()
        draft = Path(JournalFile.tmp_of(str(root / old_meta), 4242))
        draft.write_bytes(b"{}" * 8)

        calls = tuple(journal._thread_calls("7", "t-1"))

        on_disk = 0
        for entry in (root / "t-1").iterdir():
            on_disk += entry.stat().st_size

        assert len(calls) == 1
        assert f"t-1/{draft.name}" in calls[0].files
        assert calls[0].bytes_used == on_disk

        fresh = journal.open(CONTEXT)

        assert not draft.exists()

        fresh.close(JournalNote.ABORTED.value)


class TestLiveWriting:
    """Файл читается по ходу вызова, а подписчик узнаёт о каждой порции."""

    class Listener:
        """Подписчик реестра: считает события и будит ожидающего."""

        def __init__(self) -> None:
            self.opened: list[str] = []
            self.closed: list[str] = []
            self.data: list[StreamKey] = []
            self.wake = threading.Event()

        def on_open(self, call: CallJournal) -> None:
            self.opened.append(call.context.call_id)

        def on_data(self, key: StreamKey) -> None:
            self.data.append(key)
            self.wake.set()

        def on_close(self, call: CallJournal) -> None:
            self.closed.append(call.context.call_id)

    def test_bytes_are_readable_before_the_call_ends(self, tmp_path: Path) -> None:
        journal = _journal(tmp_path)
        listener = self.Listener()
        journal.registry.subscribe(listener)

        call = journal.open(CONTEXT)
        sink = call.sink("bash", Channel.TOOL_PAYLOAD, None)
        sink.feed(b"first chunk\n")

        assert listener.wake.wait(WAIT_SEC)

        window = journal.slice_at(KEY, 0)
        assert window is not None
        assert window.text == "first chunk\n"
        assert window.closed is False

        assert listener.opened == ["call-1"]
        assert listener.data[0] == KEY

        call.close(JournalNote.ABORTED.value)
        journal.registry.unsubscribe(listener)

        assert listener.closed == ["call-1"]

    def test_live_call_is_listed_in_the_registry(self, tmp_path: Path) -> None:
        journal = _journal(tmp_path)
        call = journal.open(CONTEXT)

        assert journal.registry.call_of("t-1", "call-1") is call
        assert journal.registry.live_prefixes() == frozenset({"t-1/call-1."})
        assert journal.registry.live_threads() == frozenset({"t-1"})

        call.close(JournalNote.ABORTED.value)

        assert journal.registry.call_of("t-1", "call-1") is None
        assert journal.registry.live_prefixes() == frozenset()


class TestWriterThread:
    """Диск обслуживает выделенный поток: цикл насоса его не ждёт."""

    SLOW_WRITE_SEC = 0.05
    CHUNKS = 20
    CHUNK = b"chunk\n"

    def test_feeding_does_not_wait_for_the_disk(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Медленный диск: приёмник возвращается сразу, файл дописывается потом."""
        journal = _journal(tmp_path)
        call = journal.open(CONTEXT)
        sink = call.sink("bash", Channel.TOOL_PAYLOAD, None)

        real_write = os.write

        def slow_write(fd: int, data: bytes) -> int:
            time.sleep(self.SLOW_WRITE_SEC)

            return real_write(fd, data)

        monkeypatch.setattr(journal_module.os, "write", slow_write)

        started = time.monotonic()
        for _ in range(self.CHUNKS):
            sink.feed(self.CHUNK)

        elapsed = time.monotonic() - started

        # синхронная запись стоила бы CHUNKS * SLOW_WRITE_SEC
        assert elapsed < self.CHUNKS * self.SLOW_WRITE_SEC / 4

        call.close(JournalNote.ABORTED.value)
        monkeypatch.undo()

        window = journal.slice_at(KEY, -1)
        assert window is not None
        assert window.size == self.CHUNKS * len(self.CHUNK)

    PROBE_SEC: ClassVar[float] = 1.0
    """Сколько ждём ответа свойства: заведомо меньше зависшей записи."""

    def test_the_close_flag_is_readable_while_a_write_is_stuck(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Диск завис: закрытие вызова опрашивает каналы, а не ждёт запись."""
        journal = _journal(tmp_path)
        root = journal.vault_root("7")
        DirVault.make(os.path.join(root, "t-1"))

        record = JournalRecord(
            KEY,
            os.path.join(root, KEY.rel_log()),
            os.path.join(root, KEY.rel_meta()),
            None,
        )

        entered = threading.Event()
        release = threading.Event()
        real_write = os.write

        def stuck_write(fd: int, data: bytes) -> int:
            entered.set()
            release.wait(WAIT_SEC)

            return real_write(fd, data)

        monkeypatch.setattr(journal_module.os, "write", stuck_write)

        writer = threading.Thread(
            target=record.write, args=(b"data",), name="stuck-write", daemon=True
        )
        writer.start()

        assert entered.wait(WAIT_SEC)

        answered = threading.Event()

        def probe() -> None:
            assert record.closed is False
            answered.set()

        threading.Thread(target=probe, name="probe", daemon=True).start()

        assert answered.wait(self.PROBE_SEC), "опрос закрытия ждёт зависшую запись"

        release.set()
        writer.join(WAIT_SEC)
        monkeypatch.undo()

        record.finish(JournalNote.DONE.value)

        assert record.closed is True

    def test_a_short_write_is_finished(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Ядро приняло байт из порции: хвост дописывается, размер не врёт."""
        journal = _journal(tmp_path)
        call = journal.open(CONTEXT)
        sink = call.sink("bash", Channel.TOOL_PAYLOAD, None)

        real_write = os.write

        def short_write(fd: int, data: bytes) -> int:
            return real_write(fd, bytes(data[:1]))

        monkeypatch.setattr(journal_module.os, "write", short_write)

        sink.feed(b"0123456789")
        sink.close()
        call.close(JournalNote.ABORTED.value)
        monkeypatch.undo()

        window = journal.slice_at(KEY, 0)
        assert window is not None
        assert window.text == "0123456789"
        assert window.size == 10


class TestWindowChains:
    """Окна стыкуются встык: цепочка в любую сторону собирает файл целиком."""

    LINES = 15000
    """~220 КБ трёхколоночного csv: больше трёх окон журнала."""

    def _written(self, tmp_path: Path) -> tuple[StreamJournal, bytes]:
        journal = _journal(tmp_path)
        call = journal.open(CONTEXT)

        body = b""
        for index in range(self.LINES):
            body += f"{index},{index & 0x3FFFFF},{index & 0x3FF}\n".encode()

        _written(call, "bash", Channel.TOOL_PAYLOAD, body)
        call.close(JournalNote.ABORTED.value)

        return journal, body

    def test_forward_chain_rebuilds_the_file(self, tmp_path: Path) -> None:
        journal, body = self._written(tmp_path)

        rebuilt = b""
        offset = 0
        while offset < len(body):
            piece = journal.slice_at(KEY, offset)
            assert piece is not None
            assert piece.offset == offset
            rebuilt += piece.text.encode()
            offset = piece.end

        assert rebuilt == body

    def test_backward_chain_rebuilds_the_file(self, tmp_path: Path) -> None:
        journal, body = self._written(tmp_path)

        rebuilt = b""
        end = len(body)
        while end > 0:
            piece = journal.slice_before(KEY, end)
            assert piece is not None
            assert piece.end == end
            rebuilt = piece.text.encode() + rebuilt
            end = piece.offset

        assert rebuilt == body

    def test_line_longer_than_the_window_still_flows(self, tmp_path: Path) -> None:
        """Строка длиннее окна отдаётся кусками: прогресс важнее выравнивания."""
        journal = _journal(tmp_path)
        call = journal.open(CONTEXT)
        _written(
            call, "bash", Channel.TOOL_PAYLOAD, b"x" * (3 * StreamJournal.WINDOW_BYTES)
        )
        call.close(JournalNote.ABORTED.value)

        first = journal.slice_at(KEY, 0)
        assert first is not None
        assert first.end == StreamJournal.WINDOW_BYTES

        second = journal.slice_at(KEY, first.end)
        assert second is not None
        assert second.offset == first.end
        assert second.end > second.offset

    def test_windows_start_at_line_boundaries(self, tmp_path: Path) -> None:
        journal, body = self._written(tmp_path)

        middle = journal.slice_before(KEY, len(body) // 2)
        assert middle is not None
        assert middle.offset > 0
        assert body[middle.offset - 1 : middle.offset] == b"\n"
        assert not middle.text.startswith("\n")

        tail = journal.slice_at(KEY, -1)
        assert tail is not None
        assert tail.end == len(body)


class TestUsageAndEviction:
    """Единица учёта и вытеснения — вызов целиком, со всеми каналами."""

    BULK_BYTES: ClassVar[int] = 4 * 1024 * 1024
    """Журнал заметного для statvfs размера: меньше блока учёта не видно."""

    MARGIN_BYTES: ClassVar[int] = 2 * 1024 * 1024
    """Нехватка резерва: её закрывает вытеснение ровно одного вызова."""

    @staticmethod
    def _fill(journal: StreamJournal, thread_id: str, call_id: str) -> None:
        context = ToolCallContext(
            user_id="7", thread_id=thread_id, call_id=call_id, tool="bash"
        )
        call = journal.open(context)
        _written(call, "bash", Channel.TOOL_STDOUT, b"x" * 100)
        _written(call, "bash", Channel.TOOL_PAYLOAD, b"y" * 200)
        call.close(JournalNote.ABORTED.value)

    @classmethod
    def _bulk(cls, journal: StreamJournal, call_id: str) -> None:
        """Закрытый вызов треда CONTEXT, чей журнал виден свободному месту."""
        context = ToolCallContext(
            user_id="7", thread_id="t-1", call_id=call_id, tool="bash"
        )
        call = journal.open(context)
        _written(call, "bash", Channel.TOOL_PAYLOAD, b"y" * cls.BULK_BYTES)
        call.close(JournalNote.ABORTED.value)

    def test_usage_counts_a_call_once(self, tmp_path: Path) -> None:
        journal = _journal(tmp_path)
        self._fill(journal, "t-1", "c-1")
        self._fill(journal, "t-1", "c-2")

        usage = journal.usage("7")

        assert len(usage.threads) == 1
        assert usage.threads[0].calls == 2
        assert usage.threads[0].bytes_used > 600

    def test_usage_lists_threads_oldest_first(self, tmp_path: Path) -> None:
        """Порядок отчёта — порядок уборки: старейший тред идёт первым."""
        journal = _journal(tmp_path)
        self._fill(journal, "t-old", "c-1")
        self._fill(journal, "t-new", "c-2")

        listed: list[str] = []
        for entry in journal.usage("7").threads:
            listed.append(entry.thread_id)

        assert listed == ["t-old", "t-new"]

    def test_purge_removes_the_thread(self, tmp_path: Path) -> None:
        journal = _journal(tmp_path)
        self._fill(journal, "t-1", "c-1")

        freed = journal.purge_thread("7", "t-1")

        assert freed > 300
        assert journal.purge_thread("7", "t-1") == 0

    def test_the_oldest_call_is_evicted_first(self, tmp_path: Path) -> None:
        """Резерв закрывается одним вытеснением: уходит старейший, свежий цел."""
        filler = _journal(tmp_path)
        self._bulk(filler, "c-old")
        self._bulk(filler, "c-new")

        root = tmp_path / "vault" / "7"
        space = os.statvfs(root)
        reserve = space.f_bavail * space.f_frsize + self.MARGIN_BYTES

        journal = StreamJournal(DirVault(str(tmp_path / "vault")), reserve)
        call = journal.open(CONTEXT)
        call.close(JournalNote.ABORTED.value)

        assert not (root / "t-1" / "c-old.bash.tool_payload.log").exists()
        assert (root / "t-1" / "c-new.bash.tool_payload.log").exists()

    def test_an_unremovable_call_gives_up_instead_of_looping(
        self, tmp_path: Path
    ) -> None:
        """Файлы жертвы не снимаются: резерв сдаётся, вызов идёт без журнала."""
        journal = _journal(tmp_path, reserve_bytes=2**60)
        self._fill(journal, "t-locked", "c-locked")

        locked = tmp_path / "vault" / "7" / "t-locked"
        os.chmod(locked, 0o500)

        opened: list[CallJournal] = []
        done = threading.Event()

        def open_call() -> None:
            opened.append(journal.open(CONTEXT))
            done.set()

        worker = threading.Thread(target=open_call, name="open-call", daemon=True)
        worker.start()
        returned = done.wait(WAIT_SEC)
        os.chmod(locked, 0o700)

        assert returned, "открытие журнала не вернулось: цикл вытеснения зациклен"
        assert (locked / "c-locked.bash.tool_stdout.log").exists()

        opened[0].close(JournalNote.ABORTED.value)

    def test_eviction_takes_all_channels_of_a_call(self, tmp_path: Path) -> None:
        """Резерв недостижим: незащищённый вызов уходит целиком, живой остаётся."""
        journal = _journal(tmp_path, reserve_bytes=2**60)
        self._fill(journal, "t-old", "c-old")

        call = journal.open(CONTEXT)
        _written(call, "bash", Channel.TOOL_STDOUT, b"fresh")

        root = tmp_path / "vault" / "7"

        assert not (root / "t-old").exists()
        assert (root / "t-1" / "call-1.bash.tool_stdout.log").exists()

        call.close(JournalNote.ABORTED.value)

    def test_a_live_call_never_loses_a_channel(self, tmp_path: Path) -> None:
        """Соседний вызов давит резервом: у живого остаются все его каналы."""
        journal = _journal(tmp_path, reserve_bytes=2**60)

        live = journal.open(CONTEXT)
        _written(live, "bash", Channel.TOOL_STDOUT, b"one")
        _written(live, "bash", Channel.TOOL_PAYLOAD, b"two")
        _written(live, "bash", Channel.TOOL_RESULT, b"{}")

        neighbour = ToolCallContext(
            user_id="7", thread_id="t-1", call_id="c-next", tool="bash"
        )
        other = journal.open(neighbour)
        _written(other, "bash", Channel.TOOL_STDOUT, b"three")

        root = tmp_path / "vault" / "7" / "t-1"
        names: list[str] = []
        for entry in root.iterdir():
            if not entry.name.startswith("call-1."):
                continue

            if not entry.name.endswith(".log"):
                continue

            names.append(entry.name)

        assert sorted(names) == [
            "call-1.bash.tool_payload.log",
            "call-1.bash.tool_result.log",
            "call-1.bash.tool_stdout.log",
        ]

        other.close(JournalNote.ABORTED.value)
        live.close(JournalNote.ABORTED.value)

    def test_live_call_of_the_same_thread_is_protected(self, tmp_path: Path) -> None:
        """Защищён живой вызов, а не тред: старый закрытый вызов уходит."""
        journal = _journal(tmp_path, reserve_bytes=2**60)
        self._fill(journal, "t-1", "c-old")

        call = journal.open(CONTEXT)
        _written(call, "bash", Channel.TOOL_STDOUT, b"fresh")

        root = tmp_path / "vault" / "7" / "t-1"

        assert not (root / "c-old.bash.tool_stdout.log").exists()
        assert (root / "call-1.bash.tool_stdout.log").exists()

        call.close(JournalNote.ABORTED.value)


class TestVaultBoundary:
    """Путь тома собирается только из проверенных сегментов.

    Тред для уборки называет модель, поэтому сегмент вне алфавита обязан
    падать на границе журнала, а не превращаться в путь за корнем.
    """

    OUTSIDE: ClassVar[str] = "precious"

    def _stand(self, tmp_path: Path) -> tuple[StreamJournal, Path]:
        journal = _journal(tmp_path)
        TestUsageAndEviction._fill(journal, "t-1", "c-1")

        outside = tmp_path / self.OUTSIDE
        outside.mkdir()
        (outside / "file").write_bytes(b"keep me")

        return journal, outside

    def test_purge_refuses_a_relative_escape(self, tmp_path: Path) -> None:
        journal, outside = self._stand(tmp_path)

        with pytest.raises(JournalError):
            journal.purge_thread("7", f"../../{self.OUTSIDE}")

        assert (outside / "file").exists()

    def test_purge_refuses_an_absolute_path(self, tmp_path: Path) -> None:
        journal, outside = self._stand(tmp_path)

        with pytest.raises(JournalError):
            journal.purge_thread("7", str(outside))

        assert (outside / "file").exists()

    def test_purge_refuses_a_neighbour_thread_of_another_user(
        self, tmp_path: Path
    ) -> None:
        """`../8` увёл бы удаление в том соседа, а не за корень."""
        journal, _outside = self._stand(tmp_path)
        TestUsageAndEviction._fill(journal, "t-2", "c-2")

        with pytest.raises(JournalError):
            journal.purge_thread("7", "../7/t-2")

        assert (tmp_path / "vault" / "7" / "t-2").exists()

    def test_listing_refuses_an_escape(self, tmp_path: Path) -> None:
        journal, _outside = self._stand(tmp_path)

        with pytest.raises(JournalError):
            journal.keys_of("7", f"../../{self.OUTSIDE}", "c-1")

        with pytest.raises(JournalError):
            journal.usage("../7")


class TestVaultPermissions:
    """Имена файлов несут id тредов и вызовов: том закрыт от соседей."""

    def test_files_and_dirs_belong_to_the_owner_alone(self, tmp_path: Path) -> None:
        journal = _journal(tmp_path)
        call = journal.open(CONTEXT)
        _written(call, "bash", Channel.TOOL_PAYLOAD, b"secret", StreamFormat.CSV)
        call.close(JournalNote.ABORTED.value)

        root = tmp_path / "vault" / "7"

        paths = [
            root,
            root / "t-1",
            root / KEY.rel_log(),
            root / KEY.rel_meta(),
        ]

        modes: list[int] = []
        for path in paths:
            modes.append(stat.S_IMODE(path.stat().st_mode))

        assert modes == [
            VaultMode.DIR,
            VaultMode.DIR,
            VaultMode.FILE,
            VaultMode.FILE,
        ]


class TestDeclaredProduct:
    """Формат продукта узла объявляет стадия, и он переживает вызов."""

    def test_the_format_is_readable_after_the_call(self, tmp_path: Path) -> None:
        journal = _journal(tmp_path)
        call = journal.open(CONTEXT)
        _written(call, "bash", Channel.TOOL_PAYLOAD, b"a,b\n", StreamFormat.CSV)
        _written(call, "bash", Channel.TOOL_STDOUT, b"note\n", StreamFormat.CSV)
        _written(call, "g1", Channel.WRAP_STDERR, b"bwrap\n", None)
        call.close(JournalNote.ABORTED.value)

        stdout_key = CONTEXT.key("bash", Channel.TOOL_STDOUT)
        group_key = CONTEXT.key("g1", Channel.WRAP_STDERR)

        assert journal.out_of(KEY) is StreamFormat.CSV
        assert journal.out_of(stdout_key) is StreamFormat.CSV
        assert journal.out_of(group_key) is None

    def test_the_format_is_readable_while_the_call_runs(self, tmp_path: Path) -> None:
        """Сайдкар пишется на открытии: панель знает формат до конца вызова."""
        journal = _journal(tmp_path)
        call = journal.open(CONTEXT)
        call.sink("bash", Channel.TOOL_PAYLOAD, StreamFormat.NDJSON)

        assert journal.out_of(KEY) is StreamFormat.NDJSON

        call.close(JournalNote.ABORTED.value)


class TestJournalFailure:
    """Сбой журнала гасится внутрь: приёмник отключается, стадия работает."""

    def test_buffer_overflow_disables_the_sink(self, tmp_path: Path) -> None:
        journal = _journal(tmp_path)
        call = journal.open(CONTEXT)

        sink = call.sink("bash", Channel.TOOL_PAYLOAD, None)
        sink.feed(b"head\n")
        sink.feed(b"z" * (JournalWriter.BUFFER_BYTES + 1))
        sink.feed(b"after the overflow\n")

        call.close(JournalNote.ABORTED.value)

        window = journal.slice_at(KEY, 0)
        assert window is not None
        assert window.text == "head\n"
        assert window.closed is True
        assert window.note == JournalNote.OVERFLOW.value

    def test_write_failure_closes_the_journal_not_the_stage(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ENOSPC без отдельной ФС не воспроизвести: подменяется os.write."""
        journal = _journal(tmp_path)
        call = journal.open(CONTEXT)

        sink = call.sink("bash", Channel.TOOL_PAYLOAD, None)
        sink.feed(b"head")

        def written() -> bool:
            window = journal.slice_at(KEY, 0)

            return window is not None and window.size == 4

        _await(written)

        def no_space(fd: int, data: bytes) -> int:
            raise OSError(errno.ENOSPC, "No space left on device")

        def closed() -> bool:
            window = journal.slice_at(KEY, 0)

            return window is not None and window.closed

        monkeypatch.setattr(journal_module.os, "write", no_space)
        sink.feed(b"overflow")
        _await(closed)
        monkeypatch.undo()

        sink.feed("после сбоя стадия пишет дальше".encode())
        call.close(JournalNote.ABORTED.value)

        window = journal.slice_at(KEY, -1)
        assert window is not None
        assert window.closed is True
        assert JournalNote.WRITE_FAILED.value in window.note
        assert window.text == "head"

    def test_unwritable_vault_leaves_the_call_without_a_file(
        self, tmp_path: Path
    ) -> None:
        """Каталог только на чтение: приёмник есть, файла нет, стадия цела."""
        vault = tmp_path / "vault" / "7" / "t-1"
        vault.mkdir(parents=True)
        os.chmod(vault, 0o500)

        journal = _journal(tmp_path)
        call = journal.open(CONTEXT)

        sink = call.sink("bash", Channel.TOOL_PAYLOAD, None)
        sink.feed(b"nothing lands")
        sink.close()
        call.close(JournalNote.ABORTED.value)

        os.chmod(vault, 0o700)

        assert journal.slice_at(KEY, 0) is None
        assert call.journals() == ()
