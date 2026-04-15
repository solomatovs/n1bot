from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from io import BufferedIOBase, TextIOBase
from uuid import UUID
from abc import ABC, abstractmethod

from boba.domain.core.patterns import Id, Storage, Validator


class WorkspaceId(Id[UUID]):
    """Идентификатор workspace'а — value object."""


@dataclass(frozen=True)
class FileMeta:
    """Метаданные файла."""

    path: str
    size: int
    modified: datetime


class PathValidator(Validator[str], ABC):
    """Валидатор ключей (путей) workspace'а. Бросает PermissionError при нарушении."""


class FileStorage(Storage[str, FileMeta], ABC):
    """
    Текстовое файловое хранилище workspace'а. Ключи — плоские пути с '/' как разделитель.
    """

    @property
    @abstractmethod
    def workspace_id(self) -> WorkspaceId: ...

    @abstractmethod
    def open_text(self, path: str, encoding: str = "utf-8") -> TextIOBase: ...

    @abstractmethod
    def open_binary(self, path: str) -> BufferedIOBase: ...


class WorkspaceManager(ABC):
    """Выдаёт WorkspaceService: по UUID — существующий, без UUID — новый."""

    @abstractmethod
    def get_or_create(self, workspace_id: UUID | None = None) -> FileStorage: ...
