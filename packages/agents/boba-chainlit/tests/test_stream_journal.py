"""Журнал вывода инструментов: файлы, окна, сайдкар и вытеснение."""

from __future__ import annotations

import errno
import os
from collections.abc import Callable
from pathlib import Path

import pytest
from pydantic import ValidationError

from boba.chainlit.chat.data import stream_journal
from boba.chainlit.chat.data.stream_journal import (
    DirVault,
    StreamJournal,
    StreamKey,
    StreamRecorder,
)

KEY = StreamKey(user_id="7", thread_id="t-1", call_id="call-1")


@pytest.fixture(autouse=True)
def chainlit_context() -> None:
    "журнал не зависит от сессии chainlit"


def _wake() -> None:
    pass


def _journal(tmp_path: Path) -> StreamJournal:
    return StreamJournal(DirVault(str(tmp_path / "vault")), reserve_bytes=0)


def _recorder(
    journal: StreamJournal, on_data: Callable[[], None] = _wake
) -> StreamRecorder:
    return journal.recorder(KEY, "bash", on_data, frozenset())


class TestKey:
    """Сегменты ключа — только безопасные символы: путь строится из них."""

    def test_traversal_is_refused(self) -> None:
        with pytest.raises(ValidationError):
            StreamKey(user_id="7", thread_id="t", call_id="../../etc/passwd")

    def test_dot_prefix_is_refused(self) -> None:
        with pytest.raises(ValidationError):
            StreamKey(user_id="7", thread_id=".hidden", call_id="c")

    def test_normal_langchain_call_id_passes(self) -> None:
        key = StreamKey(
            user_id="7", thread_id="t-1", call_id="call_uKx7pB2qX.0"
        )
        assert key.rel_log() == "t-1/call_uKx7pB2qX.0.log"


class TestRecorder:
    """Запись по мере работы, идемпотентное закрытие, чтение окнами."""

    def test_written_bytes_are_readable_by_windows(self, tmp_path: Path) -> None:
        journal = _journal(tmp_path)
        recorder = _recorder(journal)

        recorder.feed(b"0123456789" * 20000)
        recorder.close("rc=0")

        piece = journal.slice_at(KEY, 0)
        assert piece is not None
        assert piece.size == 200000
        assert len(piece.text.encode()) == StreamJournal.WINDOW_BYTES

        middle = journal.slice_at(KEY, 100000)
        assert middle is not None
        assert middle.offset == 100000
        assert middle.text.startswith("0123456789")

        tail = journal.slice_at(KEY, -1)
        assert tail is not None
        assert tail.offset == 200000 - StreamJournal.WINDOW_BYTES
        assert tail.closed is True
        assert tail.note == "rc=0"

    def test_feed_after_close_is_ignored(self, tmp_path: Path) -> None:
        journal = _journal(tmp_path)
        recorder = _recorder(journal)

        recorder.feed(b"until")
        recorder.close("done")
        recorder.feed(b" after")
        recorder.close("другая причина")

        piece = journal.slice_at(KEY, 0)
        assert piece is not None
        assert piece.text == "until"
        assert piece.note == "done"

    def test_on_data_fires_on_feed_and_close(self, tmp_path: Path) -> None:
        wakes: list[int] = []

        def wake() -> None:
            wakes.append(1)

        recorder = _recorder(_journal(tmp_path), wake)
        recorder.feed(b"")
        assert wakes == []

        recorder.feed(b"x")
        recorder.close("done")
        assert len(wakes) == 2

    def test_live_recorder_is_readable_while_writing(
        self, tmp_path: Path
    ) -> None:
        journal = _journal(tmp_path)
        recorder = _recorder(journal)

        recorder.feed(b"live ")
        first = recorder.tail(StreamJournal.WINDOW_BYTES)
        recorder.feed(b"tail")
        second = recorder.tail(StreamJournal.WINDOW_BYTES)

        assert first.text == "live "
        assert first.closed is False
        assert second.text == "live tail"

    def test_missing_journal_is_none(self, tmp_path: Path) -> None:
        journal = _journal(tmp_path)

        assert journal.slice_at(KEY, 0) is None

    def test_lost_meta_reads_as_closed(self, tmp_path: Path) -> None:
        journal = _journal(tmp_path)
        recorder = _recorder(journal)
        recorder.feed(b"data")
        recorder.close("rc=0")

        root = DirVault(str(tmp_path / "vault")).root_for(KEY.user_id)
        os.remove(os.path.join(root, KEY.rel_meta()))

        piece = journal.slice_at(KEY, 0)
        assert piece is not None
        assert piece.closed is True
        assert piece.note == ""


class TestWindowChains:
    """Окна стыкуются встык: цепочка в любую сторону собирает файл целиком.

    Голова каждого окна (кроме начала файла) — начало строки: прокрутка
    никогда не показывает рваные строки на краях.
    """

    LINES = 15000
    """~220 КБ трёхколоночного csv: больше трёх окон журнала."""

    def _written(self, tmp_path: Path) -> tuple[StreamJournal, bytes]:
        journal = _journal(tmp_path)
        recorder = _recorder(journal)
        body = b""
        for index in range(self.LINES):
            body += f"{index},{index & 0x3FFFFF},{index & 0x3FF}\n".encode()
        recorder.feed(body)
        recorder.close("rc=0")
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

    def test_windows_start_at_line_boundaries(self, tmp_path: Path) -> None:
        journal, body = self._written(tmp_path)

        middle = journal.slice_before(KEY, len(body) // 2)
        assert middle is not None
        assert middle.offset > 0
        assert body[middle.offset - 1 : middle.offset] == b"\n"
        assert not middle.text.startswith("\n")

        tail = journal.slice_at(KEY, -1)
        assert tail is not None
        assert body[tail.offset - 1 : tail.offset] == b"\n"
        assert tail.end == len(body)

    def test_line_longer_than_the_window_still_flows(
        self, tmp_path: Path
    ) -> None:
        """Строка длиннее окна отдаётся кусками: прогресс важнее выравнивания."""
        journal = _journal(tmp_path)
        recorder = _recorder(journal)
        recorder.feed(b"x" * (3 * StreamJournal.WINDOW_BYTES))
        recorder.close("rc=0")

        first = journal.slice_at(KEY, 0)
        assert first is not None
        assert first.end == StreamJournal.WINDOW_BYTES

        second = journal.slice_at(KEY, first.end)
        assert second is not None
        assert second.offset == first.end
        assert second.end > second.offset


class TestUsageAndPurge:
    """Занятость тома по тредам и адресная уборка."""

    @staticmethod
    def _fill(journal: StreamJournal, thread_id: str, body: bytes) -> None:
        key = StreamKey(user_id="7", thread_id=thread_id, call_id="c-1")
        recorder = journal.recorder(key, "bash", _wake, frozenset())
        recorder.feed(body)
        recorder.close("rc=0")

    def test_usage_lists_threads_oldest_first(self, tmp_path: Path) -> None:
        journal = _journal(tmp_path)
        self._fill(journal, "t-old", b"x" * 100)
        os.utime(
            tmp_path / "vault" / "7" / "t-old" / "c-1.log", (1000.0, 1000.0)
        )
        self._fill(journal, "t-new", b"y" * 5000)

        usage = journal.usage("7")

        assert usage.total_bytes > 0
        names = [entry.thread_id for entry in usage.threads]
        assert names == ["t-old", "t-new"]
        by_name = {entry.thread_id: entry for entry in usage.threads}
        assert by_name["t-new"].bytes_used > 5000
        assert by_name["t-new"].calls == 1

    def test_purge_removes_the_thread(self, tmp_path: Path) -> None:
        journal = _journal(tmp_path)
        self._fill(journal, "t-1", b"z" * 3000)

        freed = journal.purge_thread("7", "t-1")

        assert freed > 3000
        assert journal.slice_at(KEY, 0) is None
        assert journal.purge_thread("7", "t-1") == 0

    def test_rotation_evicts_oldest_unprotected(self, tmp_path: Path) -> None:
        """Резерв больше свободного места: старые логи вытесняются.

        Каталог-том резерв считает по диску хоста, поэтому резерв задаётся
        заведомо недостижимым: ротация обязана удалить всё незащищённое и
        отступиться от защищённого лога.
        """
        journal = StreamJournal(
            DirVault(str(tmp_path / "vault")), reserve_bytes=2**60
        )
        self._fill(journal, "t-old", b"a" * 100)
        self._fill(journal, "t-protected", b"b" * 100)

        key = StreamKey(user_id="7", thread_id="t-cur", call_id="c-2")
        journal.recorder(key, "bash", _wake, frozenset({"t-protected/c-1.log"}))

        root = tmp_path / "vault" / "7"
        assert not (root / "t-old").exists()
        assert (root / "t-protected" / "c-1.log").exists()
        assert (root / "t-cur" / "c-2.log").exists()

    def test_closed_call_of_current_thread_is_evictable(
        self, tmp_path: Path
    ) -> None:
        """Защищён живой вызов, не весь тред: старый закрытый лог того же треда уходит."""
        journal = StreamJournal(
            DirVault(str(tmp_path / "vault")), reserve_bytes=2**60
        )
        old_key = StreamKey(user_id="7", thread_id="t-1", call_id="c-old")
        recorder = journal.recorder(old_key, "bash", _wake, frozenset())
        recorder.feed(b"a" * 100)
        recorder.close("rc=0")

        fresh_key = StreamKey(user_id="7", thread_id="t-1", call_id="c-new")
        journal.recorder(fresh_key, "bash", _wake, frozenset())

        root = tmp_path / "vault" / "7"
        assert not (root / "t-1" / "c-old.log").exists()
        assert (root / "t-1" / "c-new.log").exists()


class TestWriteFailure:
    """Кончилось место на точке монтирования: журнал гаснет, инструмент живёт.

    ENOSPC на tmp_path не воспроизвести без отдельной ФС, поэтому os.write
    подменяется — интеграционный путь недоступен.
    """

    def test_enospc_closes_the_journal_not_the_tool(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        journal = _journal(tmp_path)
        recorder = journal.recorder(KEY, "bash", _wake, frozenset())
        recorder.feed(b"head")

        def no_space(fd: int, data: bytes) -> int:
            raise OSError(errno.ENOSPC, "No space left on device")

        monkeypatch.setattr(stream_journal.os, "write", no_space)
        recorder.feed(b"overflow")

        monkeypatch.undo()
        recorder.feed("после закрытия не падает".encode())

        assert recorder.closed is True

        piece = journal.slice_at(KEY, -1)
        assert piece is not None
        assert piece.closed is True
        assert "journal stopped" in piece.note
        assert piece.text == "head"
