"""Журнал вывода инструментов: файлы каналов, окна, сайдкар и вытеснение."""

from __future__ import annotations

import errno
import os
from collections.abc import Callable
from pathlib import Path

import pytest
from pydantic import ValidationError

from boba.chainlit.data import stream_journal
from boba.chainlit.data.stream_journal import (
    DirVault,
    StreamJournal,
    StreamKey,
    StreamRecorder,
)
from boba.chainlit.domain.stream import (
    JournalFile,
    JournalWindow,
    LogName,
    StreamJournalError,
)
from boba.toolkit.channels import ToolChannel

KEY = StreamKey(user_id="7", thread_id="t-1", call_id="call-1")

STDOUT = ToolChannel.STDOUT
STDERR = ToolChannel.STDERR


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
    return journal.recorder(KEY, "bash", STDOUT, on_data, frozenset())


class TestKey:
    """Сегменты ключа — только безопасные символы: путь строится из них."""

    def test_traversal_is_refused(self) -> None:
        with pytest.raises(ValidationError):
            StreamKey(user_id="7", thread_id="t", call_id="../../etc/passwd")

    def test_dot_prefix_is_refused(self) -> None:
        with pytest.raises(ValidationError):
            StreamKey(user_id="7", thread_id=".hidden", call_id="c")

    def test_dot_inside_call_id_is_refused(self) -> None:
        """Точка внутри call_id сделала бы имя файла неразложимым."""
        with pytest.raises(ValidationError):
            StreamKey(user_id="7", thread_id="t-1", call_id="call_uKx7pB2qX.0")

    def test_rel_log_carries_tool_and_channel(self) -> None:
        key = StreamKey(user_id="7", thread_id="t-1", call_id="call_uKx7pB2qX0")
        if key.rel_log("bash", STDOUT) != "t-1/call_uKx7pB2qX0.bash.tool_stdout.log":
            raise AssertionError('key.rel_log("bash", STDOUT) == ( "t-1/call_uKx7pB2q…')


class TestVaultSegments:
    """Том проверяет сегмент сам: путь строится и из тех значений, что мимо ключа."""

    def test_vault_refuses_traversal(self, tmp_path: Path) -> None:
        vault = DirVault(str(tmp_path / "vault"))

        with pytest.raises(StreamJournalError):
            vault.root_for("../../etc")

        if (tmp_path / "etc").exists():
            raise AssertionError('not (tmp_path / "etc").exists()')

    def test_purge_refuses_traversal(self, tmp_path: Path) -> None:
        journal = _journal(tmp_path)
        outside = tmp_path / "outside"
        outside.mkdir()

        with pytest.raises(StreamJournalError):
            journal.purge_thread(KEY.user_id, "../../outside")

        if not (outside.is_dir()):
            raise AssertionError("outside.is_dir()")


class TestLogName:
    """Имя файла разбирается по сегментам с конца, не срезом суффикса."""

    def test_roundtrip(self) -> None:
        rel = JournalFile.rel_log("t-1", "c-1", "pg_query", STDERR)
        name = rel.split("/")[1]
        parsed = JournalFile.parse_log(name)
        if not (
            parsed == LogName(call_id="c-1", tool="pg_query", channel="tool_stderr")
        ):
            raise AssertionError('parsed == LogName( call_id="c-1", tool="pg_query", …')

    def test_foreign_name_groups_as_whole_stem(self) -> None:
        parsed = JournalFile.parse_log("legacy-call.log")
        if parsed.call_id != "legacy-call":
            raise AssertionError('parsed.call_id == "legacy-call"')
        if parsed.tool != "":
            raise AssertionError('parsed.tool == ""')

    def test_dots_in_segments_are_refused_at_render(self) -> None:
        with pytest.raises(ValueError, match="dots"):
            JournalFile.rel_log("t-1", "c.1", "bash", STDOUT)


class TestRecorder:
    """Запись по мере работы, идемпотентное закрытие, чтение окнами."""

    def test_written_bytes_are_readable_by_windows(self, tmp_path: Path) -> None:
        journal = _journal(tmp_path)
        recorder = _recorder(journal)

        recorder.feed(b"0123456789" * 20000)
        recorder.close("rc=0")

        piece = journal.slice_at(KEY, 0, STDOUT)
        if piece is None:
            raise AssertionError("piece is not None")
        if piece.size != 200000:
            raise AssertionError("piece.size == 200000")
        if len(piece.text.encode()) != JournalWindow.BYTES:
            raise AssertionError("len(piece.text.encode()) == JournalWindow.BYTES")

        middle = journal.slice_at(KEY, 100000, STDOUT)
        if middle is None:
            raise AssertionError("middle is not None")
        if middle.offset != 100000:
            raise AssertionError("middle.offset == 100000")
        if not (middle.text.startswith("0123456789")):
            raise AssertionError('middle.text.startswith("0123456789")')

        tail = journal.slice_at(KEY, -1, STDOUT)
        if tail is None:
            raise AssertionError("tail is not None")
        if tail.offset != 200000 - JournalWindow.BYTES:
            raise AssertionError("tail.offset == 200000 - JournalWindow.BYTES")
        if tail.closed is not True:
            raise AssertionError("tail.closed is True")
        if tail.note != "rc=0":
            raise AssertionError('tail.note == "rc=0"')

    def test_channels_are_separate_files(self, tmp_path: Path) -> None:
        journal = _journal(tmp_path)
        out = journal.recorder(KEY, "bash", STDOUT, _wake, frozenset())
        err = journal.recorder(KEY, "bash", STDERR, _wake, frozenset())

        out.feed(b"body")
        err.feed(b"trace")
        out.close("rc=0")
        err.close("rc=0")

        stdout_piece = journal.slice_at(KEY, 0, STDOUT)
        stderr_piece = journal.slice_at(KEY, 0, STDERR)
        if stdout_piece is None:
            raise AssertionError("stdout_piece is not None")
        if stderr_piece is None:
            raise AssertionError("stderr_piece is not None")
        if stdout_piece.text != "body":
            raise AssertionError('stdout_piece.text == "body"')
        if stderr_piece.text != "trace":
            raise AssertionError('stderr_piece.text == "trace"')

    def test_feed_after_close_is_ignored(self, tmp_path: Path) -> None:
        journal = _journal(tmp_path)
        recorder = _recorder(journal)

        recorder.feed(b"until")
        recorder.close("done")
        recorder.feed(b" after")
        recorder.close("другая причина")

        piece = journal.slice_at(KEY, 0, STDOUT)
        if piece is None:
            raise AssertionError("piece is not None")
        if piece.text != "until":
            raise AssertionError('piece.text == "until"')
        if piece.note != "done":
            raise AssertionError('piece.note == "done"')

    def test_on_data_fires_on_feed_and_close(self, tmp_path: Path) -> None:
        wakes: list[int] = []

        def wake() -> None:
            wakes.append(1)

        recorder = _recorder(_journal(tmp_path), wake)
        recorder.feed(b"")
        if wakes != []:
            raise AssertionError("wakes == []")

        recorder.feed(b"x")
        recorder.close("done")
        if len(wakes) != 2:
            raise AssertionError("len(wakes) == 2")

    def test_live_recorder_is_readable_while_writing(self, tmp_path: Path) -> None:
        journal = _journal(tmp_path)
        recorder = _recorder(journal)

        recorder.feed(b"live ")
        first = recorder.tail(JournalWindow.BYTES)
        recorder.feed(b"tail")
        second = recorder.tail(JournalWindow.BYTES)

        if first.text != "live ":
            raise AssertionError('first.text == "live "')
        if first.closed is not False:
            raise AssertionError("first.closed is False")
        if second.text != "live tail":
            raise AssertionError('second.text == "live tail"')

    def test_missing_journal_is_none(self, tmp_path: Path) -> None:
        journal = _journal(tmp_path)

        if journal.slice_at(KEY, 0, STDOUT) is not None:
            raise AssertionError("journal.slice_at(KEY, 0, STDOUT) is None")

    def test_lost_meta_reads_as_missing(self, tmp_path: Path) -> None:
        """Без сайдкара имя инструмента не восстановить — файла канала «нет»."""
        journal = _journal(tmp_path)
        recorder = _recorder(journal)
        recorder.feed(b"data")
        recorder.close("rc=0")

        root = DirVault(str(tmp_path / "vault")).root_for(KEY.user_id)
        os.remove(os.path.join(root, KEY.rel_meta()))

        if journal.slice_at(KEY, 0, STDOUT) is not None:
            raise AssertionError("journal.slice_at(KEY, 0, STDOUT) is None")


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
            piece = journal.slice_at(KEY, offset, STDOUT)
            if piece is None:
                raise AssertionError("piece is not None")
            if piece.offset != offset:
                raise AssertionError("piece.offset == offset")
            rebuilt += piece.text.encode()
            offset = piece.end

        if rebuilt != body:
            raise AssertionError("rebuilt == body")

    def test_backward_chain_rebuilds_the_file(self, tmp_path: Path) -> None:
        journal, body = self._written(tmp_path)

        rebuilt = b""
        end = len(body)
        while end > 0:
            piece = journal.slice_before(KEY, end, STDOUT)
            if piece is None:
                raise AssertionError("piece is not None")
            if piece.end != end:
                raise AssertionError("piece.end == end")
            rebuilt = piece.text.encode() + rebuilt
            end = piece.offset

        if rebuilt != body:
            raise AssertionError("rebuilt == body")

    def test_windows_start_at_line_boundaries(self, tmp_path: Path) -> None:
        journal, body = self._written(tmp_path)

        middle = journal.slice_before(KEY, len(body) // 2, STDOUT)
        if middle is None:
            raise AssertionError("middle is not None")
        if middle.offset <= 0:
            raise AssertionError("middle.offset > 0")
        if body[middle.offset - 1 : middle.offset] != b"\n":
            raise AssertionError('body[middle.offset - 1 : middle.offset] == b"\\n"')
        if middle.text.startswith("\n"):
            raise AssertionError('not middle.text.startswith("\\n")')

        tail = journal.slice_at(KEY, -1, STDOUT)
        if tail is None:
            raise AssertionError("tail is not None")
        if body[tail.offset - 1 : tail.offset] != b"\n":
            raise AssertionError('body[tail.offset - 1 : tail.offset] == b"\\n"')
        if tail.end != len(body):
            raise AssertionError("tail.end == len(body)")

    def test_line_longer_than_the_window_still_flows(self, tmp_path: Path) -> None:
        """Строка длиннее окна отдаётся кусками: прогресс важнее выравнивания."""
        journal = _journal(tmp_path)
        recorder = _recorder(journal)
        recorder.feed(b"x" * (3 * JournalWindow.BYTES))
        recorder.close("rc=0")

        first = journal.slice_at(KEY, 0, STDOUT)
        if first is None:
            raise AssertionError("first is not None")
        if first.end != JournalWindow.BYTES:
            raise AssertionError("first.end == JournalWindow.BYTES")

        second = journal.slice_at(KEY, first.end, STDOUT)
        if second is None:
            raise AssertionError("second is not None")
        if second.offset != first.end:
            raise AssertionError("second.offset == first.end")
        if second.end <= second.offset:
            raise AssertionError("second.end > second.offset")


class TestUsageAndPurge:
    """Занятость тома по тредам и адресная уборка."""

    @staticmethod
    def _fill(journal: StreamJournal, thread_id: str, body: bytes) -> None:
        key = StreamKey(user_id="7", thread_id=thread_id, call_id="c-1")
        recorder = journal.recorder(key, "bash", STDOUT, _wake, frozenset())
        recorder.feed(body)
        recorder.close("rc=0")

    def test_usage_lists_threads_oldest_first(self, tmp_path: Path) -> None:
        journal = _journal(tmp_path)
        self._fill(journal, "t-old", b"x" * 100)
        old_log = tmp_path / "vault" / "7" / "t-old" / "c-1.bash.tool_stdout.log"
        os.utime(old_log, (1000.0, 1000.0))
        self._fill(journal, "t-new", b"y" * 5000)

        usage = journal.usage("7")

        if usage.total_bytes <= 0:
            raise AssertionError("usage.total_bytes > 0")
        names = [entry.thread_id for entry in usage.threads]
        if names != ["t-old", "t-new"]:
            raise AssertionError('names == ["t-old", "t-new"]')
        by_name = {entry.thread_id: entry for entry in usage.threads}
        if by_name["t-new"].bytes_used <= 5000:
            raise AssertionError('by_name["t-new"].bytes_used > 5000')
        if by_name["t-new"].calls != 1:
            raise AssertionError('by_name["t-new"].calls == 1')

    def test_channels_of_one_call_count_as_one(self, tmp_path: Path) -> None:
        """Три файла каналов одного вызова — один вызов в учёте треда."""
        journal = _journal(tmp_path)
        for channel in (STDOUT, STDERR, ToolChannel.RESULT):
            recorder = journal.recorder(KEY, "bash", channel, _wake, frozenset())
            recorder.feed(b"x")
            recorder.close("rc=0")

        usage = journal.usage("7")

        by_name = {entry.thread_id: entry for entry in usage.threads}
        if by_name["t-1"].calls != 1:
            raise AssertionError('by_name["t-1"].calls == 1')

    def test_purge_removes_the_thread(self, tmp_path: Path) -> None:
        journal = _journal(tmp_path)
        self._fill(journal, "t-1", b"z" * 3000)

        freed = journal.purge_thread("7", "t-1")

        if freed <= 3000:
            raise AssertionError("freed > 3000")
        if journal.slice_at(KEY, 0, STDOUT) is not None:
            raise AssertionError("journal.slice_at(KEY, 0, STDOUT) is None")
        if journal.purge_thread("7", "t-1") != 0:
            raise AssertionError('journal.purge_thread("7", "t-1") == 0')

    def test_rotation_evicts_oldest_unprotected(self, tmp_path: Path) -> None:
        """Резерв больше свободного места: старые логи вытесняются.

        Каталог-том резерв считает по диску хоста, поэтому резерв задаётся
        заведомо недостижимым: ротация обязана удалить всё незащищённое и
        отступиться от защищённого вызова.
        """
        journal = StreamJournal(DirVault(str(tmp_path / "vault")), reserve_bytes=2**60)
        self._fill(journal, "t-old", b"a" * 100)
        self._fill(journal, "t-protected", b"b" * 100)

        key = StreamKey(user_id="7", thread_id="t-cur", call_id="c-2")
        journal.recorder(key, "bash", STDOUT, _wake, frozenset({"t-protected/c-1."}))

        root = tmp_path / "vault" / "7"
        if (root / "t-old").exists():
            raise AssertionError('not (root / "t-old").exists()')
        if not ((root / "t-protected" / "c-1.bash.tool_stdout.log").exists()):
            raise AssertionError('(root / "t-protected" / "c-1.bash.tool_stdout.log")…')
        if not ((root / "t-cur" / "c-2.bash.tool_stdout.log").exists()):
            raise AssertionError('(root / "t-cur" / "c-2.bash.tool_stdout.log").exist…')

    def test_eviction_takes_the_call_with_all_channels(self, tmp_path: Path) -> None:
        """Вызов вытесняется целиком: все каналы и сайдкар, не один файл."""
        journal = _journal(tmp_path)
        old_key = StreamKey(user_id="7", thread_id="t-1", call_id="c-old")
        for channel in (STDOUT, STDERR):
            recorder = journal.recorder(old_key, "bash", channel, _wake, frozenset())
            recorder.feed(b"a" * 100)
            recorder.close("rc=0")

        rotating = StreamJournal(DirVault(str(tmp_path / "vault")), reserve_bytes=2**60)
        fresh_key = StreamKey(user_id="7", thread_id="t-1", call_id="c-new")
        rotating.recorder(fresh_key, "bash", STDOUT, _wake, frozenset())

        root = tmp_path / "vault" / "7" / "t-1"
        if (root / "c-old.bash.tool_stdout.log").exists():
            raise AssertionError('not (root / "c-old.bash.tool_stdout.log").exists()')
        if (root / "c-old.bash.tool_stderr.log").exists():
            raise AssertionError('not (root / "c-old.bash.tool_stderr.log").exists()')
        if (root / "c-old.meta.json").exists():
            raise AssertionError('not (root / "c-old.meta.json").exists()')
        if not ((root / "c-new.bash.tool_stdout.log").exists()):
            raise AssertionError('(root / "c-new.bash.tool_stdout.log").exists()')

    def test_closed_call_of_current_thread_is_evictable(self, tmp_path: Path) -> None:
        """Защищён живой вызов, не тред: старый закрытый лог треда уходит."""
        journal = StreamJournal(DirVault(str(tmp_path / "vault")), reserve_bytes=2**60)
        old_key = StreamKey(user_id="7", thread_id="t-1", call_id="c-old")
        recorder = journal.recorder(old_key, "bash", STDOUT, _wake, frozenset())
        recorder.feed(b"a" * 100)
        recorder.close("rc=0")

        fresh_key = StreamKey(user_id="7", thread_id="t-1", call_id="c-new")
        journal.recorder(fresh_key, "bash", STDOUT, _wake, frozenset())

        root = tmp_path / "vault" / "7"
        if (root / "t-1" / "c-old.bash.tool_stdout.log").exists():
            raise AssertionError('not (root / "t-1" / "c-old.bash.tool_stdout.log").e…')
        if not ((root / "t-1" / "c-new.bash.tool_stdout.log").exists()):
            raise AssertionError('(root / "t-1" / "c-new.bash.tool_stdout.log").exist…')


class TestWriteFailure:
    """Кончилось место на точке монтирования: журнал гаснет, инструмент живёт.

    ENOSPC на tmp_path не воспроизвести без отдельной ФС, поэтому os.write
    подменяется — интеграционный путь недоступен.
    """

    def test_enospc_closes_the_journal_not_the_tool(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        journal = _journal(tmp_path)
        recorder = journal.recorder(KEY, "bash", STDOUT, _wake, frozenset())
        recorder.feed(b"head")

        def no_space(fd: int, data: bytes) -> int:
            raise OSError(errno.ENOSPC, "No space left on device")

        monkeypatch.setattr(stream_journal.os, "write", no_space)
        recorder.feed(b"overflow")

        monkeypatch.undo()
        recorder.feed("после закрытия не падает".encode())

        if recorder.closed is not True:
            raise AssertionError("recorder.closed is True")

        piece = journal.slice_at(KEY, -1, STDOUT)
        if piece is None:
            raise AssertionError("piece is not None")
        if piece.closed is not True:
            raise AssertionError("piece.closed is True")
        if "journal stopped" not in piece.note:
            raise AssertionError('"journal stopped" in piece.note')
        if piece.text != "head":
            raise AssertionError('piece.text == "head"')
