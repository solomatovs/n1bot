from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from io import BufferedIOBase, TextIOBase
from typing import Generic, TypeVar

from boba.domain.core.patterns import Id, Specification, StrId, UuId

TWsId = TypeVar("TWsId", bound=Id)


class WorkspaceId(UuId):
    """Идентификатор user-сессии — value object.

    Шарится между namespace'ами одной сессии (project/history/scratch):
    один и тот же `WorkspaceId` адресует три параллельных хранилища
    через разные `WorkspaceRegistry`. Plugin-namespace использует
    отдельный Id-тип (см. подклассы :class:`Id` в plugin-инфраструктуре).
    """


@dataclass(frozen=True)
class GrepMatch:
    """Одно совпадение при поиске через :meth:`WorkspaceShell.grep`.

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
class EntryMeta:
    """Метаданные элемента внутри workspace (файл или директория).

    ``path`` — относительный путь от корня workspace (безопасно показывать
    пользователю). ``kind`` — ``"file"`` / ``"directory"`` / ``"other"``.
    """

    path: str
    size: int
    modified: datetime
    kind: str


class WorkspaceError(Exception):
    """Базовая ошибка ``WorkspaceShell``.

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

    def __init__(self, path: str, encoding: str, cause: UnicodeDecodeError) -> None:
        super().__init__(
            f"cannot decode {path!r} as {encoding!r}: {cause.reason} "
            f"at byte {cause.start}",
            path=path,
        )
        self.encoding = encoding
        self.position = cause.start


class WorkspaceShell(ABC, Generic[TWsId]):
    """Shell-сессия над изолированным workspace'ом.

    Hold'ит состояние сессии (``cwd``) и предоставляет shell-подобный API
    над ограниченной директорией: ``cd``, ``ls``, ``mkdir``, ``touch``,
    ``cp``, ``mv``, ``rm``, ``grep``, чтение/запись файлов, find-and-replace
    правки. Все пути нормализуются относительно корня workspace: абсолютный
    ``/foo`` и относительный ``foo`` одинаково адресуют ``root/foo``; ``..``
    не выводит выше корня. Ошибки и логирование дают пользователю путь
    относительно workspace, а не реальный путь на диске.

    Один shell владеет одной директорией. Все остальные компоненты
    должны делить один экземпляр — в будущем сюда попадёт локирование/
    конкурентный доступ, которое требует единого владельца ресурса.
    Дискриминация «какой workspace» делается в DI через маркерные
    подклассы (:class:`ProjectWorkspaceShell` и т.п.), а не runtime-
    полем. Тип идентификатора фиксируется generic-параметром ``TWsId``
    в маркерном подклассе: user-namespace'ы (project/history/scratch)
    используют :class:`WorkspaceId` (UUID), plugin-namespace —
    собственный строковый Id.
    """

    @property
    @abstractmethod
    def workspace_id(self) -> TWsId: ...

    @property
    @abstractmethod
    def cwd(self) -> str:
        """Текущая директория относительно корня workspace.

        Формат: ``/`` для корня, ``/docs/api`` для вложенной. Все методы
        shell'а разрешают относительные пути от ``cwd``; абсолютные (с
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
    def meta(self, path: str) -> EntryMeta:
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
    def write_binary(self, path: str) -> BufferedIOBase:
        """Открыть/создать ресурс для записи бинарных данных (перезапись).

        Создаёт родительские директории. Нужен для файлов, которые не
        раскладываются в текст по умолчанию — загрузки пользователя
        (PDF, архивы, картинки), дампы, кэш-артефакты.

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


class WorkspaceRegistry(ABC, Generic[TWsId]):
    """Реестр workspace'ов одного namespace.

    Реестр фиксирует ровно один namespace: реализация знает свою
    директорию, а ключ в DI — маркерный подкласс (например,
    :class:`ProjectWorkspaceRegistry`). Разделять namespace'ы через параметр
    метода намеренно не стали — иначе пришлось бы тянуть дискриминатор
    в сигнатуры tools и сервисов. Тип идентификатора (``TWsId``)
    фиксируется маркерным подклассом и должен совпадать с типом,
    параметризующим соответствующий :class:`WorkspaceShell`.
    """

    @abstractmethod
    def create(self) -> WorkspaceShell[TWsId]:
        """Создать новый workspace с автосгенерированным id."""
        ...

    @abstractmethod
    def get(self, workspace_id: TWsId) -> WorkspaceShell[TWsId]:
        """Получить существующий workspace.

        Raises:
            WorkspaceNotFoundError: если workspace не существует.
        """
        ...

    @abstractmethod
    def get_or_create(self, workspace_id: TWsId) -> WorkspaceShell[TWsId]:
        """Вернуть существующий workspace или создать новый по заданному id.

        Используется для разделения одного id между несколькими реестрами
        разных namespace'ов — каждый создаёт свой namespace под тем же id
        при первом обращении.
        """
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
    """DI-маркер реестра :class:`ProjectWorkspaceShell`."""


class HistoryWorkspaceRegistry(WorkspaceRegistry[WorkspaceId]):
    """DI-маркер реестра :class:`HistoryWorkspaceShell`."""


class ScratchWorkspaceRegistry(WorkspaceRegistry[WorkspaceId]):
    """DI-маркер реестра :class:`ScratchWorkspaceShell`."""


class ExtensionWorkspaceId(StrId):
    """Строковый id extension-namespace.

    В отличие от :class:`WorkspaceId` (UUID, генерится для каждой
    user-сессии), extension workspace — application-singleton, и id у него
    единственный и стабильный. :class:`StrId` даёт читаемое имя
    (``"extensions"``), без UUID-балласта в путях и логах.
    """


class ExtensionWorkspaceShell(WorkspaceShell[ExtensionWorkspaceId]):
    """DI-маркер: workspace c расширениями (``*.py``-плагинами и
    ``*.md``/``*.txt``-промптами) — application-singleton.

    Используется :class:`~boba.infra.extensions.ExtensionLoader` для
    discovery (``tree``/``read_text``); сами расширения могут читать
    соседние файлы через ``ctx.extension_workspace`` (data-файлы,
    шаблоны, кэш).
    """


class ExtensionWorkspaceRegistry(WorkspaceRegistry[ExtensionWorkspaceId]):
    """DI-маркер реестра :class:`ExtensionWorkspaceShell`."""
