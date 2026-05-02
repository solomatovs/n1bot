from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from io import BufferedIOBase, TextIOBase
from typing import Generic, TypeVar

from boba.patterns import Id, Specification, StrId, UuId

TWsId = TypeVar("TWsId", bound=Id)


class WorkspaceId(UuId):
    """Идентификатор user-сессии — value object."""


@dataclass(frozen=True)
class GrepMatch:
    """Одно совпадение при поиске через grep (line — 1-based)."""

    path: str
    line: int
    content: str
    before: tuple[str, ...]
    after: tuple[str, ...]


@dataclass(frozen=True)
class EntryMeta:
    """Метаданные элемента внутри workspace (файл/директория)."""

    path: str
    size: int
    modified: datetime
    kind: str


class WorkspaceError(Exception):
    """Базовая ошибка WorkspaceShell."""

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

    def __init__(self, path: str, encoding: str, cause: UnicodeDecodeError) -> None:
        super().__init__(
            f"cannot decode {path!r} as {encoding!r}: {cause.reason} "
            f"at byte {cause.start}",
            path=path,
        )
        self.encoding = encoding
        self.position = cause.start


class WorkspaceShell(ABC, Generic[TWsId]):
    """Shell-сессия над изолированным workspace'ом (cwd, cd/ls/mkdir/grep/edit_text)."""

    @property
    @abstractmethod
    def workspace_id(self) -> TWsId: ...

    @property
    @abstractmethod
    def cwd(self) -> str:
        """Текущая директория относительно корня workspace."""
        ...

    @abstractmethod
    def cd(self, path: str) -> None:
        """Сменить текущую директорию."""
        ...

    @abstractmethod
    def exists(self, path: str) -> bool: ...

    @abstractmethod
    def delete(self, path: str, *, recursive: bool = False) -> None:
        """Удалить ресурс; директория — только при recursive=True."""
        ...

    @abstractmethod
    def move(self, src: str, dst: str) -> None:
        """Переместить/переименовать ресурс (семантика mv)."""
        ...

    @abstractmethod
    def touch(self, path: str) -> None:
        """Создать пустой файл или обновить mtime."""
        ...

    @abstractmethod
    def copy(self, src: str, dst: str, *, recursive: bool = False) -> None:
        """Скопировать ресурс; директория — только при recursive=True."""
        ...

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
        """Рекурсивный обход всех элементов начиная с пути."""
        ...

    @abstractmethod
    def meta(self, path: str) -> EntryMeta:
        """Метаданные ресурса."""
        ...

    @abstractmethod
    def mkdir(self, path: str) -> None:
        """Создать директорию (с промежуточными)."""
        ...

    @abstractmethod
    def read_lines(
        self, path: str, *, reverse: bool = False, encoding: str = "utf-8"
    ) -> Iterator[str]:
        """Построчное чтение; reverse=True — без загрузки всего файла в память."""
        ...

    @abstractmethod
    def read_text(self, path: str, encoding: str = "utf-8") -> TextIOBase:
        """Открыть ресурс для чтения текста."""
        ...

    @abstractmethod
    def read_binary(self, path: str) -> BufferedIOBase:
        """Открыть ресурс для чтения бинарных данных."""
        ...

    @abstractmethod
    def write_text(self, path: str, encoding: str = "utf-8") -> TextIOBase:
        """Открыть/создать ресурс для записи текста (перезапись)."""
        ...

    @abstractmethod
    def write_binary(self, path: str) -> BufferedIOBase:
        """Открыть/создать ресурс для записи бинарных данных (перезапись)."""
        ...

    @abstractmethod
    def append_text(self, path: str, encoding: str = "utf-8") -> TextIOBase:
        """Открыть/создать ресурс для дозаписи."""
        ...

    @abstractmethod
    def grep(  # noqa: PLR0913 — все параметры — независимые флаги grep'а
        self,
        pattern: str,
        path: str | None = None,
        *,
        recursive: bool = True,
        include: str | None = None,
        case_insensitive: bool = False,
        context: int = 0,
        limit: int = 100,
        fixed_string: bool = False,
        encoding: str = "utf-8",
    ) -> Iterator[GrepMatch]:
        """Grep-подобный поиск по содержимому файлов; бинарные пропускаются."""
        ...

    @abstractmethod
    def edit_text(
        self,
        path: str,
        old: str,
        new: str,
        *,
        replace_all: bool = False,
        encoding: str = "utf-8",
    ) -> int:
        """Find-and-replace правка; без replace_all old должен быть уникален."""
        ...


class WorkspaceRegistry(ABC, Generic[TWsId]):
    """Реестр workspace'ов одного namespace."""

    @abstractmethod
    def create(self) -> WorkspaceShell[TWsId]:
        """Создать новый workspace с автосгенерированным id."""
        ...

    @abstractmethod
    def get(self, workspace_id: TWsId) -> WorkspaceShell[TWsId]:
        """Получить существующий workspace; WorkspaceNotFoundError если нет."""
        ...

    @abstractmethod
    def get_or_create(self, workspace_id: TWsId) -> WorkspaceShell[TWsId]:
        """Вернуть существующий или создать новый по заданному id."""
        ...

    @abstractmethod
    def delete(self, workspace_id: TWsId) -> None:
        """Удалить workspace и все его данные."""
        ...


class ProjectWorkspaceShell(WorkspaceShell[WorkspaceId]):
    """DI-маркер: workspace проекта — код/документы пользователя, доступен tools."""


class HistoryWorkspaceShell(WorkspaceShell[WorkspaceId]):
    """DI-маркер: системный workspace — history, debug-артефакты."""


class ScratchWorkspaceShell(WorkspaceShell[WorkspaceId]):
    """DI-маркер: эфемерный workspace, чистится на выходе из request scope."""


class ProjectWorkspaceRegistry(WorkspaceRegistry[WorkspaceId]):
    """DI-маркер реестра ProjectWorkspaceShell."""


class HistoryWorkspaceRegistry(WorkspaceRegistry[WorkspaceId]):
    """DI-маркер реестра HistoryWorkspaceShell."""


class ScratchWorkspaceRegistry(WorkspaceRegistry[WorkspaceId]):
    """DI-маркер реестра ScratchWorkspaceShell."""


class PromptWorkspaceId(StrId):
    """Строковый id prompt-namespace (application-singleton)."""


class PromptWorkspaceShell(WorkspaceShell[PromptWorkspaceId]):
    """DI-маркер: workspace со статическими *.md/*.txt-промптами."""


class PromptWorkspaceRegistry(WorkspaceRegistry[PromptWorkspaceId]):
    """DI-маркер реестра PromptWorkspaceShell."""
