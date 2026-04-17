from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from io import BufferedIOBase, TextIOBase

from boba.domain.core.patterns import Specification, UuId


class WorkspaceId(UuId):
    """Идентификатор workspace'а — value object."""


@dataclass(frozen=True)
class FileMeta:
    """Метаданные файла."""

    path: str
    size: int
    modified: datetime


class WorkspaceError(Exception):
    """Базовая ошибка ``WorkspaceService``.

    Абстрактна, не привязана к конкретной реализации хранилища. Хранит
    контекст ресурса (``path``). Исходное исключение (если было оборачивание)
    доступно через ``__cause__`` — ``raise WorkspaceError(...) from err``.
    """

    def __init__(self, message: str, *, path: str | None = None) -> None:
        super().__init__(message)
        self.path = path


class WorkspaceNotFoundError(WorkspaceError):
    """Ресурс внутри workspace не найден.

    Абстрактная замена ``FileNotFoundError`` для бэкендов, не основанных на
    файловой системе. Сохраняет путь ресурса.
    """

    def __init__(self, path: str) -> None:
        super().__init__(f"resource not found: {path!r}", path=path)


class WorkspacePermissionError(WorkspaceError):
    """Нет прав на операцию с ресурсом.

    Покрывает как системный ``PermissionError``, так и нарушение границ
    workspace'а (выход за пределы root). Сохраняет путь ресурса и причину.
    """

    def __init__(self, path: str, reason: str | None = None) -> None:
        msg = f"permission denied: {path!r}"
        if reason:
            msg += f" ({reason})"
        super().__init__(msg, path=path)
        self.reason = reason


class WorkspaceDecodingError(WorkspaceError):
    """Невозможно декодировать содержимое ресурса в строку.

    Сохраняет контекст: путь ресурса, запрошенная кодировка, позиция
    ошибочного байта. Исходный ``UnicodeDecodeError`` — через ``__cause__``.
    """

    def __init__(
        self, path: str, encoding: str, cause: UnicodeDecodeError
    ) -> None:
        super().__init__(
            f"cannot decode {path!r} as {encoding!r}: {cause.reason} "
            f"at byte {cause.start}",
            path=path,
        )
        self.encoding = encoding
        self.position = cause.start


class WorkspaceService(ABC):
    """
    Текстовое файловое хранилище workspace'а. Ключи — плоские пути с '/' как разделитель
    """

    @property
    @abstractmethod
    def workspace_id(self) -> WorkspaceId: ...

    @abstractmethod
    def exists(self, path: str) -> bool: ...

    @abstractmethod
    def delete(self, path: str) -> None:
        """Удалить ресурс.

        Raises:
            WorkspaceNotFoundError: если ресурс не найден.
            WorkspacePermissionError: если нет прав / путь вне workspace.
            WorkspaceError: при прочих ошибках удаления.
        """
        ...

    @abstractmethod
    def ls(
        self, path: str | None = None, spec: Specification[str] | None = None
    ) -> Iterator[str]:
        """Список элементов в указанном пути (без вложенности).

        Raises:
            WorkspacePermissionError: если нет прав / путь вне workspace.
            WorkspaceError: при прочих ошибках обхода.
        """
        ...

    @abstractmethod
    def tree(
        self, path: str | None = None, spec: Specification[str] | None = None
    ) -> Iterator[str]:
        """Рекурсивный обход всех элементов начиная с указанного пути.

        Raises:
            WorkspacePermissionError: если нет прав / путь вне workspace.
            WorkspaceError: при прочих ошибках обхода.
        """
        ...

    @abstractmethod
    def meta(self, path: str) -> FileMeta:
        """Метаданные ресурса.

        Raises:
            WorkspaceNotFoundError: если ресурс не найден.
            WorkspacePermissionError: если нет прав / путь вне workspace.
            WorkspaceError: при прочих ошибках.
        """
        ...

    @abstractmethod
    def mkdir(self, path: str) -> None:
        """Создать директорию. Создаёт промежуточные директории при необходимости.

        Raises:
            WorkspacePermissionError: если нет прав / путь вне workspace.
            WorkspaceError: при прочих ошибках.
        """
        ...

    @abstractmethod
    def read_lines(
        self, path: str, *, reverse: bool = False, encoding: str = "utf-8"
    ) -> Iterator[str]:
        """
        Построчное чтение файла.
        reverse=True — строки от последней к первой, без загрузки всего файла в память.

        Raises:
            WorkspaceNotFoundError: если ресурс не найден.
            WorkspacePermissionError: если нет прав / путь вне workspace.
            WorkspaceDecodingError: если содержимое не декодируется в указанной
                кодировке.
            WorkspaceError: при прочих ошибках чтения ресурса; исходное
                низкоуровневое исключение — в ``__cause__``.
        """
        ...

    @abstractmethod
    def read_text(self, path: str, encoding: str = "utf-8") -> TextIOBase:
        """Открыть ресурс для чтения текста.

        Raises:
            WorkspaceNotFoundError: если ресурс не найден.
            WorkspacePermissionError: если нет прав / путь вне workspace.
            WorkspaceError: при прочих ошибках открытия.
        """
        ...

    @abstractmethod
    def read_binary(self, path: str) -> BufferedIOBase:
        """Открыть ресурс для чтения бинарных данных.

        Raises:
            WorkspaceNotFoundError: если ресурс не найден.
            WorkspacePermissionError: если нет прав / путь вне workspace.
            WorkspaceError: при прочих ошибках открытия.
        """
        ...

    @abstractmethod
    def write_text(self, path: str, encoding: str = "utf-8") -> TextIOBase:
        """Открыть/создать ресурс для записи (перезапись).

        Создаёт родительские директории.

        Raises:
            WorkspacePermissionError: если нет прав / путь вне workspace.
            WorkspaceError: при прочих ошибках открытия/создания.
        """
        ...

    @abstractmethod
    def append_text(self, path: str, encoding: str = "utf-8") -> TextIOBase:
        """Открыть/создать ресурс для дозаписи.

        Создаёт родительские директории.

        Raises:
            WorkspacePermissionError: если нет прав / путь вне workspace.
            WorkspaceError: при прочих ошибках открытия/создания.
        """
        ...


class WorkspaceManager(ABC):
    """Управляет жизненным циклом workspace'ов."""

    @abstractmethod
    def create(self) -> WorkspaceService:
        """Создать новый workspace."""
        ...

    @abstractmethod
    def get(self, workspace_id: WorkspaceId) -> WorkspaceService:
        """
        Получить существующий workspace.
        Бросает FileNotFoundError, если не найден."""
        ...

    @abstractmethod
    def delete(self, workspace_id: WorkspaceId) -> None:
        """Удалить workspace и все его данные."""
        ...
