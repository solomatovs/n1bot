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
    контекст ресурса (``path``) в виде, безопасном для показа пользователю —
    относительно корня workspace, реальный путь на диске наружу не
    утекает. Исходное исключение (если было оборачивание) доступно через
    ``__cause__``.
    """

    def __init__(self, message: str, *, path: str | None = None) -> None:
        super().__init__(message)
        self.path = path


class WorkspaceNotFoundError(WorkspaceError):
    """Ресурс внутри workspace не найден."""

    def __init__(self, path: str) -> None:
        super().__init__(f"resource not found: {path!r}", path=path)


class WorkspacePermissionError(WorkspaceError):
    """Нет прав на операцию с ресурсом."""

    def __init__(self, path: str, reason: str | None = None) -> None:
        msg = f"permission denied: {path!r}"
        if reason:
            msg += f" ({reason})"
        super().__init__(msg, path=path)
        self.reason = reason


class WorkspaceDecodingError(WorkspaceError):
    """Невозможно декодировать содержимое ресурса в строку."""

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
    """Единственный сервис работы с файлами workspace'а.

    Все пути, переданные извне, нормализуются относительно корня
    workspace: абсолютный ``/foo`` и относительный ``foo`` одинаково
    адресуют ``root/foo``; ``..`` не выводит выше корня. Ошибки и
    логирование дают пользователю путь относительно workspace, а не
    реальный путь на диске.

    Один сервис владеет одной директорией. Все остальные компоненты
    должны делить один экземпляр — в будущем сюда попадёт локирование/
    конкурентный доступ, которое требует единого владельца ресурса.
    Дискриминация «какой workspace» делается в DI через маркерные
    подклассы (:class:`UserWorkspaceService` и т.п.), а не runtime-
    полем.
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
            WorkspacePermissionError: если нет прав.
            WorkspaceError: при прочих ошибках удаления.
        """
        ...

    @abstractmethod
    def ls(
        self, path: str | None = None, spec: Specification[str] | None = None
    ) -> Iterator[str]:
        """Список элементов в указанном пути (без вложенности).

        Raises:
            WorkspacePermissionError: если нет прав.
            WorkspaceError: при прочих ошибках обхода.
        """
        ...

    @abstractmethod
    def tree(
        self, path: str | None = None, spec: Specification[str] | None = None
    ) -> Iterator[str]:
        """Рекурсивный обход всех элементов начиная с указанного пути.

        Raises:
            WorkspacePermissionError: если нет прав.
            WorkspaceError: при прочих ошибках обхода.
        """
        ...

    @abstractmethod
    def meta(self, path: str) -> FileMeta:
        """Метаданные ресурса.

        Raises:
            WorkspaceNotFoundError: если ресурс не найден.
            WorkspacePermissionError: если нет прав.
            WorkspaceError: при прочих ошибках.
        """
        ...

    @abstractmethod
    def mkdir(self, path: str) -> None:
        """Создать директорию. Создаёт промежуточные директории при необходимости.

        Raises:
            WorkspacePermissionError: если нет прав.
            WorkspaceError: при прочих ошибках.
        """
        ...

    @abstractmethod
    def read_lines(
        self, path: str, *, reverse: bool = False, encoding: str = "utf-8"
    ) -> Iterator[str]:
        """Построчное чтение файла.

        ``reverse=True`` — строки от последней к первой, без загрузки
        всего файла в память.

        Raises:
            WorkspaceNotFoundError: если ресурс не найден.
            WorkspacePermissionError: если нет прав.
            WorkspaceDecodingError: если содержимое не декодируется в
                указанной кодировке.
            WorkspaceError: при прочих ошибках чтения ресурса; исходное
                низкоуровневое исключение — в ``__cause__``.
        """
        ...

    @abstractmethod
    def read_text(self, path: str, encoding: str = "utf-8") -> TextIOBase:
        """Открыть ресурс для чтения текста.

        Raises:
            WorkspaceNotFoundError: если ресурс не найден.
            WorkspacePermissionError: если нет прав.
            WorkspaceError: при прочих ошибках открытия.
        """
        ...

    @abstractmethod
    def read_binary(self, path: str) -> BufferedIOBase:
        """Открыть ресурс для чтения бинарных данных.

        Raises:
            WorkspaceNotFoundError: если ресурс не найден.
            WorkspacePermissionError: если нет прав.
            WorkspaceError: при прочих ошибках открытия.
        """
        ...

    @abstractmethod
    def write_text(self, path: str, encoding: str = "utf-8") -> TextIOBase:
        """Открыть/создать ресурс для записи (перезапись).

        Создаёт родительские директории.

        Raises:
            WorkspacePermissionError: если нет прав.
            WorkspaceError: при прочих ошибках открытия/создания.
        """
        ...

    @abstractmethod
    def append_text(self, path: str, encoding: str = "utf-8") -> TextIOBase:
        """Открыть/создать ресурс для дозаписи.

        Создаёт родительские директории.

        Raises:
            WorkspacePermissionError: если нет прав.
            WorkspaceError: при прочих ошибках открытия/создания.
        """
        ...


class WorkspaceManager(ABC):
    """Управляет жизненным циклом workspace'ов одного namespace.

    Менеджер фиксирует ровно один namespace: реализация знает свою
    директорию, а сервис-ключ в DI — маркерный подкласс (например,
    :class:`UserWorkspaceManager`). Разделять namespace'ы через параметр
    метода намеренно не стали — иначе пришлось бы тянуть дискриминатор
    в сигнатуры tools и сервисов.
    """

    @abstractmethod
    def create(self) -> WorkspaceService:
        """Создать новый workspace с автосгенерированным ``WorkspaceId``."""
        ...

    @abstractmethod
    def get(self, workspace_id: WorkspaceId) -> WorkspaceService:
        """Получить существующий workspace.

        Raises:
            WorkspaceNotFoundError: если workspace не существует.
        """
        ...

    @abstractmethod
    def get_or_create(self, workspace_id: WorkspaceId) -> WorkspaceService:
        """Вернуть существующий workspace или создать новый по заданному id.

        Используется для разделения одного :class:`WorkspaceId` между
        несколькими менеджерами разных namespace'ов — каждый создаёт
        свой namespace под тем же id при первом обращении.
        """
        ...

    @abstractmethod
    def delete(self, workspace_id: WorkspaceId) -> None:
        """Удалить workspace и все его данные."""
        ...


class UserWorkspaceService(WorkspaceService):
    """DI-маркер: пользовательский workspace, доступный tools."""


class SystemWorkspaceService(WorkspaceService):
    """DI-маркер: системный workspace — history, debug-артефакты."""


class TmpWorkspaceService(WorkspaceService):
    """DI-маркер: временный workspace, чистится на выходе из request scope."""


class UserWorkspaceManager(WorkspaceManager):
    """DI-маркер менеджера :class:`UserWorkspaceService`."""


class SystemWorkspaceManager(WorkspaceManager):
    """DI-маркер менеджера :class:`SystemWorkspaceService`."""


class TmpWorkspaceManager(WorkspaceManager):
    """DI-маркер менеджера :class:`TmpWorkspaceService`."""
