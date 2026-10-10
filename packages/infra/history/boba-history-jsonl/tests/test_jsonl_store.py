"""Хранилище JSONL на настоящих файлах: захват, права, запечатывание хвоста,
повтор пачки, чтение рваной строки, отказы файловой системы."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from boba.agent.history import BrokenLine, HistoryError, SessionId, StoredLine
from boba.agent.records import (
    PermissionMode,
    PermissionModeRecord,
    UserMessage,
    UserRecord,
)
from boba.history.jsonl import JsonlHistoryStore

pytestmark = pytest.mark.anyio

SESSION = SessionId(value="abc-123")
STAMP = "2026-10-10T10:00:00.000Z"


def mode(seq: int) -> PermissionModeRecord:
    return PermissionModeRecord(
        timestamp=STAMP,
        session_id=SESSION.value,
        seq=seq,
        permission_mode=PermissionMode.DEFAULT,
    )


def user(seq: int, text: str) -> UserRecord:
    return UserRecord(
        uuid=f"u-{seq}",
        parent_uuid=None,
        timestamp=STAMP,
        session_id=SESSION.value,
        version="0.0.26.dev4",
        seq=seq,
        message=UserMessage(content=text),
    )


class TestAcquire:
    async def test_creates_the_journal_with_private_modes(self, tmp_path: Path) -> None:
        root = tmp_path / "history"
        store = JsonlHistoryStore(root)

        lease = await store.acquire(SESSION)
        try:
            path = root / "abc-123.jsonl"
            assert path.is_file()
            assert stat.S_IMODE(root.stat().st_mode) == 0o700
            assert stat.S_IMODE(path.stat().st_mode) == 0o600
        finally:
            await store.release(lease)

    async def test_second_acquire_is_refused_while_held(self, tmp_path: Path) -> None:
        store = JsonlHistoryStore(tmp_path)
        other = JsonlHistoryStore(tmp_path)

        lease = await store.acquire(SESSION)
        try:
            with pytest.raises(HistoryError, match="already in use by another process"):
                await other.acquire(SESSION)
        finally:
            await store.release(lease)

        again = await other.acquire(SESSION)
        await other.release(again)

    async def test_read_only_root_is_an_error_with_the_cause(
        self, tmp_path: Path
    ) -> None:
        root = tmp_path / "frozen"
        root.mkdir(mode=0o500)
        store = JsonlHistoryStore(root)
        try:
            with pytest.raises(HistoryError, match="Could not open the journal"):
                await store.acquire(SESSION)
        finally:
            root.chmod(0o700)

    async def test_append_with_a_foreign_lease_is_refused(
        self, tmp_path: Path
    ) -> None:
        store = JsonlHistoryStore(tmp_path)
        other = JsonlHistoryStore(tmp_path / "other")
        lease = await other.acquire(SESSION)
        try:
            with pytest.raises(HistoryError, match="lease is unknown"):
                await store.append(lease, [mode(1)])
        finally:
            await other.release(lease)


class TestAppend:
    async def test_batch_is_written_in_order_and_read_back(
        self, tmp_path: Path
    ) -> None:
        store = JsonlHistoryStore(tmp_path)
        lease = await store.acquire(SESSION)
        try:
            await store.append(lease, [mode(1), user(2, "a"), user(3, "b")])
        finally:
            await store.release(lease)

        lines = [line async for line in store.read(SESSION)]

        assert [line.number for line in lines] == [1, 2, 3]
        stored = [line for line in lines if isinstance(line, StoredLine)]
        assert [line.raw["seq"] for line in stored] == [1, 2, 3]
        assert stored[1].raw["message"]["content"] == "a"

    async def test_repeated_seq_is_not_duplicated(self, tmp_path: Path) -> None:
        store = JsonlHistoryStore(tmp_path)
        lease = await store.acquire(SESSION)
        try:
            await store.append(lease, [user(1, "a"), user(2, "b")])
            await store.append(lease, [user(1, "a"), user(2, "b"), user(3, "c")])
        finally:
            await store.release(lease)

        lines = [line async for line in store.read(SESSION)]

        assert len(lines) == 3

    async def test_reopened_journal_knows_its_last_seq(self, tmp_path: Path) -> None:
        store = JsonlHistoryStore(tmp_path)
        lease = await store.acquire(SESSION)
        try:
            await store.append(lease, [user(1, "a"), user(2, "b")])
        finally:
            await store.release(lease)

        again = JsonlHistoryStore(tmp_path)
        lease = await again.acquire(SESSION)
        try:
            await again.append(lease, [user(2, "b"), user(3, "c")])
        finally:
            await again.release(lease)

        lines = [line async for line in again.read(SESSION)]
        assert [line.number for line in lines] == [1, 2, 3]

    async def test_out_of_order_batch_is_refused(self, tmp_path: Path) -> None:
        store = JsonlHistoryStore(tmp_path)
        lease = await store.acquire(SESSION)
        try:
            with pytest.raises(HistoryError, match="out of order"):
                await store.append(lease, [user(2, "b"), user(1, "a")])
        finally:
            await store.release(lease)

    async def test_torn_tail_is_sealed_before_the_first_append(
        self, tmp_path: Path
    ) -> None:
        store = JsonlHistoryStore(tmp_path)
        lease = await store.acquire(SESSION)
        try:
            await store.append(lease, [user(1, "a")])
        finally:
            await store.release(lease)
        path = tmp_path / "abc-123.jsonl"
        with path.open("a", encoding="utf-8") as out:
            out.write('{"type": "user", "uuid": "torn')

        again = JsonlHistoryStore(tmp_path)
        lease = await again.acquire(SESSION)
        try:
            await again.append(lease, [user(2, "b")])
        finally:
            await again.release(lease)

        lines = [line async for line in again.read(SESSION)]
        assert isinstance(lines[1], BrokenLine)
        assert "not JSON" in lines[1].reason
        assert isinstance(lines[2], StoredLine)
        assert lines[2].raw["seq"] == 2

    async def test_unwritable_journal_is_an_error_with_the_cause(
        self, tmp_path: Path
    ) -> None:
        store = JsonlHistoryStore(tmp_path)
        lease = await store.acquire(SESSION)
        path = tmp_path / "abc-123.jsonl"
        try:
            await store.append(lease, [user(1, "a")])
            path.chmod(0o400)
            with pytest.raises(HistoryError, match="for appending"):
                await store.append(lease, [user(2, "b")])
        finally:
            path.chmod(0o600)
            await store.release(lease)

        lines = [line async for line in store.read(SESSION)]
        assert len(lines) == 1


class TestRead:
    async def test_missing_journal_reads_as_empty(self, tmp_path: Path) -> None:
        store = JsonlHistoryStore(tmp_path)

        lines = [line async for line in store.read(SessionId(value="nobody"))]

        assert lines == []

    async def test_non_object_line_is_broken(self, tmp_path: Path) -> None:
        path = tmp_path / "abc-123.jsonl"
        path.write_text('[1, 2]\n{"type": "user"}\n\n', encoding="utf-8")
        store = JsonlHistoryStore(tmp_path)

        lines = [line async for line in store.read(SESSION)]

        assert isinstance(lines[0], BrokenLine)
        assert "expected a JSON object, got list" in lines[0].reason
        assert isinstance(lines[1], StoredLine)
        assert len(lines) == 2

    async def test_unreadable_journal_is_an_error(self, tmp_path: Path) -> None:
        path = tmp_path / "abc-123.jsonl"
        path.write_text("{}\n", encoding="utf-8")
        path.chmod(0o000)
        store = JsonlHistoryStore(tmp_path)
        try:
            if os.geteuid() == 0:
                pytest.skip("root reads any file")

            with pytest.raises(HistoryError, match="Could not read the journal"):
                _ = [line async for line in store.read(SESSION)]
        finally:
            path.chmod(0o600)
