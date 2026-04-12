"""Файловый DataLayer для Chainlit — персистенция threads в JSON-файлах.

Каждый thread хранится как {thread_id}.json в {folder}/.boba/chats/.
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, cast

from chainlit.data.base import BaseDataLayer
from chainlit.types import (
    Feedback,
    PageInfo,
    PaginatedResponse,
    Pagination,
    ThreadDict,
    ThreadFilter,
)
from chainlit.user import PersistedUser, User

log = logging.getLogger(__name__)


class FileDataLayer(BaseDataLayer):
    """Файловое хранилище threads для Chainlit.

    Структура:
        {base_dir}/{folder}/.boba/chats/{thread_id}.json
    """

    def __init__(
        self, base_dir: Path, boba_dir_name: str = ".boba", chats_dir_name: str = "chats"
    ) -> None:
        self._base_dir = base_dir
        self._boba = boba_dir_name
        self._chats = chats_dir_name

    # ------------------------------------------------------------------
    # User (заглушки — нет auth)
    # ------------------------------------------------------------------

    async def get_user(self, identifier: str) -> Optional[PersistedUser]:
        return PersistedUser(
            id="default",
            identifier=identifier,
            createdAt=datetime.now(timezone.utc).isoformat(),
        )

    async def create_user(self, user: User) -> Optional[PersistedUser]:
        return PersistedUser(
            id="default",
            identifier=user.identifier,
            createdAt=datetime.now(timezone.utc).isoformat(),
        )

    # ------------------------------------------------------------------
    # Threads
    # ------------------------------------------------------------------

    async def get_thread(self, thread_id: str) -> Optional[ThreadDict]:
        for path in self._find_thread_files(thread_id):
            return self._read_thread(path)
        return None

    async def get_thread_author(self, thread_id: str) -> str:
        thread = await self.get_thread(thread_id)
        if thread:
            return thread.get("userIdentifier", "default") or "default"
        return "default"

    async def list_threads(
        self, pagination: Pagination, filters: ThreadFilter
    ) -> PaginatedResponse[ThreadDict]:
        all_threads: list[ThreadDict] = []

        for folder_dir in self._iter_folders():
            chats_dir = folder_dir / self._boba / self._chats
            if not chats_dir.is_dir():
                continue
            for path in chats_dir.glob("*.json"):
                thread = self._read_thread(path)
                if thread:
                    all_threads.append(thread)

        # Фильтр по search
        if filters.search:
            q = filters.search.lower()
            all_threads = [
                t for t in all_threads if q in (t.get("name") or "").lower()
            ]

        # Сортировка по дате (новые первые)
        all_threads.sort(key=lambda t: t.get("createdAt", ""), reverse=True)

        # Pagination (cursor = индекс)
        start = 0
        if pagination.cursor:
            try:
                start = int(pagination.cursor)
            except ValueError:
                start = 0

        page = all_threads[start : start + pagination.first]
        has_next = start + pagination.first < len(all_threads)

        return PaginatedResponse(
            pageInfo=PageInfo(
                hasNextPage=has_next,
                startCursor=str(start),
                endCursor=str(start + len(page)),
            ),
            data=page,
        )

    async def update_thread(
        self,
        thread_id: str,
        name: Optional[str] = None,
        user_id: Optional[str] = None,
        metadata: Optional[Dict] = None,
        tags: Optional[List[str]] = None,
    ) -> None:
        for path in self._find_thread_files(thread_id):
            data = self._read_thread(path)
            if not data:
                return
            thread: Any = data
            if name is not None:
                thread["name"] = name
            if user_id is not None:
                thread["userId"] = user_id
            if metadata is not None:
                existing = thread.get("metadata") or {}
                if isinstance(existing, str):
                    try:
                        existing = json.loads(existing)
                    except json.JSONDecodeError:
                        existing = {}
                existing.update(metadata)
                thread["metadata"] = existing
            if tags is not None:
                thread["tags"] = tags
            self._write_thread(path, thread)
            return

        # Thread не существует — создаём (Chainlit вызывает update_thread до create_step)
        if metadata and "folder" in metadata:
            folder = metadata["folder"]
            chats_dir = self._base_dir / folder / self._boba / self._chats
            chats_dir.mkdir(parents=True, exist_ok=True)
            path = chats_dir / f"{thread_id}.json"
            new_thread: Any = {
                "id": thread_id,
                "createdAt": datetime.now(timezone.utc).isoformat(),
                "name": name,
                "userId": user_id,
                "userIdentifier": "default",
                "metadata": metadata,
                "tags": tags,
                "steps": [],
            }
            self._write_thread(path, cast(ThreadDict, new_thread))

    async def delete_thread(self, thread_id: str) -> None:
        for path in self._find_thread_files(thread_id):
            path.unlink(missing_ok=True)

    # ------------------------------------------------------------------
    # Steps (messages)
    # ------------------------------------------------------------------

    async def create_step(self, step_dict: Any) -> None:
        thread_id = step_dict.get("threadId", "")
        if not thread_id:
            return
        for path in self._find_thread_files(thread_id):
            data: Any = self._read_thread(path)
            if not data:
                return
            steps: list[Any] = data.get("steps") or []
            steps.append(step_dict)
            data["steps"] = steps

            if not data.get("name") and step_dict.get("type") == "user_message":
                output = step_dict.get("output", "")
                data["name"] = output[:50] if output else None

            self._write_thread(path, data)
            return

    async def update_step(self, step_dict: Any) -> None:
        thread_id = step_dict.get("threadId", "")
        step_id = step_dict.get("id", "")
        if not thread_id or not step_id:
            return
        for path in self._find_thread_files(thread_id):
            data: Any = self._read_thread(path)
            if not data:
                return
            steps: list[Any] = data.get("steps") or []
            for i, s in enumerate(steps):
                if s.get("id") == step_id:
                    steps[i] = step_dict
                    break
            else:
                steps.append(step_dict)
            data["steps"] = steps
            self._write_thread(path, data)
            return

    async def delete_step(self, step_id: str) -> None:
        pass

    # ------------------------------------------------------------------
    # Elements, Feedback (заглушки)
    # ------------------------------------------------------------------

    async def create_element(self, element: "object") -> None:
        pass

    async def get_element(
        self, thread_id: str, element_id: str
    ) -> Optional["dict"]:
        return None

    async def delete_element(
        self, element_id: str, thread_id: Optional[str] = None
    ) -> None:
        pass

    async def upsert_feedback(self, feedback: Feedback) -> str:
        return feedback.id or str(uuid.uuid4())

    async def delete_feedback(self, feedback_id: str) -> bool:
        return True

    async def get_favorite_steps(self, user_id: str) -> list:
        return []

    async def build_debug_url(self) -> str:
        return ""

    async def close(self) -> None:
        pass

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _iter_folders(self):
        """Итерировать папки документов в base_dir."""
        if not self._base_dir.is_dir():
            return
        for d in self._base_dir.iterdir():
            if d.is_dir() and not d.name.startswith("."):
                yield d

    def _find_thread_files(self, thread_id: str):
        """Найти JSON-файл thread по ID во всех folders."""
        for folder_dir in self._iter_folders():
            path = folder_dir / self._boba / self._chats / f"{thread_id}.json"
            if path.is_file():
                yield path

    def _read_thread(self, path: Path) -> Optional[ThreadDict]:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            data.setdefault("steps", [])
            data.setdefault("elements", [])
            return data
        except (json.JSONDecodeError, OSError) as e:
            log.warning("Failed to read thread %s: %s", path, e)
            return None

    def _write_thread(self, path: Path, thread: ThreadDict) -> None:
        try:
            path.write_text(
                json.dumps(thread, ensure_ascii=False, indent=2, default=str),
                encoding="utf-8",
            )
        except OSError as e:
            log.warning("Failed to write thread %s: %s", path, e)
