"""Хранилище истории без хранилища: захват без замка, дозапись в никуда,
пустое чтение, пустой список, удаление без ошибки."""

from __future__ import annotations

import pytest

from boba.agent.history import MemoryHistoryStore, SessionId
from boba.agent.records import UserMessage, UserRecord

pytestmark = pytest.mark.anyio

SESSION = SessionId(value="memory-1")


def user(seq: int, text: str) -> UserRecord:
    return UserRecord(
        uuid=f"u-{seq}",
        parent_uuid=None,
        timestamp="2026-10-11T10:00:00.000Z",
        session_id=SESSION.value,
        version="0.0.26.dev4",
        seq=seq,
        message=UserMessage(content=text),
    )


class TestMemoryHistoryStore:
    async def test_two_holders_of_one_session_do_not_collide(self) -> None:
        store = MemoryHistoryStore()
        other = MemoryHistoryStore()

        first = await store.acquire(SESSION)
        second = await other.acquire(SESSION)
        third = await store.acquire(SESSION)

        assert first.session == SESSION
        assert len({first.key, second.key, third.key}) == 3
        await store.release(first)
        await other.release(second)
        await store.release(third)

    async def test_appended_records_are_not_readable_back(self) -> None:
        store = MemoryHistoryStore()
        lease = await store.acquire(SESSION)
        await store.append(lease, [user(1, "a"), user(2, "b")])
        await store.release(lease)

        lines = [line async for line in store.read(SESSION)]
        again = [line async for line in MemoryHistoryStore().read(SESSION)]

        assert lines == []
        assert again == []

    async def test_list_is_empty_and_delete_is_silent(self) -> None:
        store = MemoryHistoryStore()
        lease = await store.acquire(SESSION)
        await store.append(lease, [user(1, "a")])

        page = await store.list(None, 50)
        await store.delete(SESSION)
        await store.release(lease)

        assert page.sessions == ()
        assert page.next_cursor is None
