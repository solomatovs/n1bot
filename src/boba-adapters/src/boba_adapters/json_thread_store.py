"""Файловое хранилище thread'ов — один thread.json на workspace.

Каждый workspace (папка в import_base_dir) хранит ровно один thread.
Все пути резолвятся через AppConfig.
"""
from __future__ import annotations

import json
import logging
import shutil
from dataclasses import asdict
from pathlib import Path

from boba_domain.chat.thread import (
    ChatStep,
    ChatThread,
    StepFeedback,
    StepType,
    ThreadMetadata,
)
from boba_domain.config import AppConfig
from boba_domain.core.thread_store import ThreadPage
from boba_domain.errors import (
    ThreadDeleteError,
    ThreadNotFoundError,
    ThreadReadError,
    ThreadWriteError,
    ValidationError,
)

log = logging.getLogger(__name__)


class JsonThreadStore:
    """Один thread.json на workspace. Все пути через AppConfig."""

    def __init__(self, cfg: AppConfig) -> None:
        self._cfg = cfg

    # ------------------------------------------------------------------
    # Thread CRUD
    # ------------------------------------------------------------------

    def get_thread(self, thread_id: str) -> ChatThread | None:
        for folder_dir in self._cfg.iter_workspaces():
            path = self._cfg.thread_path(folder_dir)
            thread = self._read(path)
            if thread is not None and thread.id == thread_id:
                return thread
        return None

    def get_thread_by_folder(self, folder_name: str) -> ChatThread | None:
        path = self._cfg.thread_path(self._cfg.folder_path(folder_name))
        return self._read(path)

    def list_threads(
        self,
        limit: int,
        offset: int = 0,
        search: str | None = None,
    ) -> ThreadPage:
        threads: list[ChatThread] = []
        for folder_dir in self._cfg.iter_workspaces():
            thread = self._read(self._cfg.thread_path(folder_dir))
            if thread is None:
                continue
            if search and search.lower() not in (thread.name or "").lower():
                continue
            threads.append(thread)

        threads.sort(key=lambda t: t.created_at, reverse=True)
        page = threads[offset : offset + limit]
        return ThreadPage(threads=page, has_next=offset + limit < len(threads))

    def save_thread(self, thread: ChatThread) -> None:
        if not thread.metadata.folder:
            raise ValidationError("thread.metadata.folder is required")
        path = self._cfg.thread_path(self._cfg.folder_path(thread.metadata.folder))
        path.parent.mkdir(parents=True, exist_ok=True)
        self._write(path, thread)

    def delete_thread(self, thread_id: str) -> None:
        for folder_dir in self._cfg.iter_workspaces():
            path = self._cfg.thread_path(folder_dir)
            thread = self._read(path)
            if thread is not None and thread.id == thread_id:
                try:
                    shutil.rmtree(folder_dir)
                except OSError as e:
                    raise ThreadDeleteError(thread_id, e) from e
                return
        raise ThreadNotFoundError(thread_id)

    def add_step(self, thread_id: str, step: ChatStep) -> None:
        thread = self.get_thread(thread_id)
        if thread is None:
            raise ThreadNotFoundError(thread_id)

        steps = [*thread.steps, step]
        name = thread.name
        if not name and step.step_type is StepType.USER_MESSAGE and step.output:
            name = step.output[:50]

        self._save_updated(thread, steps, name=name)

    def update_step(self, thread_id: str, step: ChatStep) -> None:
        thread = self.get_thread(thread_id)
        if thread is None:
            raise ThreadNotFoundError(thread_id)

        steps = list(thread.steps)
        for i, s in enumerate(steps):
            if s.id == step.id:
                steps[i] = step
                break
        else:
            steps.append(step)

        self._save_updated(thread, steps)

    def delete_step(self, thread_id: str, step_id: str) -> None:
        thread = self.get_thread(thread_id)
        if thread is None:
            raise ThreadNotFoundError(thread_id)

        steps = [s for s in thread.steps if s.id != step_id]
        if len(steps) < len(thread.steps):
            self._save_updated(thread, steps)

    # ------------------------------------------------------------------
    # Feedback
    # ------------------------------------------------------------------

    def set_feedback(
        self, thread_id: str, step_id: str, feedback: StepFeedback | None
    ) -> None:
        thread = self.get_thread(thread_id)
        if thread is None:
            raise ThreadNotFoundError(thread_id)

        steps = list(thread.steps)
        for i, s in enumerate(steps):
            if s.id == step_id:
                steps[i] = ChatStep(
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

        self._save_updated(thread, steps)

    def get_favorite_steps(self, user_id: str) -> list[ChatStep]:
        favorites: list[ChatStep] = []
        for folder_dir in self._cfg.iter_workspaces():
            thread = self._read(self._cfg.thread_path(folder_dir))
            if thread is None:
                continue
            favorites.extend(s for s in thread.steps if s.is_favorite)
        return favorites

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _save_updated(
        self,
        thread: ChatThread,
        steps: list[ChatStep],
        name: str | None = None,
    ) -> None:
        self.save_thread(ChatThread(
            id=thread.id,
            created_at=thread.created_at,
            name=name if name is not None else thread.name,
            user_id=thread.user_id,
            user_identifier=thread.user_identifier,
            metadata=thread.metadata,
            steps=steps,
            tags=thread.tags,
        ))

    def _read(self, path: Path) -> ChatThread | None:
        if not path.is_file():
            return None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            return _deserialize_thread(raw)
        except (json.JSONDecodeError, OSError, KeyError) as e:
            raise ThreadReadError(str(path), e) from e

    def _write(self, path: Path, thread: ChatThread) -> None:
        try:
            raw = _serialize_thread(thread)
            path.write_text(
                json.dumps(raw, ensure_ascii=False, indent=2, default=str),
                encoding="utf-8",
            )
        except OSError as e:
            raise ThreadWriteError(str(path), e) from e


# ------------------------------------------------------------------
# Serialization
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
