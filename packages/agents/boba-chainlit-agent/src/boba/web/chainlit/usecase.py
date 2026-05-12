"""Application-слой: use cases оркестрации ChatSession + LRU-кеш сессий."""

from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass

from boba.web.chainlit.auth import User
from boba.web.chainlit.session import ChatSession
from boba.workspace.contract import WorkspaceId

__all__ = ["ChatSessionPool", "OpenChatSession"]

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _SessionKey:
    user_id: str
    workspace_id: WorkspaceId


class ChatSessionPool:
    """LRU-кеш ChatSession по (user, workspace) с локом против двойного build."""

    def __init__(
        self,
        build: Callable[[WorkspaceId], ChatSession],
        capacity: int = 32,
    ) -> None:
        if capacity < 1:
            msg = f"ChatSessionPool: capacity must be >= 1, got {capacity}"
            raise ValueError(msg)
        self._build = build
        self._capacity = capacity
        self._sessions: OrderedDict[_SessionKey, ChatSession] = OrderedDict()
        self._locks: dict[_SessionKey, asyncio.Lock] = {}
        self._registry_lock = asyncio.Lock()

    async def get_or_create(
        self,
        user: User,
        workspace_id: WorkspaceId,
    ) -> ChatSession:
        key = _SessionKey(user.username, workspace_id)
        async with self._registry_lock:
            lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            cached = self._sessions.get(key)
            if cached is not None:
                async with self._registry_lock:
                    self._sessions.move_to_end(key)
                return cached
            session = await asyncio.to_thread(self._build, key.workspace_id)
            await self._insert(key, session)
            return session

    async def drop(self, user: User, workspace_id: WorkspaceId) -> None:
        key = _SessionKey(user.username, workspace_id)
        async with self._registry_lock:
            self._sessions.pop(key, None)
            # Если lock сейчас залочен — оставляем владельцу; иначе освобождаем
            # слот, чтобы _locks не рос линейно вместе с числом уникальных key.
            existing_lock = self._locks.get(key)
            if existing_lock is not None and not existing_lock.locked():
                self._locks.pop(key, None)

    async def _insert(self, key: _SessionKey, session: ChatSession) -> None:
        async with self._registry_lock:
            self._sessions[key] = session
            self._sessions.move_to_end(key)
            while len(self._sessions) > self._capacity:
                evicted_key, _ = self._sessions.popitem(last=False)
                evicted_lock = self._locks.get(evicted_key)
                if evicted_lock is not None and not evicted_lock.locked():
                    self._locks.pop(evicted_key, None)
                logger.info("ChatSessionPool evicted LRU: %s", evicted_key)


class OpenChatSession:
    """Use case: получить ChatSession для (user, workspace), создавая лениво."""

    def __init__(self, pool: ChatSessionPool) -> None:
        self._pool = pool

    async def execute(
        self,
        user: User,
        workspace_id: WorkspaceId,
    ) -> ChatSession:
        return await self._pool.get_or_create(user, workspace_id)
