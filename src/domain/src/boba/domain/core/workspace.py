from uuid import UUID
from abc import ABC, abstractmethod

import asyncio


class WorkspaceId:
    """Идентификатор workspace'а, который может быть использован для получения самого workspace'а из реестра."""

    def __init__(self, id: UUID) -> None:
        self._id = id
        self._count = 0
        self._lock = asyncio.Lock()

    @property
    def id(self) -> UUID:
        return self._id

    async def acquire(self) -> None:
        """Сервис начинает работу с workspace."""
        async with self._lock:
            self._count += 1

    async def release(self) -> None:
        """Сервис завершает работу с workspace."""
        async with self._lock:
            if self._count <= 0:
                raise RuntimeError("release без acquire")
            self._count -= 1

    @property
    def active(self) -> bool:
        """Есть ли активные сервисы."""
        return self._count > 0


class WorkspaceAwareService(ABC):
    """Сервис, который работает в рамках workspace'а.
    Принимает WorkspaceId, вызывает enter() при старте и close() при завершении.
    Это позволяет отслеживать, какие workspace'ы используются, и предотвращать удаление занятых workspace'ов.
    """

    def __init__(self, workspace: WorkspaceId) -> None:
        self._workspace = workspace

    async def enter(self) -> None:
        """Начать работу — acquire ref count."""
        await self._workspace.acquire()

    async def close(self) -> None:
        """Завершить работу — release ref count."""
        await self._workspace.release()


class WorkspaceBusyError(Exception):
    """Ошибка при попытке удалить workspace, который всё ещё используется."""

    workspace_id: WorkspaceId


class WorkspaceNotFoundError(Exception):
    """Ошибка при попытке получить несуществующий workspace."""

    workspace_id: WorkspaceId


class WorkspaceRegistry(ABC):
    """Управляет жизненным циклом workspace'ов: создание, получение, удаление"""

    @abstractmethod
    def create(self) -> WorkspaceId: ...

    @abstractmethod
    def get(self, id: UUID) -> WorkspaceId: ...

    @abstractmethod
    async def delete(self, workspace: WorkspaceId) -> None:
        """Raises WorkspaceBusyError если workspace.active."""
        ...
