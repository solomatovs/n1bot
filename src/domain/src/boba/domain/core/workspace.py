from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from io import BufferedIOBase, TextIOBase
from typing import Iterator
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


@dataclass(frozen=True)
class FileMeta:
    """Метаданные файла."""

    path: str
    size: int
    modified: datetime


class PathValidator(ABC):
    """Валидатор ключей (путей) workspace'а."""

    @abstractmethod
    def validate(self, path: str) -> str:
        """Валидирует и нормализует путь. Бросает PermissionError при нарушении."""
        ...


class WorkspaceService(ABC):
    """Абстрактное хранилище файлов workspace'а. Пути — плоские ключи с '/' как разделитель."""

    @property
    @abstractmethod
    def workspace_id(self) -> WorkspaceId: ...

    @abstractmethod
    def open_text(self, path: str, mode: str = "r", encoding: str = "utf-8") -> TextIOBase:
        """Открывает текстовый файл."""
        ...

    @abstractmethod
    def open_binary(self, path: str, mode: str = "rb") -> BufferedIOBase:
        """Открывает бинарный файл."""
        ...

    @abstractmethod
    def exists(self, path: str) -> bool: ...

    @abstractmethod
    def delete(self, path: str) -> None: ...

    @abstractmethod
    def rename(self, src: str, dst: str) -> None: ...

    @abstractmethod
    def list(self, prefix: str = "", recursive: bool = False) -> Iterator[str]: ...

    @abstractmethod
    def meta(self, path: str) -> FileMeta: ...


class WorkspaceManager(ABC):
    """Выдаёт WorkspaceService: по UUID — существующий, без UUID — новый."""

    @abstractmethod
    def get_or_create(self, workspace_id: UUID | None = None) -> WorkspaceService: ...
