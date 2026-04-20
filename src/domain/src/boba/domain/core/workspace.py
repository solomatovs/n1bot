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
class GrepMatch:
    """Одно совпадение при поиске через :meth:`WorkspaceService.grep`.

    ``path`` — относительный путь файла от корня workspace. ``line`` — 1-based
    номер строки с совпадением, ``content`` — сама строка (без trailing
    newline). ``before``/``after`` — строки контекста (могут быть пустыми).
    """

    path: str
    line: int
    content: str
    before: tuple[str, ...]
    after: tuple[str, ...]


@dataclass(frozen=True)
class FileMeta:
    """Метаданные ресурса workspace.

    ``path`` — относительный путь от корня workspace (безопасно показывать
    пользователю). ``kind`` — ``"file"`` / ``"directory"`` / ``"other"``.
    """

    path: str
    size: int
    modified: datetime
    kind: str


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

    @property
    @abstractmethod
    def cwd(self) -> str:
        """Текущая директория относительно корня workspace.

        Формат: ``/`` для корня, ``/docs/api`` для вложенной. Все методы
        сервиса разрешают относительные пути от ``cwd``; абсолютные (с
        ведущим ``/``) — от корня workspace.
        """
        ...

    @abstractmethod
    def cd(self, path: str) -> None:
        """Сменить текущую директорию.

        Путь нормализуется теми же правилами, что и в остальных методах:
        абсолютный — от корня workspace, относительный — от текущей
        ``cwd``. После успеха последующие обращения по относительным
        путям разрешаются уже от новой ``cwd``.

        Raises:
            WorkspaceNotFoundError: если путь не существует.
            WorkspacePermissionError: если нет прав.
            WorkspaceError: если путь существует, но не директория, или
                при прочих I/O-ошибках.
        """
        ...

    @abstractmethod
    def exists(self, path: str) -> bool: ...

    @abstractmethod
    def delete(self, path: str, *, recursive: bool = False) -> None:
        """Удалить ресурс.

        Файл удаляется всегда. Директория удаляется только при
        ``recursive=True`` (со всем содержимым) — без флага на
        непустой директории возвращается ошибка (аналог ``rm`` vs ``rm -r``).

        Raises:
            WorkspaceNotFoundError: если ресурс не найден.
            WorkspacePermissionError: если нет прав.
            WorkspaceError: если это непустая директория без ``recursive``,
                или при прочих ошибках удаления.
        """
        ...

    @abstractmethod
    def move(self, src: str, dst: str) -> None:
        """Переместить/переименовать ресурс.

        Семантика ``mv``: если ``dst`` — существующая директория, ``src``
        переносится внутрь с тем же именем; иначе ``src`` переименовывается
        в ``dst``. Существующий файл по пути ``dst`` перезаписывается.
        Промежуточные директории не создаются — родитель ``dst`` должен
        существовать.

        Raises:
            WorkspaceNotFoundError: если ``src`` не существует.
            WorkspacePermissionError: если нет прав.
            WorkspaceError: при прочих ошибках перемещения.
        """
        ...

    @abstractmethod
    def touch(self, path: str) -> None:
        """Создать пустой файл или обновить mtime существующего ресурса.

        Если ``path`` не существует — создаётся пустой файл (промежуточные
        директории создаются автоматически). Если существует — обновляется
        время модификации (работает и для файла, и для директории).

        Raises:
            WorkspacePermissionError: если нет прав.
            WorkspaceError: при прочих I/O-ошибках.
        """
        ...

    @abstractmethod
    def copy(self, src: str, dst: str, *, recursive: bool = False) -> None:
        """Скопировать ресурс.

        Для файла — байтовое копирование. Для директории требуется
        ``recursive=True`` (аналог ``cp`` vs ``cp -r``). Если ``dst`` —
        существующая директория, копия кладётся внутрь с именем ``src``;
        иначе копируется прямо в ``dst``. Существующий файл по ``dst``
        перезаписывается.

        Raises:
            WorkspaceNotFoundError: если ``src`` не существует.
            WorkspacePermissionError: если нет прав.
            WorkspaceError: если ``src`` — директория без ``recursive``,
                или при прочих I/O-ошибках.
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
        """Поиск по содержимому файлов (grep-подобный).

        ``pattern`` — regex (Python-синтаксис); при ``fixed_string=True``
        трактуется как литеральная строка. ``path`` — старт поиска; если
        файл — ищем только в нём, если директория — обход с учётом
        ``recursive``. ``include`` — fnmatch-glob по относительному пути
        (``"*.py"`` и т.п.). ``context`` — строк до/после каждого
        совпадения. ``limit`` — потолок количества совпадений.

        Бинарные файлы (по null-byte в первых 8 KB) и файлы, не
        декодирующиеся в ``encoding``, пропускаются молча.

        Raises:
            WorkspaceNotFoundError: если ``path`` не существует.
            WorkspacePermissionError: нет прав.
            WorkspaceError: невалидный regex или прочие I/O-ошибки.
        """
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
        """Find-and-replace редактирование текстового файла.

        Подменяет ``old`` на ``new`` в содержимом файла. Если
        ``replace_all=False`` (по умолчанию), ``old`` должен встречаться
        ровно один раз — иначе ``WorkspaceError`` с явным сообщением
        (LLM должна перечитать файл и уточнить контекст). Если
        ``replace_all=True``, заменяются все вхождения. ``old`` не должна
        быть пустой. Возвращает число выполненных замен.

        Raises:
            WorkspaceNotFoundError: файла не существует.
            WorkspacePermissionError: нет прав.
            WorkspaceDecodingError: файл не декодируется в ``encoding``.
            WorkspaceError: ``old`` не найден / неуникален без
                ``replace_all`` / прочие I/O-ошибки.
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
