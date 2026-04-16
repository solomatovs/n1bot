from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from io import BufferedIOBase, TextIOBase
from uuid import UUID

from boba.domain.core.patterns import Id, Specification, Validator


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


class WorkspaceService(ABC):
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
    def read_text(self, path: str, encoding: str = "utf-8") -> TextIOBase:
        """Открыть файл для чтения текста. Бросает FileNotFoundError, если файл не существует."""
        ...

    @abstractmethod
    def read_binary(self, path: str) -> BufferedIOBase:
        """Открыть файл для чтения бинарных данных. Бросает FileNotFoundError, если файл не существует."""
        ...

    @abstractmethod
    def write_text(self, path: str, encoding: str = "utf-8") -> TextIOBase:
        """Открыть/создать файл для записи (перезапись). Создаёт родительские директории."""
        ...

    @abstractmethod
    def append_text(self, path: str, encoding: str = "utf-8") -> TextIOBase:
        """Открыть/создать файл для дозаписи. Создаёт родительские директории."""
        ...


class WorkspaceManager(ABC):
    """Управляет жизненным циклом workspace'ов."""

    @abstractmethod
    def create(self) -> WorkspaceService:
        """Создать новый workspace."""
        ...

    @abstractmethod
    def get(self, workspace_id: WorkspaceId) -> WorkspaceService:
        """Получить существующий workspace. Бросает FileNotFoundError, если не найден."""
        ...

    @abstractmethod
    def delete(self, workspace_id: WorkspaceId) -> None:
        """Удалить workspace и все его данные."""
        ...
