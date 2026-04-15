from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from io import BufferedIOBase, TextIOBase
from uuid import UUID
from abc import ABC, abstractmethod
from typing import Iterator

from boba.domain.core.patterns import Id, Validator, Specification


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


class FileStorage(ABC):
    """
    Текстовое файловое хранилище workspace'а. Ключи — плоские пути с '/' как разделитель.
    """

    @property
    @abstractmethod
    def workspace_id(self) -> WorkspaceId: ...

    @abstractmethod
    def exists(self, path: str) -> bool: ...

    @abstractmethod
    def delete(self, path: str) -> None: ...

    @abstractmethod
    def ls(
        self, path: str | None = None, spec: Specification[str] | None = None
    ) -> Iterator[str]:
        """Список элементов в указанном пути (без вложенности)."""
        ...

    @abstractmethod
    def tree(
        self, path: str | None = None, spec: Specification[str] | None = None
    ) -> Iterator[str]:
        """Рекурсивный обход всех элементов начиная с указанного пути."""
        ...

    @abstractmethod
    def meta(self, path: str) -> FileMeta:
        """Метаданные файла."""
        ...

    @abstractmethod
    def mkdir(self, path: str) -> None:
        """Создать директорию. Создаёт промежуточные директории при необходимости."""
        ...

    @abstractmethod
    def open_text(self, path: str, encoding: str = "utf-8") -> TextIOBase:
        """Открыть файл для чтения текста. Бросает FileNotFoundError, если файл не существует."""
        ...

    @abstractmethod
    def open_binary(self, path: str) -> BufferedIOBase:
        """Открыть файл для чтения бинарных данных. Бросает FileNotFoundError, если файл не существует."""
        ...


class WorkspaceManager(ABC):
    """Выдаёт WorkspaceService: по UUID — существующий, без UUID — новый."""

    @abstractmethod
    def get_or_create(self, workspace_id: UUID | None = None) -> FileStorage: ...
