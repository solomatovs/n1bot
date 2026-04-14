from __future__ import annotations

from uuid import UUID
from abc import ABC, abstractmethod


class WorkspaceId:
    """Идентификатор workspace'а — value object."""

    def __init__(self, name: UUID) -> None:
        self._name = name

    @property
    def name(self) -> UUID:
        return self._name

    def __eq__(self, other: object) -> bool:
        return isinstance(other, WorkspaceId) and self._name == other._name

    def __hash__(self) -> int:
        return hash(self._name)

    def __repr__(self) -> str:
        return f"WorkspaceId({self._name!r})"


class WorkspaceService(ABC):
    """Сервис для работы с файлами внутри workspace'а."""

    @property
    @abstractmethod
    def workspace_id(self) -> WorkspaceId: ...


class WorkspaceManager(ABC):
    """Выдаёт WorkspaceService: по UUID — существующий, без UUID — новый."""

    @abstractmethod
    def get_or_create(self, workspace_id: UUID | None = None) -> WorkspaceService: ...
