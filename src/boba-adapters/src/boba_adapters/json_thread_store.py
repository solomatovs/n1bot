"""Файловое хранилище thread'ов — JSON файлы в .boba/chats/.

Каждый thread — отдельный {thread_id}.json.
Структура: {base_dir}/{folder}/.boba/chats/{thread_id}.json
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict
from pathlib import Path

from boba_domain.chat.thread import (
    ChatStep,
    ChatThread,
    StepFeedback,
    StepType,
    ThreadMetadata,
)
from boba_domain.core.thread_store import ThreadPage

log = logging.getLogger(__name__)


class JsonThreadStore:
    """ChatThreadStore реализация на JSON-файлах."""

    def __init__(
        self,
        base_dir: Path,
        boba_dir_name: str = ".boba",
        chats_dir_name: str = "chats",
    ) -> None:
        self._base_dir = base_dir
        self._boba = boba_dir_name
        self._chats = chats_dir_name

    # ------------------------------------------------------------------
    # Thread CRUD
    # ------------------------------------------------------------------

    def get_thread(self, thread_id: str) -> ChatThread | None:
        path = self._find_thread_path(thread_id)
        if path is None:
            return None
        return self._read(path)

    def list_threads(
        self,
        limit: int,
        offset: int = 0,
        search: str | None = None,
    ) -> ThreadPage:
        threads = self._collect_all_threads(search)
        threads.sort(key=lambda t: t.created_at, reverse=True)

        page = threads[offset : offset + limit]
        has_next = offset + limit < len(threads)
        return ThreadPage(threads=page, has_next=has_next)

    def save_thread(self, thread: ChatThread) -> None:
        path = self._ensure_thread_path(thread)
        self._write(path, thread)

    def delete_thread(self, thread_id: str) -> None:
        path = self._find_thread_path(thread_id)
        if path is not None:
            path.unlink(missing_ok=True)

    # ------------------------------------------------------------------
    # Step CRUD
    # ------------------------------------------------------------------

    def add_step(self, thread_id: str, step: ChatStep) -> None:
        thread = self._load_or_none(thread_id)
        if thread is None:
            return

        updated_steps = list(thread.steps)
        updated_steps.append(step)

        name = thread.name
        if not name and step.step_type is StepType.USER_MESSAGE and step.output:
            name = step.output[:50]

        self._save_with_steps(thread, updated_steps, name=name)

    def update_step(self, thread_id: str, step: ChatStep) -> None:
        thread = self._load_or_none(thread_id)
        if thread is None:
            return

        updated_steps = list(thread.steps)
        replaced = False
        for i, s in enumerate(updated_steps):
            if s.id == step.id:
                updated_steps[i] = step
                replaced = True
                break
        if not replaced:
            updated_steps.append(step)

        self._save_with_steps(thread, updated_steps)

    def delete_step(self, thread_id: str, step_id: str) -> None:
        thread = self._load_or_none(thread_id)
        if thread is None:
            return

        updated_steps = [s for s in thread.steps if s.id != step_id]
        if len(updated_steps) == len(thread.steps):
            return

        self._save_with_steps(thread, updated_steps)

    # ------------------------------------------------------------------
    # Feedback
    # ------------------------------------------------------------------

    def set_feedback(
        self, thread_id: str, step_id: str, feedback: StepFeedback | None
    ) -> None:
        thread = self._load_or_none(thread_id)
        if thread is None:
            return

        updated_steps = list(thread.steps)
        for i, s in enumerate(updated_steps):
            if s.id == step_id:
                updated_steps[i] = ChatStep(
                    id=s.id,
                    thread_id=s.thread_id,
                    step_type=s.step_type,
                    output=s.output,
                    input=s.input,
                    created_at=s.created_at,
                    name=s.name,
                    parent_id=s.parent_id,
                    feedback=feedback,
                    is_favorite=s.is_favorite,
                )
                break

        self._save_with_steps(thread, updated_steps)

    def get_favorite_steps(self, user_id: str) -> list[ChatStep]:
        favorites: list[ChatStep] = []
        for thread in self._collect_all_threads():
            for step in thread.steps:
                if step.is_favorite:
                    favorites.append(step)
        return favorites

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _load_or_none(self, thread_id: str) -> ChatThread | None:
        path = self._find_thread_path(thread_id)
        if path is None:
            return None
        return self._read(path)

    def _save_with_steps(
        self,
        thread: ChatThread,
        steps: list[ChatStep],
        name: str | None = None,
    ) -> None:
        updated = ChatThread(
            id=thread.id,
            created_at=thread.created_at,
            name=name if name is not None else thread.name,
            user_id=thread.user_id,
            user_identifier=thread.user_identifier,
            metadata=thread.metadata,
            steps=steps,
            tags=thread.tags,
        )
        self.save_thread(updated)

    def _collect_all_threads(self, search: str | None = None) -> list[ChatThread]:
        threads: list[ChatThread] = []
        for folder_dir in self._iter_folders():
            chats_dir = folder_dir / self._boba / self._chats
            if not chats_dir.is_dir():
                continue
            for path in chats_dir.glob("*.json"):
                thread = self._read(path)
                if thread is None:
                    continue
                if search and search.lower() not in (thread.name or "").lower():
                    continue
                threads.append(thread)
        return threads

    # ------------------------------------------------------------------
    # File I/O
    # ------------------------------------------------------------------

    def _iter_folders(self):
        if not self._base_dir.is_dir():
            return
        for d in self._base_dir.iterdir():
            if d.is_dir() and not d.name.startswith("."):
                yield d

    def _find_thread_path(self, thread_id: str) -> Path | None:
        for folder_dir in self._iter_folders():
            path = folder_dir / self._boba / self._chats / f"{thread_id}.json"
            if path.is_file():
                return path
        return None

    def _ensure_thread_path(self, thread: ChatThread) -> Path:
        folder = thread.metadata.folder or "_default"
        chats_dir = self._base_dir / folder / self._boba / self._chats
        chats_dir.mkdir(parents=True, exist_ok=True)
        return chats_dir / f"{thread.id}.json"

    def _read(self, path: Path) -> ChatThread | None:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            return _deserialize_thread(raw)
        except (json.JSONDecodeError, OSError, KeyError) as e:
            log.warning("Failed to read thread %s: %s", path, e)
            return None

    def _write(self, path: Path, thread: ChatThread) -> None:
        try:
            raw = _serialize_thread(thread)
            path.write_text(
                json.dumps(raw, ensure_ascii=False, indent=2, default=str),
                encoding="utf-8",
            )
        except OSError as e:
            log.warning("Failed to write thread %s: %s", path, e)


# ------------------------------------------------------------------
# Serialization (module-level, stateless)
# ------------------------------------------------------------------


def _serialize_thread(thread: ChatThread) -> dict:
    return {
        "id": thread.id,
        "createdAt": thread.created_at,
        "name": thread.name,
        "userId": thread.user_id,
        "userIdentifier": thread.user_identifier,
        "metadata": asdict(thread.metadata),
        "tags": thread.tags,
        "steps": [_serialize_step(s) for s in thread.steps],
    }


def _serialize_step(step: ChatStep) -> dict:
    result: dict = {
        "id": step.id,
        "threadId": step.thread_id,
        "type": step.step_type.value,
        "output": step.output,
        "input": step.input,
        "createdAt": step.created_at,
        "name": step.name,
        "parentId": step.parent_id,
        "isFavorite": step.is_favorite,
    }
    if step.feedback is not None:
        result["feedback"] = asdict(step.feedback)
    return result


def _deserialize_thread(raw: dict) -> ChatThread:
    meta_raw = raw.get("metadata") or {}
    if isinstance(meta_raw, str):
        meta_raw = json.loads(meta_raw)

    return ChatThread(
        id=raw["id"],
        created_at=raw.get("createdAt", ""),
        name=raw.get("name"),
        user_id=raw.get("userId", "default"),
        user_identifier=raw.get("userIdentifier", "default"),
        metadata=ThreadMetadata(
            folder=meta_raw.get("folder", ""),
            model=meta_raw.get("model", ""),
        ),
        steps=[_deserialize_step(s) for s in raw.get("steps", [])],
        tags=raw.get("tags", []),
    )


def _deserialize_step(raw: dict) -> ChatStep:
    feedback_raw = raw.get("feedback")
    feedback = None
    if isinstance(feedback_raw, dict) and "id" in feedback_raw:
        feedback = StepFeedback(
            id=feedback_raw["id"],
            value=feedback_raw.get("value", 0),
            comment=feedback_raw.get("comment", ""),
            strategy=feedback_raw.get("strategy", "user"),
        )

    return ChatStep(
        id=raw.get("id", ""),
        thread_id=raw.get("threadId", ""),
        step_type=_parse_step_type(raw.get("type", "")),
        output=raw.get("output", ""),
        input=raw.get("input", ""),
        created_at=raw.get("createdAt", ""),
        name=raw.get("name", ""),
        parent_id=raw.get("parentId"),
        feedback=feedback,
        is_favorite=raw.get("isFavorite", False),
    )


def _parse_step_type(value: str) -> StepType:
    try:
        return StepType(value)
    except ValueError:
        return StepType.RUN
