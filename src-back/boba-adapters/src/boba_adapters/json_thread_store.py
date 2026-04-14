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
from collections.abc import Iterator
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

    def _iter_workspace_dirs(self) -> Iterator[Path]:
        """Все папки-workspace'ы (не начинающиеся с точки)."""
        base = Path(self._cfg.import_base_dir)
        if not base.is_dir():
            return
        for d in base.iterdir():
            if d.is_dir() and not d.name.startswith("."):
                yield d

    # ------------------------------------------------------------------
    # Thread CRUD
    # ------------------------------------------------------------------

    def get_thread_by_folder(self, folder_name: str) -> ChatThread:
        """Прочитать thread по имени папки. Бросает ThreadReadError."""
        workspace_path = self._cfg.workspace_path(folder_name)
        path = self._cfg.thread_path(workspace_path)
        return self._read(path)

    def iter_threads(
        self,
        search: str | None = None,
    ) -> Iterator[ChatThread]:
        """Итерация по всем thread'ам, от новых к старым (по created_at)."""
        threads: list[ChatThread] = []
        for folder_dir in self._iter_workspace_dirs():
            try:
                thread = self._read(self._cfg.thread_path(folder_dir))
            except ThreadReadError as e:
                log.error("iter_threads: %s", e)
                continue

            if search and not thread.matches_search(search):
                continue

            threads.append(thread)

        threads.sort(key=lambda t: t.created_at, reverse=True)
        yield from threads

    def save_thread(self, thread: ChatThread) -> None:
        if not thread.metadata.folder:
            raise ValidationError("thread.metadata.folder is required")

        workspace = self._cfg.thread_path(self._cfg.workspace_path(thread.metadata.folder))
        workspace.parent.mkdir(parents=True, exist_ok=True)
        self._write(workspace, thread)

    def delete_thread(self, folder_name: str) -> None:
        folder_dir = self._cfg.workspace_path(folder_name)
        if not folder_dir.is_dir():
            raise ThreadNotFoundError(folder_name)
        try:
            shutil.rmtree(folder_dir)
        except OSError as e:
            raise ThreadDeleteError(folder_name, e) from e

    def _get_current_or_new_chat_name(self, step: ChatStep) -> str:
        default_name = "New chat"

        if step.step_type is not StepType.USER_MESSAGE or not step.output:
            return default_name

        return step.output[:50] or default_name

    def add_step(self, folder_name: str, step: ChatStep) -> None:
        thread = self.get_thread_by_folder(folder_name)
        steps = [*thread.steps, step]

        if thread.has_auto_name():
            name = self._get_current_or_new_chat_name(step)
            self._save_updated(thread, steps, name=name)
        else:
            self._save_updated(thread, steps)

    def update_step(self, folder_name: str, step: ChatStep) -> None:
        thread = self.get_thread_by_folder(folder_name)
        steps = list(thread.steps)
        for i, s in enumerate(steps):
            if s.id == step.id:
                steps[i] = step
                break
        else:
            steps.append(step)
        self._save_updated(thread, steps)

    def delete_step(self, folder_name: str, step_id: str) -> None:
        thread = self.get_thread_by_folder(folder_name)
        steps = [s for s in thread.steps if s.id != step_id]
        if len(steps) < len(thread.steps):
            self._save_updated(thread, steps)

    # ------------------------------------------------------------------
    # Feedback
    # ------------------------------------------------------------------

    def set_feedback(
        self, folder_name: str, step_id: str, feedback: StepFeedback | None
    ) -> None:
        thread = self.get_thread_by_folder(folder_name)
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
        for folder_dir in self._iter_workspace_dirs():
            try:
                thread = self._read(self._cfg.thread_path(folder_dir))
            except ThreadReadError as e:
                log.error("get_favorite_steps: %s", e)
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
        name: str = "",
    ) -> None:
        self.save_thread(
            ChatThread(
                id=thread.id,
                created_at=thread.created_at,
                name=name or thread.name,
                user_id=thread.user_id,
                user_identifier=thread.user_identifier,
                metadata=thread.metadata,
                steps=steps,
                tags=thread.tags,
            )
        )

    def _read(self, path: Path) -> ChatThread:
        """Прочитать thread.json. Бросает ThreadReadError при любой ошибке."""
        if not path.is_file():
            raise ThreadReadError(str(path), FileNotFoundError(f"{path} not found"))
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
        name=raw.get("name") or raw["id"][:12],
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
