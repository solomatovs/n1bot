"""Порт ThreadRepository + FS-реализация поверх threads-index.json.

Контракт намеренно узкий: общий create(...) принимает поля, нужные для
нового треда; изменения существующей меты идут через именованные операции
(rename, set_system_prompt, set_user, set_tags, merge_metadata).

Это устраняет класс багов, когда вызывающий собирает полный ThreadMeta и
случайно теряет поле, о котором не знал (например, system_prompt,
добавленный позже). Поле updated_at штампуется самим репозиторием.
"""

from __future__ import annotations

import asyncio
import io
import threading
from abc import ABC, abstractmethod
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, ClassVar

from pydantic import TypeAdapter

from boba.chainlit.models import (
    ThreadId,
    ThreadIndexEntry,
    ThreadMeta,
    UserId,
)
from boba.workspace.contract import WorkspaceId, WorkspaceShell

__all__ = [
    "FsThreadRepository",
    "ThreadAlreadyExistsError",
    "ThreadNotFoundError",
    "ThreadRepository",
]


_THREAD_INDEX_ADAPTER: TypeAdapter[dict[ThreadId, ThreadIndexEntry]] = TypeAdapter(
    dict[ThreadId, ThreadIndexEntry],
)


class ThreadNotFoundError(Exception):
    """Узкая операция вызвана для несуществующего thread_id."""


class ThreadAlreadyExistsError(Exception):
    """create вызван для уже существующего thread_id."""


class ThreadRepository(ABC):
    """Хранилище мет тредов."""

    @abstractmethod
    async def get_meta(self, thread_id: ThreadId) -> ThreadMeta | None: ...

    @abstractmethod
    def get_meta_sync(self, thread_id: ThreadId) -> ThreadMeta | None: ...

    @abstractmethod
    async def list_for_user(self, user_id: UserId) -> list[ThreadMeta]: ...

    @abstractmethod
    async def create(
        self,
        thread_id: ThreadId,
        workspace_id: WorkspaceId,
        user_id: UserId | None,
        user_identifier: str | None,
        *,
        name: str | None = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        system_prompt: str | None = None,
        enabled_tool_ids: list[str] | None = None,
    ) -> None: ...

    @abstractmethod
    async def rename(self, thread_id: ThreadId, name: str | None) -> None: ...

    @abstractmethod
    async def set_system_prompt(
        self,
        thread_id: ThreadId,
        system_prompt: str | None,
    ) -> None: ...

    @abstractmethod
    async def set_user(
        self,
        thread_id: ThreadId,
        user_id: UserId | None,
        user_identifier: str | None,
    ) -> None: ...

    @abstractmethod
    async def set_tags(self, thread_id: ThreadId, tags: list[str]) -> None: ...

    @abstractmethod
    async def set_enabled_tools(
        self,
        thread_id: ThreadId,
        tool_ids: list[str] | None,
    ) -> None: ...

    @abstractmethod
    async def merge_metadata(
        self,
        thread_id: ThreadId,
        patch: dict[str, Any],
    ) -> None: ...

    @abstractmethod
    async def delete(self, thread_id: ThreadId) -> None: ...


_Index = dict[ThreadId, ThreadIndexEntry]
_Mutator = Callable[[_Index], None]


class FsThreadRepository(ThreadRepository):
    """Меты тредов в system_shell/threads-index.json."""

    _DEFAULT_INDEX_FILENAME: ClassVar[str] = "threads-index.json"

    def __init__(
        self,
        system_shell: WorkspaceShell,
        index_filename: str = _DEFAULT_INDEX_FILENAME,
    ) -> None:
        self._system_shell = system_shell
        self._index_filename = index_filename
        self._index_lock = asyncio.Lock()
        # sync-lock используется только синхронным read-путём (PromptProvider
        # вызывается из worker-thread'а без event loop'а). Не пересекается
        # с _index_lock — sync-read это чистый snapshot, не транзакция.
        self._sync_lock = threading.Lock()

    @staticmethod
    def _now() -> str:
        return datetime.now(UTC).isoformat()

    async def get_meta(self, thread_id: ThreadId) -> ThreadMeta | None:
        index = await self._load_index_locked()
        entry = index.get(thread_id)
        if entry is None:
            return None
        return entry.to_meta(thread_id)

    def get_meta_sync(self, thread_id: ThreadId) -> ThreadMeta | None:
        with self._sync_lock:
            index = self._load_index()
        entry = index.get(thread_id)
        if entry is None:
            return None
        return entry.to_meta(thread_id)

    async def list_for_user(self, user_id: UserId) -> list[ThreadMeta]:
        index = await self._load_index_locked()
        metas = [
            entry.to_meta(thread_id)
            for thread_id, entry in index.items()
            if entry.user_id == user_id
        ]
        metas.sort(key=lambda m: m.updated_at, reverse=True)
        return metas

    async def create(
        self,
        thread_id: ThreadId,
        workspace_id: WorkspaceId,
        user_id: UserId | None,
        user_identifier: str | None,
        *,
        name: str | None = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        system_prompt: str | None = None,
        enabled_tool_ids: list[str] | None = None,
    ) -> None:
        now = self._now()
        meta = ThreadMeta(
            id=thread_id,
            workspace_id=workspace_id,
            user_id=user_id,
            user_identifier=user_identifier,
            name=name,
            tags=list(tags) if tags is not None else [],
            metadata=dict(metadata) if metadata is not None else {},
            system_prompt=system_prompt,
            enabled_tool_ids=(
                list(enabled_tool_ids) if enabled_tool_ids is not None else None
            ),
            created_at=now,
            updated_at=now,
        )

        def mutate(index: _Index) -> None:
            if thread_id in index:
                raise ThreadAlreadyExistsError(thread_id)
            index[thread_id] = ThreadIndexEntry.from_meta(meta)

        await self._mutate_index(mutate)

    async def rename(self, thread_id: ThreadId, name: str | None) -> None:
        await self._patch_entry(
            thread_id,
            lambda e: e.model_copy(update={"name": name}),
        )

    async def set_system_prompt(
        self,
        thread_id: ThreadId,
        system_prompt: str | None,
    ) -> None:
        await self._patch_entry(
            thread_id,
            lambda e: e.model_copy(update={"system_prompt": system_prompt}),
        )

    async def set_user(
        self,
        thread_id: ThreadId,
        user_id: UserId | None,
        user_identifier: str | None,
    ) -> None:
        await self._patch_entry(
            thread_id,
            lambda e: e.model_copy(
                update={"user_id": user_id, "user_identifier": user_identifier},
            ),
        )

    async def set_tags(self, thread_id: ThreadId, tags: list[str]) -> None:
        await self._patch_entry(
            thread_id,
            lambda e: e.model_copy(update={"tags": list(tags)}),
        )

    async def set_enabled_tools(
        self,
        thread_id: ThreadId,
        tool_ids: list[str] | None,
    ) -> None:
        normalized = list(tool_ids) if tool_ids is not None else None
        await self._patch_entry(
            thread_id,
            lambda e: e.model_copy(update={"enabled_tool_ids": normalized}),
        )

    async def merge_metadata(
        self,
        thread_id: ThreadId,
        patch: dict[str, Any],
    ) -> None:
        def update(entry: ThreadIndexEntry) -> ThreadIndexEntry:
            merged = dict(entry.metadata)
            merged.update(patch)
            return entry.model_copy(update={"metadata": merged})

        await self._patch_entry(thread_id, update)

    async def delete(self, thread_id: ThreadId) -> None:
        def mutate(index: _Index) -> None:
            index.pop(thread_id, None)

        await self._mutate_index(mutate)

    async def _patch_entry(
        self,
        thread_id: ThreadId,
        update: Callable[[ThreadIndexEntry], ThreadIndexEntry],
    ) -> None:
        now = self._now()

        def mutate(index: _Index) -> None:
            entry = index.get(thread_id)
            if entry is None:
                raise ThreadNotFoundError(thread_id)
            patched = update(entry)
            index[thread_id] = patched.model_copy(update={"updated_at": now})

        await self._mutate_index(mutate)

    async def _mutate_index(self, mutator: _Mutator) -> None:
        async with self._index_lock:
            index = await asyncio.to_thread(self._load_index)
            mutator(index)
            await asyncio.to_thread(self._save_index, index)

    async def _load_index_locked(self) -> _Index:
        async with self._index_lock:
            return await asyncio.to_thread(self._load_index)

    def _load_index(self) -> _Index:
        if not self._system_shell.exists(self._index_filename):
            return {}
        with self._system_shell.read_text(self._index_filename) as fh:
            text = fh.read() or "{}"
        return _THREAD_INDEX_ADAPTER.validate_json(text)

    def _save_index(self, index: _Index) -> None:
        payload = _THREAD_INDEX_ADAPTER.dump_json(
            index,
            by_alias=True,
            indent=2,
        ).decode("utf-8")
        self._system_shell.atomic_write_text(self._index_filename, io.StringIO(payload))
