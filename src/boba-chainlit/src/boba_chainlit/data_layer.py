"""Chainlit DataLayer адаптер — мост между Chainlit types и ChatThreadStore.

ChainlitConverter — конвертация ThreadDict ↔ ChatThread, StepDict ↔ ChatStep.
ChainlitDataLayerAdapter — реализация BaseDataLayer, делегирующая JsonThreadStore.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Dict, List, Optional, cast

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
    def from_step_dict(raw: dict) -> ChatStep:
        feedback_raw = raw.get("feedback")
        feedback = None
        if isinstance(feedback_raw, dict) and "id" in feedback_raw:
            feedback = StepFeedback(
                id=feedback_raw["id"],
                value=feedback_raw.get("value", 0),
                comment=feedback_raw.get("comment", ""),
                strategy=feedback_raw.get("strategy", "user"),
            )

        meta = raw.get("metadata") or {}
        is_favorite = meta.get("favorite", False) if isinstance(meta, dict) else False

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
        thread = self._store.get_thread(thread_id)
        if thread is None:
            return None
        return ChainlitConverter.to_thread_dict(thread)

    async def get_thread_author(self, thread_id: str) -> str:
        thread = self._store.get_thread(thread_id)
        return thread.user_identifier if thread else "default"

    async def list_threads(
        self, pagination: Pagination, filters: ThreadFilter
    ) -> PaginatedResponse[ThreadDict]:
        offset = int(pagination.cursor) if pagination.cursor else 0

        page = self._store.list_threads(
            limit=pagination.first,
            offset=offset,
            search=filters.search,
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
        thread = self._store.get_thread(thread_id)

        if thread is None:
            meta = ChainlitConverter.metadata_from_dict(metadata or {})
            thread = ChatThread(
                id=thread_id,
                created_at=datetime.now(timezone.utc).isoformat(),
                name=name,
                user_id=user_id or "default",
                user_identifier="default",
                metadata=meta,
                tags=tags or [],
            )
            self._store.save_thread(thread)
            return

        updated_meta = thread.metadata
        if metadata:
            merged = ChainlitConverter.metadata_from_dict(metadata)
            updated_meta = ThreadMetadata(
                folder=merged.folder or thread.metadata.folder,
                model=merged.model or thread.metadata.model,
            )

        # Chainlit автоименует thread текстом первого сообщения (emitter.py).
        # Если workspace уже имеет имя и metadata не передан — это авто-rename,
        # игнорируем его, сохраняя имя workspace.
        if thread.metadata.folder and metadata is None:
            effective_name = thread.name
        else:
            effective_name = name if name is not None else thread.name

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
        self._store.save_thread(updated)

    async def delete_thread(self, thread_id: str) -> None:
        self._store.delete_thread(thread_id)

    # -- Steps --

    async def create_step(self, step_dict: dict) -> None:
        thread_id = step_dict.get("threadId", "")
        if not thread_id:
            return
        step = ChainlitConverter.from_step_dict(step_dict)
        self._store.add_step(thread_id, step)

    async def update_step(self, step_dict: dict) -> None:
        thread_id = step_dict.get("threadId", "")
        if not thread_id:
            return
        step = ChainlitConverter.from_step_dict(step_dict)
        self._store.update_step(thread_id, step)

    async def delete_step(self, step_id: str) -> None:
        for thread in self._store.list_threads(limit=10000).threads:
            for step in thread.steps:
                if step.id == step_id:
                    self._store.delete_step(thread.id, step_id)
                    return

    # -- Feedback --

    async def upsert_feedback(self, feedback: Feedback) -> str:
        domain_fb = ChainlitConverter.feedback_from_chainlit(feedback)

        if feedback.forId:
            for thread in self._store.list_threads(limit=10000).threads:
                for step in thread.steps:
                    if step.id == feedback.forId:
                        self._store.set_feedback(thread.id, step.id, domain_fb)
                        return domain_fb.id

        return domain_fb.id

    async def delete_feedback(self, feedback_id: str) -> bool:
        for thread in self._store.list_threads(limit=10000).threads:
            for step in thread.steps:
                if step.feedback and step.feedback.id == feedback_id:
                    self._store.set_feedback(thread.id, step.id, None)
                    return True
        return False

    async def get_favorite_steps(self, user_id: str) -> list:
        steps = self._store.get_favorite_steps(user_id)
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
