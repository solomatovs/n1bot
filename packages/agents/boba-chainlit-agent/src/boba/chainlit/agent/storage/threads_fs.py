"""Порт `ThreadRepository` + FS-реализация поверх `threads-index.json`."""

from __future__ import annotations

import asyncio
import io
from abc import ABC, abstractmethod
from typing import ClassVar

from pydantic import TypeAdapter

from boba.chainlit.agent.models import (
    ThreadId,
    ThreadIndexEntry,
    ThreadMeta,
    UserId,
)
from boba.workspace.contract import HistoryWorkspaceShell

__all__ = ["FsThreadRepository", "ThreadRepository"]


_THREAD_INDEX_ADAPTER: TypeAdapter[dict[ThreadId, ThreadIndexEntry]] = TypeAdapter(
    dict[ThreadId, ThreadIndexEntry],
)


class ThreadRepository(ABC):
    """Хранилище мет тредов"""

    @abstractmethod
    async def get_meta(self, thread_id: ThreadId) -> ThreadMeta | None: ...

    @abstractmethod
    async def upsert_meta(self, meta: ThreadMeta) -> None: ...

    @abstractmethod
    async def delete(self, thread_id: ThreadId) -> None: ...

    @abstractmethod
    async def list_for_user(self, user_id: UserId) -> list[ThreadMeta]: ...


class FsThreadRepository(ThreadRepository):
    """Меты тредов в `system_shell/threads-index.json`"""

    _DEFAULT_INDEX_FILENAME: ClassVar[str] = "threads-index.json"

    def __init__(
        self,
        system_shell: HistoryWorkspaceShell,
        index_filename: str = _DEFAULT_INDEX_FILENAME,
    ) -> None:
        self._system_shell = system_shell
        self._index_filename = index_filename
        self._index_lock = asyncio.Lock()

    async def get_meta(self, thread_id: ThreadId) -> ThreadMeta | None:
        entry = await self._lookup_entry(thread_id)
        if entry is None:
            return None
        return entry.to_meta(thread_id)

    async def upsert_meta(self, meta: ThreadMeta) -> None:
        async with self._index_lock:
            index = await asyncio.to_thread(self._load_index)
            index[meta.id] = ThreadIndexEntry.from_meta(meta)
            await asyncio.to_thread(self._save_index, index)

    async def delete(self, thread_id: ThreadId) -> None:
        async with self._index_lock:
            index = await asyncio.to_thread(self._load_index)
            if index.pop(thread_id, None) is not None:
                await asyncio.to_thread(self._save_index, index)

    async def list_for_user(self, user_id: UserId) -> list[ThreadMeta]:
        index = await self._load_index_locked()
        metas = [
            entry.to_meta(thread_id)
            for thread_id, entry in index.items()
            if entry.user_id == user_id
        ]
        metas.sort(key=lambda m: m.updated_at, reverse=True)
        return metas

    async def _lookup_entry(self, thread_id: ThreadId) -> ThreadIndexEntry | None:
        index = await self._load_index_locked()
        return index.get(thread_id)

    async def _load_index_locked(self) -> dict[ThreadId, ThreadIndexEntry]:
        async with self._index_lock:
            return await asyncio.to_thread(self._load_index)

    def _load_index(self) -> dict[ThreadId, ThreadIndexEntry]:
        if not self._system_shell.exists(self._index_filename):
            return {}
        with self._system_shell.read_text(self._index_filename) as fh:
            text = fh.read() or "{}"
        return _THREAD_INDEX_ADAPTER.validate_json(text)

    def _save_index(self, index: dict[ThreadId, ThreadIndexEntry]) -> None:
        payload = _THREAD_INDEX_ADAPTER.dump_json(
            index,
            by_alias=True,
            indent=2,
        ).decode("utf-8")
        self._system_shell.atomic_write_text(self._index_filename, io.StringIO(payload))
