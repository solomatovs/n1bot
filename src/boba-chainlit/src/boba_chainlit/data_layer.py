"""Chainlit DataLayer адаптер — мост между Chainlit types и ChatThreadStore.

StepInput — типизированный входной шаг от Chainlit (валидация на границе).
ChainlitConverter — конвертация ThreadDict ↔ ChatThread, StepInput → ChatStep.
ChainlitDataLayerAdapter — реализация BaseDataLayer, делегирующая JsonThreadStore.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
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

from boba_adapters.json_thread_store import JsonThreadStore
from boba_domain.chat.thread import (
    ChatStep,
    ChatThread,
    StepFeedback,
    StepType,
    ThreadMetadata,
)
from boba_domain.errors import (
    ThreadNotFoundError,
    ThreadReadError,
    ThreadStoreError,
    ThreadWriteError,
    ValidationError,
)

log = logging.getLogger(__name__)

_STEP_REQUIRED_FIELDS = ("id", "threadId", "type")


async def _emit_error(text: str) -> None:
    """Отправить ошибку в браузер через Chainlit toast.

    Если нет активного контекста (вызов вне WebSocket-сессии) — пропускаем.
    В логи пишет вызывающий код, здесь только UI.
    """
    try:
        import chainlit as cl

        await cl.context.emitter.emit("toast", {"message": text, "level": "error"})
    except Exception:
        pass


# ------------------------------------------------------------------
# StepInput — типизированный входной шаг от Chainlit
# ------------------------------------------------------------------


@dataclass(frozen=True)
class StepInput:
    """Входной шаг от Chainlit после валидации.

    Поля соответствуют Chainlit StepDict, но приведены к snake_case
    и гарантированно заполнены для обязательных полей.
    """

    id: str
    thread_id: str
    type: str
    name: str
    parent_id: str | None
    input: str
    output: str
    created_at: str
    start: str | None
    end: str | None
    streaming: bool
    is_error: bool
    metadata: dict[str, Any]
    tags: list[str] | None
    language: str | None
    generation: dict[str, Any] | None
    show_input: str | None
    default_open: bool
    auto_collapse: bool
    feedback: dict[str, Any] | None

    @classmethod
    def parse(cls, raw: dict) -> StepInput:
        """Парсинг и валидация step_dict от Chainlit.

        Raises:
            ValidationError: если отсутствует хотя бы одно обязательное поле.
        """
        missing = [f for f in _STEP_REQUIRED_FIELDS if not raw.get(f)]
        if missing:
            raise ValidationError(
                f"Step missing required fields {missing}: "
                f"id={raw.get('id', '?')}, name={raw.get('name', '?')}"
            )
        return cls(
            id=raw["id"],
            thread_id=raw["threadId"],
            type=raw["type"],
            name=raw.get("name", ""),
            parent_id=raw.get("parentId"),
            input=raw.get("input", ""),
            output=raw.get("output", ""),
            created_at=raw.get("createdAt", ""),
            start=raw.get("start"),
            end=raw.get("end"),
            streaming=raw.get("streaming", False),
            is_error=raw.get("isError", False),
            metadata=raw.get("metadata") or {},
            tags=raw.get("tags"),
            language=raw.get("language"),
            generation=raw.get("generation"),
            show_input=raw.get("showInput"),
            default_open=raw.get("defaultOpen", False),
            auto_collapse=raw.get("autoCollapse", False),
            feedback=raw.get("feedback"),
        )


# ------------------------------------------------------------------
# Converter: Chainlit types ↔ Domain types
# ------------------------------------------------------------------


class ChainlitConverter:
    """Конвертация между Chainlit TypedDict'ами и доменными dataclass'ами."""

    @staticmethod
    def to_thread_dict(thread: ChatThread) -> ThreadDict:
        result: ThreadDict = {
            "id": thread.id,
            "createdAt": thread.created_at,
            "name": thread.name,
            "userId": thread.user_id,
            "userIdentifier": thread.user_identifier,
            "metadata": {
                "folder": thread.metadata.folder,
                "model": thread.metadata.model,
            },
            "tags": thread.tags,
            "steps": cast(
                list,
                [ChainlitConverter.to_step_dict(s) for s in thread.steps],
            ),
            "elements": [],
        }
        return result

    @staticmethod
    def to_step_dict(step: ChatStep) -> dict:
        result: dict = {
            "id": step.id,
            "threadId": step.thread_id,
            "type": step.step_type.value,
            "output": step.output,
            "input": step.input,
            "createdAt": step.created_at,
            "name": step.name,
            "parentId": step.parent_id,
        }
        if step.feedback is not None:
            result["feedback"] = {
                "id": step.feedback.id,
                "value": step.feedback.value,
                "comment": step.feedback.comment,
                "strategy": step.feedback.strategy,
            }
        if step.is_favorite:
            result["metadata"] = {"favorite": True}
        return result

    @staticmethod
    def from_step_input(step: StepInput) -> ChatStep:
        """Конвертация валидированного StepInput → доменный ChatStep."""
        feedback = None
        if isinstance(step.feedback, dict) and "id" in step.feedback:
            feedback = StepFeedback(
                id=step.feedback["id"],
                value=step.feedback.get("value", 0),
                comment=step.feedback.get("comment", ""),
                strategy=step.feedback.get("strategy", "user"),
            )

        is_favorite = step.metadata.get("favorite", False)

        return ChatStep(
            id=step.id,
            thread_id=step.thread_id,
            step_type=_parse_step_type(step.type),
            output=step.output,
            input=step.input,
            created_at=step.created_at,
            name=step.name,
            parent_id=step.parent_id,
            feedback=feedback,
            is_favorite=is_favorite,
        )

    @staticmethod
    def feedback_from_chainlit(feedback: Feedback) -> StepFeedback:
        return StepFeedback(
            id=feedback.id or str(uuid.uuid4()),
            value=feedback.value,
            comment=feedback.comment or "",
        )

    @staticmethod
    def metadata_from_dict(raw: dict) -> ThreadMetadata:
        if isinstance(raw, str):
            import json

            raw = json.loads(raw)
        return ThreadMetadata(
            folder=raw.get("folder", ""),
            model=raw.get("model", ""),
        )


# ------------------------------------------------------------------
# Adapter: BaseDataLayer → JsonThreadStore
# ------------------------------------------------------------------


class ChainlitDataLayerAdapter(BaseDataLayer):
    """Chainlit DataLayer, делегирующий хранение в JsonThreadStore."""

    def __init__(self, store: JsonThreadStore) -> None:
        self._store = store

    # -- User (нет auth — default user) --

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

    # -- Threads --

    async def get_thread(self, thread_id: str) -> Optional[ThreadDict]:
        try:
            thread = self._store.get_thread(thread_id)
        except ThreadReadError as e:
            log.error("get_thread: %s", e)
            await _emit_error(str(e))
            return None
        if thread is None:
            log.warning("get_thread: thread not found: %s", thread_id)
            await _emit_error(f"Thread not found: {thread_id}")
            return None
        return ChainlitConverter.to_thread_dict(thread)

    async def get_thread_author(self, thread_id: str) -> str:
        try:
            thread = self._store.get_thread(thread_id)
        except ThreadReadError as e:
            log.error("get_thread_author: %s", e)
            await _emit_error(str(e))
            return "default"
        if thread is None:
            log.warning("get_thread_author: thread not found: %s", thread_id)
            await _emit_error(f"Thread author not found: {thread_id}")
            return "default"
        return thread.user_identifier

    async def list_threads(
        self, pagination: Pagination, filters: ThreadFilter
    ) -> PaginatedResponse[ThreadDict]:
        offset = int(pagination.cursor) if pagination.cursor else 0

        try:
            page = self._store.list_threads(
                limit=pagination.first,
                offset=offset,
                search=filters.search,
            )
        except ThreadReadError as e:
            log.error("list_threads: %s", e)
            await _emit_error(str(e))
            return PaginatedResponse(
                pageInfo=PageInfo(
                    hasNextPage=False, startCursor="0", endCursor="0",
                ),
                data=[],
            )

        return PaginatedResponse(
            pageInfo=PageInfo(
                hasNextPage=page.has_next,
                startCursor=str(offset),
                endCursor=str(offset + len(page.threads)),
            ),
            data=[ChainlitConverter.to_thread_dict(t) for t in page.threads],
        )

    async def update_thread(
        self,
        thread_id: str,
        name: Optional[str] = None,
        user_id: Optional[str] = None,
        metadata: Optional[Dict] = None,
        tags: Optional[List[str]] = None,
    ) -> None:
        try:
            thread = self._store.get_thread(thread_id)
        except ThreadReadError as e:
            log.error("update_thread: %s", e)
            await _emit_error(str(e))
            return

        if thread is None:
            meta = ChainlitConverter.metadata_from_dict(metadata or {})
            if not meta.folder:
                return
            thread = ChatThread(
                id=thread_id,
                created_at=datetime.now(timezone.utc).isoformat(),
                name=name,
                user_id=user_id or "default",
                user_identifier="default",
                metadata=meta,
                tags=tags or [],
            )
            try:
                self._store.save_thread(thread)
            except ThreadWriteError as e:
                log.error("update_thread (create): %s", e)
                await _emit_error(str(e))
            return

        effective_name = name if name is not None else thread.name

        updated_meta = thread.metadata
        if metadata is not None:
            merged = ChainlitConverter.metadata_from_dict(metadata)
            updated_meta = ThreadMetadata(
                folder=merged.folder or thread.metadata.folder,
                model=merged.model or thread.metadata.model,
            )

        updated = ChatThread(
            id=thread.id,
            created_at=thread.created_at,
            name=effective_name,
            user_id=user_id if user_id is not None else thread.user_id,
            user_identifier=thread.user_identifier,
            metadata=updated_meta,
            steps=thread.steps,
            tags=tags if tags is not None else thread.tags,
        )
        try:
            self._store.save_thread(updated)
        except ThreadWriteError as e:
            log.error("update_thread (save): %s", e)
            await _emit_error(str(e))

    async def delete_thread(self, thread_id: str) -> None:
        try:
            self._store.delete_thread(thread_id)
        except ThreadStoreError as e:
            log.error("delete_thread: %s", e)
            await _emit_error(str(e))
            raise

    # -- Steps --

    async def create_step(self, step_dict: dict) -> None:
        """Сохранить новый шаг чата. Вызывается Chainlit при каждом событии."""
        step_input = StepInput.parse(step_dict)
        step = ChainlitConverter.from_step_input(step_input)
        try:
            self._store.add_step(step_input.thread_id, step)
        except ThreadNotFoundError as e:
            log.error("create_step: %s", e)
            await _emit_error(str(e))
        except ThreadWriteError as e:
            log.error("create_step: %s", e)
            await _emit_error(str(e))

    async def update_step(self, step_dict: dict) -> None:
        """Обновить существующий шаг чата."""
        step_input = StepInput.parse(step_dict)
        step = ChainlitConverter.from_step_input(step_input)
        try:
            self._store.update_step(step_input.thread_id, step)
        except ThreadNotFoundError as e:
            log.error("update_step: %s", e)
            await _emit_error(str(e))
        except ThreadWriteError as e:
            log.error("update_step: %s", e)
            await _emit_error(str(e))

    async def delete_step(self, step_id: str) -> None:
        try:
            for thread in self._store.list_threads(limit=10000).threads:
                for step in thread.steps:
                    if step.id == step_id:
                        self._store.delete_step(thread.id, step_id)
                        return
        except ThreadStoreError as e:
            log.error("delete_step: %s", e)
            await _emit_error(str(e))

    # -- Feedback --

    async def upsert_feedback(self, feedback: Feedback) -> str:
        domain_fb = ChainlitConverter.feedback_from_chainlit(feedback)

        try:
            if feedback.forId:
                for thread in self._store.list_threads(limit=10000).threads:
                    for step in thread.steps:
                        if step.id == feedback.forId:
                            self._store.set_feedback(thread.id, step.id, domain_fb)
                            return domain_fb.id
        except ThreadStoreError as e:
            log.error("upsert_feedback: %s", e)
            await _emit_error(str(e))

        return domain_fb.id

    async def delete_feedback(self, feedback_id: str) -> bool:
        try:
            for thread in self._store.list_threads(limit=10000).threads:
                for step in thread.steps:
                    if step.feedback and step.feedback.id == feedback_id:
                        self._store.set_feedback(thread.id, step.id, None)
                        return True
        except ThreadStoreError as e:
            log.error("delete_feedback: %s", e)
            await _emit_error(str(e))
        return False

    async def get_favorite_steps(self, user_id: str) -> list:
        try:
            steps = self._store.get_favorite_steps(user_id)
        except ThreadStoreError as e:
            log.error("get_favorite_steps: %s", e)
            await _emit_error(str(e))
            return []
        return [ChainlitConverter.to_step_dict(s) for s in steps]

    # -- Elements (заглушки) --

    async def create_element(self, _element: object) -> None:
        pass

    async def get_element(self, _thread_id: str, _element_id: str) -> Optional[dict]:
        return None

    async def delete_element(
        self, _element_id: str, _thread_id: Optional[str] = None
    ) -> None:
        pass

    # -- Misc --

    async def build_debug_url(self) -> str:
        return ""

    async def close(self) -> None:
        pass


# ------------------------------------------------------------------
# Helper
# ------------------------------------------------------------------


def _parse_step_type(value: str) -> StepType:
    try:
        return StepType(value)
    except ValueError:
        return StepType.RUN
