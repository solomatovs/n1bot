"""Кастомный Chainlit DataLayer поверх FS (users.json + per-workspace threads)."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, ClassVar, cast

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter
from pydantic.alias_generators import to_camel

import chainlit as cl
from boba.web.chainlit.modesl import StoredUser, ThreadId, UserId
from boba.workspace.contract import (
    HistoryWorkspaceRegistry,
    HistoryWorkspaceShell,
    WorkspaceId,
)
from chainlit.data.base import BaseDataLayer
from chainlit.types import PageInfo, PaginatedResponse, Pagination, ThreadFilter
from chainlit.user import PersistedUser
from chainlit.user import User as ClUser

if TYPE_CHECKING:
    from chainlit.element import Element, ElementDict
    from chainlit.step import StepDict
    from chainlit.types import Feedback, ThreadDict

logger = logging.getLogger(__name__)

__all__ = [
    "BobaDataLayer",
    "FsThreadRepository",
    "FsUserCatalog",
    "ThreadDerivedWorkspaceOwnership",
    "ThreadIndexEntry",
    "ThreadMeta",
    "ThreadRepository",
    "UserCatalog",
    "WorkspaceOwnership",
    "WorkspaceOwnershipEntry",
]


_WIRE_CONFIG = ConfigDict(
    alias_generator=to_camel,
    populate_by_name=True,
    frozen=True,
)
"""Wire-формат — camelCase. Конструктор по python-name работает через
`populate_by_name=True`."""


class ThreadMeta(BaseModel):
    """Метаданные thread'а: всё, кроме самих steps. Wire — camelCase."""

    model_config = _WIRE_CONFIG

    id: ThreadId
    workspace_id: WorkspaceId
    user_id: UserId | None
    user_identifier: str | None
    name: str | None
    tags: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: str
    updated_at: str


class ThreadIndexEntry(BaseModel):
    """Снимок ThreadMeta для глобального индекса (`threads-index.json`)."""

    model_config = _WIRE_CONFIG

    workspace_id: WorkspaceId
    user_id: UserId | None
    user_identifier: str | None
    name: str | None
    tags: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: str
    updated_at: str

    @classmethod
    def from_meta(cls, meta: ThreadMeta) -> ThreadIndexEntry:
        return cls.model_validate(meta, from_attributes=True)

    def to_meta(self, thread_id: ThreadId) -> ThreadMeta:
        return ThreadMeta(id=thread_id, **self.model_dump())


_THREAD_INDEX_ADAPTER: TypeAdapter[dict[ThreadId, ThreadIndexEntry]] = TypeAdapter(
    dict[ThreadId, ThreadIndexEntry],
)
"""Адаптер `index.json`: `{ThreadId: ThreadIndexEntry}` через Pydantic."""

_USER_CATALOG_ADAPTER: TypeAdapter[dict[str, StoredUser]] = TypeAdapter(
    dict[str, StoredUser],
)
"""Адаптер `users.json`: `{identifier: StoredUser}` через Pydantic."""


class UserCatalog(ABC):
    """Источник users (auth-каталог)"""

    @abstractmethod
    async def get(self, identifier: ThreadId) -> StoredUser | None: ...

    @abstractmethod
    async def upsert(
        self, identifier: str, display_name: str | None, metadata: dict[str, Any]
    ) -> StoredUser: ...


class ThreadRepository(ABC):
    """
    Хранилище threads + их steps
    Workspace_id живёт в ThreadMeta
    """

    @abstractmethod
    async def get_meta(self, thread_id: ThreadId) -> ThreadMeta | None: ...

    @abstractmethod
    async def upsert_meta(self, meta: ThreadMeta) -> None: ...

    @abstractmethod
    async def delete(self, thread_id: ThreadId) -> None: ...

    @abstractmethod
    async def list_for_user(self, user_id: UserId) -> list[ThreadMeta]: ...

    @abstractmethod
    async def append_step(self, thread_id: ThreadId, step: dict[str, Any]) -> None: ...

    @abstractmethod
    async def update_step(self, thread_id: ThreadId, step: dict[str, Any]) -> None:
        "Обновить содержимое step"
        ...

    @abstractmethod
    async def delete_step(self, thread_id: ThreadId, step_id: str) -> None: ...

    @abstractmethod
    async def load_steps(self, thread_id: ThreadId) -> list[dict[str, Any]]:
        """Загрузить список шагов в чате"""
        ...


@dataclass(frozen=True)
class WorkspaceOwnershipEntry:
    """Workspace, принадлежащий пользователю, с метаданными для UI."""

    workspace_id: WorkspaceId
    last_used_at: str
    first_user_message: str | None


class WorkspaceOwnership(ABC):
    """Какие workspace'ы видны пользователю и в каком порядке."""

    @abstractmethod
    async def list_for_user(self, user_id: UserId) -> list[WorkspaceOwnershipEntry]: ...


class ThreadDerivedWorkspaceOwnership(WorkspaceOwnership):
    """Owner = user, чьи threads живут в workspace. Без отдельного индекса."""

    def __init__(self, threads: ThreadRepository) -> None:
        self._threads = threads

    async def list_for_user(self, user_id: UserId) -> list[WorkspaceOwnershipEntry]:
        threads = await self._threads.list_for_user(user_id)
        per_workspace: dict[WorkspaceId, list[ThreadMeta]] = {}
        for meta in threads:
            per_workspace.setdefault(meta.workspace_id, []).append(meta)
        result: list[WorkspaceOwnershipEntry] = []
        for workspace_id, metas in per_workspace.items():
            metas.sort(key=lambda m: m.updated_at, reverse=True)
            first_meta = min(metas, key=lambda m: m.created_at)
            preview = await self._first_user_message(first_meta.id)
            result.append(
                WorkspaceOwnershipEntry(
                    workspace_id=workspace_id,
                    last_used_at=metas[0].updated_at,
                    first_user_message=preview,
                )
            )
        result.sort(key=lambda e: e.last_used_at, reverse=True)
        return result

    async def _first_user_message(self, thread_id: ThreadId) -> str | None:
        "Первое пользовательское сообщение через thread_id"
        steps = await self._threads.load_steps(thread_id)
        for step in steps:
            if step.get("type") == "user_message":
                text = (step.get("output") or step.get("input") or "").strip()
                return text or None
        return None


class FsUserCatalog(UserCatalog):
    """JSON-файл `{identifier -> StoredUser}` внутри системного `HistoryWorkspaceShell`.

    Файл живёт по относительному пути внутри workspace'а (`users.json` по
    умолчанию). Запись — через `shell.atomic_write_text`, чтение — через
    `shell.read_text` / `shell.exists`. Никакого прямого FS-API.
    """

    _DEFAULT_FILENAME: ClassVar[str] = "users.json"

    def __init__(
        self,
        shell: HistoryWorkspaceShell,
        filename: str = _DEFAULT_FILENAME,
    ) -> None:
        self._shell = shell
        self._filename = filename
        self._lock = asyncio.Lock()

    async def get(self, identifier: str) -> StoredUser | None:
        async with self._lock:
            data = await asyncio.to_thread(self._load)
            return data.get(identifier)

    async def upsert(
        self, identifier: str, display_name: str | None, metadata: dict[str, Any]
    ) -> StoredUser:
        async with self._lock:
            data = await asyncio.to_thread(self._load)
            user = data.get(identifier) or StoredUser(
                id=UserId(str(uuid.uuid4())),
                identifier=identifier,
                display_name=display_name,
                metadata=dict(metadata),
                created_at=datetime.now(UTC).isoformat(),
            )
            data[identifier] = user
            await asyncio.to_thread(self._save, data)
            return user

    def _load(self) -> dict[str, StoredUser]:
        if not self._shell.exists(self._filename):
            return {}
        with self._shell.read_text(self._filename) as fh:
            text = fh.read() or "{}"
        return _USER_CATALOG_ADAPTER.validate_json(text)

    def _save(self, data: dict[str, StoredUser]) -> None:
        payload = _USER_CATALOG_ADAPTER.dump_json(
            data,
            by_alias=True,
            indent=2,
        ).decode("utf-8")
        self._shell.atomic_write_text(self._filename, payload)


@dataclass
class _StepLockSlot:
    """Lock + refcount: lock удаляется из словаря, когда refs обнулится."""

    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    refs: int = 0


class FsThreadRepository(ThreadRepository):
    """Threads хранятся внутри своего `HistoryWorkspaceShell`.

    Layout per workspace (через `history_workspaces.get_or_create(ws_id)`):
        threads/{thread_id}/meta.json
        threads/{thread_id}/steps.jsonl

    Глобальный index — в `system_shell` (отдельный системный workspace):
        {index_filename}  (по умолчанию `threads-index.json`)

    Index хранит снимок `ThreadMeta`:
    `{thread_id: {workspaceId, userId, userIdentifier, name, tags, metadata,
    createdAt, updatedAt}}`. Это даёт `list_for_user` за O(N_threads) без
    обхода всех workspace-директорий.

    Все FS-операции идут через `WorkspaceShell` API; прямого доступа к
    `pathlib.Path` за пределами shell'а нет.
    """

    _META: ClassVar[str] = "meta.json"
    _STEPS: ClassVar[str] = "steps.jsonl"
    _THREADS_DIR: ClassVar[str] = "threads"
    _DEFAULT_INDEX_FILENAME: ClassVar[str] = "threads-index.json"

    def __init__(
        self,
        history_workspaces: HistoryWorkspaceRegistry,
        system_shell: HistoryWorkspaceShell,
        index_filename: str = _DEFAULT_INDEX_FILENAME,
    ) -> None:
        self._history_workspaces = history_workspaces
        self._system_shell = system_shell
        self._index_filename = index_filename
        self._index_lock = asyncio.Lock()
        self._step_locks: dict[ThreadId, _StepLockSlot] = {}
        self._step_locks_lock = asyncio.Lock()

    async def get_meta(self, thread_id: ThreadId) -> ThreadMeta | None:
        entry = await self._lookup_entry(thread_id)
        if entry is None:
            return None
        return await asyncio.to_thread(self._read_meta, entry.workspace_id, thread_id)

    async def upsert_meta(self, meta: ThreadMeta) -> None:
        await asyncio.to_thread(self._write_meta, meta)
        await self._index_upsert(meta)

    async def delete(self, thread_id: ThreadId) -> None:
        entry = await self._lookup_entry(thread_id)
        if entry is None:
            return
        await asyncio.to_thread(self._delete_dir, entry.workspace_id, thread_id)
        await self._index_remove(thread_id)
        # _step_locks вычистится сам по refs==0; на всякий — попытаемся снять
        # незанятый слот.
        async with self._step_locks_lock:
            slot = self._step_locks.get(thread_id)
            if slot is not None and slot.refs == 0:
                self._step_locks.pop(thread_id, None)

    async def list_for_user(self, user_id: UserId) -> list[ThreadMeta]:
        index = await self._load_index_locked()
        metas = [
            entry.to_meta(thread_id)
            for thread_id, entry in index.items()
            if entry.user_id == user_id
        ]
        metas.sort(key=lambda m: m.updated_at, reverse=True)
        return metas

    async def append_step(self, thread_id: ThreadId, step: dict[str, Any]) -> None:
        "Добавить новый step в историю steps.jsonl"
        workspace_id = await self._lookup_workspace(thread_id)
        if workspace_id is None:
            logger.warning("append_step: thread %s not in index, skipping", thread_id)
            return

        async with self._step_lock(thread_id):
            await asyncio.to_thread(self._append_step, workspace_id, thread_id, step)

    async def update_step(self, thread_id: ThreadId, step: dict[str, Any]) -> None:
        "Обновить содержимое step"
        workspace_id = await self._lookup_workspace(thread_id)
        if workspace_id is None:
            return

        async with self._step_lock(thread_id):
            await asyncio.to_thread(self._rewrite_step, workspace_id, thread_id, step)

    async def delete_step(self, thread_id: ThreadId, step_id: str) -> None:
        workspace_id = await self._lookup_workspace(thread_id)
        if workspace_id is None:
            return

        async with self._step_lock(thread_id):
            await asyncio.to_thread(
                self._remove_step,
                workspace_id,
                thread_id,
                step_id,
            )

    async def load_steps(self, thread_id: ThreadId) -> list[dict[str, Any]]:
        "Загрузить шаги для указанного thread_id"
        workspace_id = await self._lookup_workspace(thread_id)
        if workspace_id is None:
            return []

        return await asyncio.to_thread(self._read_steps, workspace_id, thread_id)

    @asynccontextmanager
    async def _step_lock(self, thread_id: ThreadId) -> AsyncIterator[None]:
        """Refcount-обёртка: слот живёт ровно пока есть активные владельцы."""
        async with self._step_locks_lock:
            slot = self._step_locks.get(thread_id)
            if slot is None:
                slot = _StepLockSlot()
                self._step_locks[thread_id] = slot
            slot.refs += 1
        try:
            async with slot.lock:
                yield
        finally:
            async with self._step_locks_lock:
                slot.refs -= 1
                if slot.refs == 0:
                    self._step_locks.pop(thread_id, None)

    async def _lookup_entry(self, thread_id: ThreadId) -> ThreadIndexEntry | None:
        index = await self._load_index_locked()
        return index.get(thread_id)

    async def _lookup_workspace(self, thread_id: ThreadId) -> WorkspaceId | None:
        "Найти workspace_id по thread_id"
        entry = await self._lookup_entry(thread_id)
        return entry.workspace_id if entry is not None else None

    async def _index_upsert(self, meta: ThreadMeta) -> None:
        async with self._index_lock:
            index = await asyncio.to_thread(self._load_index)
            index[meta.id] = ThreadIndexEntry.from_meta(meta)
            await asyncio.to_thread(self._save_index, index)

    async def _index_remove(self, thread_id: ThreadId) -> None:
        async with self._index_lock:
            index = await asyncio.to_thread(self._load_index)
            if index.pop(thread_id, None) is not None:
                await asyncio.to_thread(self._save_index, index)

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
        self._system_shell.atomic_write_text(self._index_filename, payload)

    def _shell_for(self, workspace_id: WorkspaceId) -> HistoryWorkspaceShell:
        """`HistoryWorkspaceShell` для конкретного workspace_id (lazy create)."""
        shell = self._history_workspaces.get_or_create(workspace_id)
        # Все шеллы history-namespace по контракту реализуют HistoryWorkspaceShell.
        return cast("HistoryWorkspaceShell", shell)

    def _meta_path(self, thread_id: ThreadId) -> str:
        return f"{self._THREADS_DIR}/{thread_id}/{self._META}"

    def _steps_path(self, thread_id: ThreadId) -> str:
        return f"{self._THREADS_DIR}/{thread_id}/{self._STEPS}"

    def _thread_dir_path(self, thread_id: ThreadId) -> str:
        return f"{self._THREADS_DIR}/{thread_id}"

    def _read_meta(
        self,
        workspace_id: WorkspaceId,
        thread_id: ThreadId,
    ) -> ThreadMeta | None:
        shell = self._shell_for(workspace_id)
        path = self._meta_path(thread_id)
        if not shell.exists(path):
            return None
        with shell.read_text(path) as fh:
            return ThreadMeta.model_validate_json(fh.read())

    def _write_meta(self, meta: ThreadMeta) -> None:
        shell = self._shell_for(meta.workspace_id)
        shell.atomic_write_text(
            self._meta_path(meta.id),
            meta.model_dump_json(by_alias=True, indent=2),
        )

    def _delete_dir(self, workspace_id: WorkspaceId, thread_id: ThreadId) -> None:
        shell = self._shell_for(workspace_id)
        thread_dir = self._thread_dir_path(thread_id)
        if shell.exists(thread_dir):
            shell.delete(thread_dir, recursive=True)

    def _read_steps(
        self,
        workspace_id: WorkspaceId,
        thread_id: ThreadId,
    ) -> list[dict[str, Any]]:
        shell = self._shell_for(workspace_id)
        path = self._steps_path(thread_id)
        if not shell.exists(path):
            return []
        steps: list[dict[str, Any]] = []
        for lineno, raw in enumerate(shell.read_lines(path), start=1):
            line = raw.strip()
            if not line:
                continue
            try:
                steps.append(json.loads(line))
            except json.JSONDecodeError:
                # Кривая строка возможна после crash в середине _append_step;
                # пропускаем, чтобы не терять весь thread.
                logger.warning(
                    "skip corrupted step at %s/%s:%d",
                    workspace_id,
                    path,
                    lineno,
                )
        return steps

    def _append_step(
        self,
        workspace_id: WorkspaceId,
        thread_id: ThreadId,
        step: dict[str, Any],
    ) -> None:
        shell = self._shell_for(workspace_id)
        path = self._steps_path(thread_id)
        payload = json.dumps(step, ensure_ascii=False) + "\n"
        with shell.append_text(path) as fh:
            fh.write(payload)

    def _rewrite_step(
        self,
        workspace_id: WorkspaceId,
        thread_id: ThreadId,
        step: dict[str, Any],
    ) -> None:
        "Обновить один step (целиком перезаписав steps.jsonl)."
        steps = self._read_steps(workspace_id, thread_id)
        step_id = step.get("id")
        replaced = False
        for i, existing in enumerate(steps):
            if existing.get("id") == step_id:
                steps[i] = step
                replaced = True
                break
        if not replaced:
            steps.append(step)
        self._write_steps(workspace_id, thread_id, steps)

    def _remove_step(
        self,
        workspace_id: WorkspaceId,
        thread_id: ThreadId,
        step_id: str,
    ) -> None:
        steps = [
            s
            for s in self._read_steps(workspace_id, thread_id)
            if s.get("id") != step_id
        ]
        self._write_steps(workspace_id, thread_id, steps)

    def _write_steps(
        self,
        workspace_id: WorkspaceId,
        thread_id: ThreadId,
        steps: list[dict[str, Any]],
    ) -> None:
        shell = self._shell_for(workspace_id)
        body = "".join(json.dumps(step, ensure_ascii=False) + "\n" for step in steps)
        shell.atomic_write_text(self._steps_path(thread_id), body)


class BobaDataLayer(BaseDataLayer):
    """Chainlit-adapter поверх UserCatalog + ThreadRepository.

    Workspace_id берётся из metadata, переданного Chainlit'ом; если его нет,
    читаем `cl.user_session.get("workspace_id")` (его пишет on_chat_start).
    """

    _WORKSPACE_META_KEY: ClassVar[str] = "workspace_id"

    def __init__(
        self,
        users: UserCatalog,
        threads: ThreadRepository,
    ) -> None:
        self._users = users
        self._threads = threads

    async def get_user(self, identifier: ThreadId) -> PersistedUser | None:
        record = await self._users.get(identifier)
        if record is None:
            return None
        return self._to_persisted(record)

    async def create_user(self, user: ClUser) -> PersistedUser | None:
        record = await self._users.upsert(
            identifier=user.identifier,
            display_name=user.display_name,
            metadata=dict(user.metadata or {}),
        )
        return self._to_persisted(record)

    async def get_thread(self, thread_id: ThreadId) -> ThreadDict | None:
        meta = await self._threads.get_meta(thread_id)
        if meta is None:
            return None
        steps = await self._threads.load_steps(thread_id)
        return cast(
            "ThreadDict",
            {
                "id": meta.id,
                "createdAt": meta.created_at,
                "name": meta.name,
                "userId": meta.user_id if meta.user_id is not None else None,
                "userIdentifier": meta.user_identifier,
                "tags": meta.tags,
                "metadata": meta.metadata,
                "steps": steps,
                "elements": [],
            },
        )

    async def get_thread_author(self, thread_id: ThreadId) -> str:
        meta = await self._threads.get_meta(thread_id)
        return meta.user_identifier or "" if meta else ""

    async def update_thread(
        self,
        thread_id: ThreadId,
        name: str | None = None,
        user_id: UserId | None = None,
        metadata: dict[str, Any] | None = None,
        tags: list[str] | None = None,
    ) -> None:
        existing = await self._threads.get_meta(thread_id)
        # Приоритет: явный metadata > existing-тред > session. Без этого
        # повторный update_thread без metadata мог бы потерять тред при
        # незаполненной user_session.
        workspace_id = self._resolve_workspace(metadata)
        if workspace_id is None and existing is not None:
            workspace_id = existing.workspace_id
        if workspace_id is None:
            logger.warning(
                "update_thread(%s) без workspace_id — пропускаем",
                thread_id,
            )
            return

        now = datetime.now(UTC).isoformat()
        user_identifier = self._current_user_identifier()
        merged_metadata = dict(existing.metadata) if existing else {}
        if metadata:
            merged_metadata.update(metadata)
        merged_metadata[self._WORKSPACE_META_KEY] = workspace_id

        meta = ThreadMeta(
            id=thread_id,
            workspace_id=workspace_id,
            user_id=user_id
            if user_id is not None
            else (existing.user_id if existing else None),
            user_identifier=(
                user_identifier or (existing.user_identifier if existing else None)
            ),
            name=name if name is not None else (existing.name if existing else None),
            tags=list(tags)
            if tags is not None
            else (existing.tags if existing else []),
            metadata=merged_metadata,
            created_at=existing.created_at if existing else now,
            updated_at=now,
        )
        await self._threads.upsert_meta(meta)

    async def delete_thread(self, thread_id: ThreadId) -> None:
        await self._threads.delete(thread_id)

    async def list_threads(
        self,
        pagination: Pagination,
        filters: ThreadFilter,
    ) -> PaginatedResponse:
        target_user_id = filters.userId
        if target_user_id is None:
            return PaginatedResponse(
                pageInfo=PageInfo(hasNextPage=False, startCursor=None, endCursor=None),
                data=[],
            )
        metas = await self._threads.list_for_user(
            UserId(target_user_id),
        )

        start = 0
        if pagination.cursor:
            for i, m in enumerate(metas):
                if m.id == pagination.cursor:
                    start = i + 1
                    break

        chunk = metas[start : start + pagination.first]
        has_next = (start + pagination.first) < len(metas)
        data = [
            {
                "id": m.id,
                "createdAt": m.created_at,
                "name": m.name,
                "userId": m.user_id if m.user_id is not None else None,
                "userIdentifier": m.user_identifier,
                "tags": m.tags,
                "metadata": m.metadata,
                "steps": [],
                "elements": [],
            }
            for m in chunk
        ]
        return PaginatedResponse(
            pageInfo=PageInfo(
                hasNextPage=has_next,
                startCursor=chunk[0].id if chunk else None,
                endCursor=chunk[-1].id if chunk else None,
            ),
            data=data,
        )

    async def create_step(self, step_dict: StepDict) -> None:
        match step_dict.get("threadId"):
            case None:
                return
            case thread_id:
                await self._threads.append_step(
                    ThreadId(thread_id),
                    dict(step_dict),
                )

    async def update_step(self, step_dict: StepDict) -> None:
        "Обновить содержимое step"
        match step_dict.get("threadId"):
            case None:
                return
            case thread_id:
                await self._threads.update_step(
                    ThreadId(thread_id),
                    dict(step_dict),
                )

    async def delete_step(self, step_id: str) -> None:
        # Без thread_id найти step невозможно без сканирования.
        # Chainlit обычно передаёт thread_id через step_dict, не сюда.
        logger.warning("delete_step(%s) ignored — нужен thread_id", step_id)

    async def create_element(self, element: Element) -> None:
        # Elements (images/files) пока не персистим.
        return

    async def get_element(
        self,
        thread_id: ThreadId,
        element_id: str,
    ) -> ElementDict | None:
        return None

    async def delete_element(
        self, element_id: str, thread_id: ThreadId | None = None
    ) -> None:
        return

    async def upsert_feedback(self, feedback: Feedback) -> str:
        return ""

    async def delete_feedback(self, feedback_id: str) -> bool:
        return False

    async def get_favorite_steps(self, user_id: UserId) -> list[StepDict]:
        return []

    async def build_debug_url(self) -> str:
        return ""

    async def close(self) -> None:
        return

    def _resolve_workspace(
        self,
        metadata: dict[str, Any] | None,
    ) -> WorkspaceId | None:
        if metadata and self._WORKSPACE_META_KEY in metadata:
            raw = metadata[self._WORKSPACE_META_KEY]
            if isinstance(raw, str):
                try:
                    return WorkspaceId(raw)
                except ValueError:
                    pass
        # cl.user_session завязан на request-контекст: вне него (например, при
        # фоновых вызовах data_layer) обращение кидает LookupError/RuntimeError.
        try:
            ws = cl.user_session.get(self._WORKSPACE_META_KEY)
        except (LookupError, RuntimeError):
            return None
        if isinstance(ws, str):
            return WorkspaceId(ws)
        return None

    @staticmethod
    def _current_user_identifier() -> str | None:
        try:
            session_user = cl.context.session.user
        except (LookupError, RuntimeError, AttributeError):
            return None
        return session_user.identifier if session_user else None

    @staticmethod
    def _to_persisted(record: StoredUser) -> PersistedUser:
        return PersistedUser(
            id=record.id,
            identifier=record.identifier,
            display_name=record.display_name,
            metadata=dict(record.metadata),
            createdAt=record.created_at,
        )
