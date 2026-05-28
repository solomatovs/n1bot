"""Chainlit `DataLayer`-adapter: users/threads из репо, steps из HistoryService.

Источник правды для сообщений — `HistoryService` агента: `get_thread`
рендерит steps через `replay_history_to_steps_sync(...)`. Здесь же
остаются только меты тредов (id, name, tags, workspace_id, timestamps) +
пользователи для chainlit-auth.

`create_step/update_step/delete_step/create_element/...` — no-op:
сообщения пишет агент через `HistoryRecorderMiddleware`, реплей берёт
из того же HistoryService при следующем `get_thread`.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any, ClassVar, cast

import chainlit as cl
from chainlit.context import ChainlitContextException
from chainlit.data.base import BaseDataLayer
from chainlit.element import Element, ElementDict
from chainlit.step import StepDict
from chainlit.types import (
    Feedback,
    PageInfo,
    PaginatedResponse,
    Pagination,
    ThreadDict,
    ThreadFilter,
)
from chainlit.user import PersistedUser
from chainlit.user import User as ClUser

from boba.agent.history import HistoryService
from boba.chainlit.agent.models import (
    StoredUser,
    ThreadId,
    UserId,
)
from boba.chainlit.agent.rendering.replay import replay_history_to_steps_sync
from boba.chainlit.agent.storage import (
    ThreadAlreadyExistsError,
    ThreadNotFoundError,
    ThreadRepository,
    UserCatalog,
)
from boba.workspace.contract import WorkspaceId

__all__ = ["BobaDataLayer"]

logger = logging.getLogger(__name__)


class BobaDataLayer(BaseDataLayer):
    """
    Chainlit-adapter: users + thread-metas из репо, steps из HistoryService
    """

    _WORKSPACE_META_KEY: ClassVar[str] = "workspace_id"

    def __init__(
        self,
        users: UserCatalog,
        threads: ThreadRepository,
        make_history_service: Callable[[WorkspaceId], HistoryService],
    ) -> None:
        self._users = users
        self._threads = threads
        self._make_history_service = make_history_service

    async def get_user(self, identifier: str) -> PersistedUser | None:
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

        steps = await asyncio.to_thread(
            self._replay_steps,
            meta.workspace_id,
            thread_id,
        )

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

    def _replay_steps(
        self,
        workspace_id: WorkspaceId,
        thread_id: ThreadId,
    ) -> list[StepDict]:
        history = self._make_history_service(workspace_id)
        return replay_history_to_steps_sync(history, thread_id)

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
        # chainlit вызывает этот метод и при первом сообщении (когда меты
        # ещё нет), и при последующих апдейтах. Внутри — узкие операции
        # репозитория, чтобы случайно не затереть поля, о которых chainlit
        # не знает (например, system_prompt).
        existing = await self._threads.get_meta(thread_id)
        workspace_id = self._resolve_workspace(metadata)
        if workspace_id is None and existing is not None:
            workspace_id = existing.workspace_id
        if workspace_id is None:
            logger.warning(
                "update_thread(%s) без workspace_id — пропускаем",
                thread_id,
            )
            return

        user_identifier = self._current_user_identifier()
        metadata_patch = dict(metadata) if metadata else {}
        metadata_patch[self._WORKSPACE_META_KEY] = workspace_id

        if existing is None:
            try:
                await self._threads.create(
                    thread_id,
                    workspace_id,
                    user_id,
                    user_identifier,
                    name=name,
                    tags=list(tags) if tags is not None else None,
                    metadata=metadata_patch,
                    system_prompt=None,
                )
                return
            except ThreadAlreadyExistsError:
                # race с _ensure_thread_registered / on_settings_update —
                # мета только что появилась; продолжаем узкими апдейтами.
                pass

        try:
            if name is not None:
                await self._threads.rename(thread_id, name)
            if user_id is not None or user_identifier is not None:
                await self._threads.set_user(thread_id, user_id, user_identifier)
            if tags is not None:
                await self._threads.set_tags(thread_id, list(tags))
            await self._threads.merge_metadata(thread_id, metadata_patch)
        except ThreadNotFoundError:
            logger.warning(
                "update_thread(%s): мета исчезла между get_meta и патчем",
                thread_id,
            )

    async def delete_thread(self, thread_id: ThreadId) -> None:
        meta = await self._threads.get_meta(thread_id)
        await self._threads.delete(thread_id)
        if meta is not None:
            # История чата чистится отдельно: индекс ThreadMeta и
            # history.jsonl физически в разных местах.
            await asyncio.to_thread(self._clear_history, meta.workspace_id)

    def _clear_history(self, workspace_id: WorkspaceId) -> None:
        try:
            self._make_history_service(workspace_id).clear()
        except Exception:
            logger.exception(
                "delete_thread: cannot clear history for ws=%s",
                workspace_id,
            )

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
        metas = await self._threads.list_for_user(UserId(target_user_id))

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

    # --- no-op writers: HistoryService — единственный источник правды ----

    async def create_step(self, step_dict: StepDict) -> None:
        return

    async def update_step(self, step_dict: StepDict) -> None:
        return

    async def delete_step(self, step_id: str) -> None:
        return

    async def create_element(self, element: Element) -> None:
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
        # cl.user_session завязан на websocket-контекст; REST-эндпоинты
        # (rename, share, …) зовут data_layer вне него — chainlit бросает
        # ChainlitContextException, иногда LookupError/RuntimeError.
        try:
            ws = cl.user_session.get(self._WORKSPACE_META_KEY)
        except (ChainlitContextException, LookupError, RuntimeError):
            return None
        if isinstance(ws, str):
            return WorkspaceId(ws)
        return None

    @staticmethod
    def _current_user_identifier() -> str | None:
        try:
            session_user = cl.context.session.user
        except (ChainlitContextException, LookupError, RuntimeError, AttributeError):
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
