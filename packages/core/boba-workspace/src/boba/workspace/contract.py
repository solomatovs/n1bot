"""WorkspaceShell: изолированная shell-сессия над файловым namespace'ом.

ProjectWorkspaceShell — единственный тонкий маркер: DI-токен, по которому
file-tools авто-резолвят свой workspace. Остальные потребители (history,
storage, observers) принимают WorkspaceShell напрямую и конструируют
FsWorkspaceShell на месте из пути — без DI и без маркеров.
"""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from io import BufferedIOBase, TextIOBase
from typing import Generic, Literal, NewType, Protocol, TypeVar

from boba.patterns import Specification

__all__ = [
    "BinaryReadable",
    "DirectoryEntry",
    "EntryMeta",
    "FileEntry",
    "GrepMatch",
    "LsEntry",
    "OtherEntry",
    "ProjectWorkspaceShell",
    "TextReadable",
    "WorkspaceDecodingError",
    "WorkspaceError",
    "WorkspaceId",
    "WorkspaceNotFoundError",
    "WorkspacePermissionError",
    "WorkspaceShell",
    "new_workspace_id",
]


class BinaryReadable(Protocol):
    """Минимальный read-only handle: только read(n) -> bytes.

    Структурно совместим с io.BufferedIOBase, io.BytesIO,
    open(path, 'rb'), streaming-response-обёртками поверх HTTP.
    """

    def read(self, n: int = -1, /) -> bytes: ...


class TextReadable(Protocol):
    """Минимальный read-only handle: только read(n) -> str.

    Структурно совместим с io.TextIOBase, io.StringIO,
    open(path, 'r', encoding=...).
    """

    def read(self, n: int = -1, /) -> str: ...

TWsId = TypeVar("TWsId")


WorkspaceId = NewType("WorkspaceId", str)
"""Идентификатор user-сессии."""


def new_workspace_id() -> WorkspaceId:
    """Свежий WorkspaceId."""
    return WorkspaceId(str(uuid.uuid4()))


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


@dataclass(frozen=True)
class FileEntry:
    """Элемент ls/tree — обычный файл. Дискриминатор: kind == "file"."""

    path: str
    kind: Literal["file"] = "file"


@dataclass(frozen=True)
class DirectoryEntry:
    """Элемент ls/tree — директория. Дискриминатор: kind == "directory"."""

    path: str
    kind: Literal["directory"] = "directory"


@dataclass(frozen=True)
class OtherEntry:
    """Элемент ls/tree — спец-файл (symlink/socket/fifo). kind == "other"."""

    path: str
    kind: Literal["other"] = "other"


LsEntry = FileEntry | DirectoryEntry | OtherEntry
"""Discriminated union элементов ls/tree по полю kind."""


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
    ) -> Iterator[LsEntry]:
        """Список элементов в указанном пути (без вложенности).

        Включает файлы и директории; дискриминатор — поле kind.
        """
        ...

    @abstractmethod
    def tree(
        self, path: str | None = None, spec: Specification[str] | None = None
    ) -> Iterator[LsEntry]:
        """Рекурсивный обход всех элементов начиная с пути.

        Включает файлы и директории; дискриминатор — поле kind.
        """
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
    def atomic_write_text(
        self, path: str, source: TextReadable, encoding: str = "utf-8",
    ) -> None:
        """Атомарная перезапись текста: stream -> tmp -> fsync -> os.replace.

        source — любой объект с .read(n) -> str (io.StringIO,
        open(path, 'r')). Tmp создаётся в той же директории, что и
        target — обходит лимиты /tmp и гарантирует atomic rename
        в пределах одной FS. Использовать для критичных wire-моделей
        (JSON-индексы, manifest'ы, конфиги).
        """
        ...

    @abstractmethod
    def atomic_write_binary(self, path: str, source: BinaryReadable) -> None:
        """Атомарная перезапись бинарных: stream -> tmp -> fsync -> os.replace.

        source — любой объект с .read(n) -> bytes (io.BytesIO,
        open(path, 'rb'), streaming-handle). Содержимое не загружается
        в память целиком — shutil.copyfileobj копирует чанками.
        """
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


class ProjectWorkspaceShell(WorkspaceShell[WorkspaceId]):
    """DI-маркер: workspace проекта — код/документы пользователя, доступен tools.

    Единственный оставшийся маркер: служит DI-токеном, по которому file-tools
    авто-резолвят свой workspace. Все прочие виды (history, prompt, scratch) —
    это просто FsWorkspaceShell, сконструированный из нужного пути на месте.
    """
