from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from datetime import datetime
from io import BufferedIOBase, TextIOBase
from typing import Self

from boba.domain.core.patterns import Id, Resolver, Specification, UuId


class WorkspaceId(UuId):
    """Идентификатор workspace'а — value object."""


class WorkspaceKind(Id[str]):
    """Namespace workspace'а. Инкапсулирует имя подкаталога / namespace-ключа.

    Строковое представление всегда равно ``name``. Единый источник истины
    для путей/ключей — сам экземпляр ``WorkspaceKind``, хардкод строк в
    адаптерах и DI-проводке не допускается.
    """

    def to_wire(self) -> str:
        return self._name

    @classmethod
    def from_wire(cls, value: str) -> Self:
        return cls(value)


USER_WORKSPACE_KIND = WorkspaceKind("user")
"""Пользовательский workspace — доступен tools."""

SYSTEM_WORKSPACE_KIND = WorkspaceKind("system")
"""Системный workspace — history, debug-артефакты, tools его не видят."""

TMP_WORKSPACE_KIND = WorkspaceKind("tmp")
"""Временный workspace — чистится на выходе из request scope."""


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
    """
    Ресурс внутри workspace не найден.
    """

    def __init__(self, path: str) -> None:
        super().__init__(f"resource not found: {path!r}", path=path)


class WorkspacePermissionError(WorkspaceError):
    """
    Нет прав на операцию с ресурсом.
    """

    def __init__(self, path: str, reason: str | None = None) -> None:
        msg = f"permission denied: {path!r}"
        if reason:
            msg += f" ({reason})"
        super().__init__(msg, path=path)
        self.reason = reason


class WorkspaceDecodingError(WorkspaceError):
    """
    Невозможно декодировать содержимое ресурса в строку.
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

    @property
    @abstractmethod
    def kind(self) -> WorkspaceKind:
        """Namespace (user/system/tmp)."""
        ...

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
    """Управляет жизненным циклом workspace'ов одного ``WorkspaceKind``.

    Менеджер фиксирует ровно один namespace: реализация знает свой
    :attr:`kind`, а сервис-ключ в DI — маркерный подкласс (например,
    :class:`UserWorkspaceManager`). Разделять namespace'ы через параметр
    метода намеренно не стали — иначе пришлось бы тянуть kind в сигнатуры
    tools и сервисов.
    """

    @property
    @abstractmethod
    def kind(self) -> WorkspaceKind: ...

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
        несколькими менеджерами разных :class:`WorkspaceKind` — каждый
        создаёт свой namespace под тем же id при первом обращении.
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


class UnknownWorkspaceKindError(WorkspaceError):
    """Запрошен :class:`WorkspaceKind`, которого нет в резолвере.

    Отдельная ошибка (а не :class:`WorkspaceNotFoundError`): тот уровень —
    «ресурс внутри workspace не найден», а здесь — «сам namespace не
    зарегистрирован». Сохраняем контекст: имя kind.
    """

    def __init__(self, kind: WorkspaceKind) -> None:
        super().__init__(f"unknown workspace kind: {kind.name!r}")
        self.kind = kind


class WorkspaceResolver(Resolver[WorkspaceKind, WorkspaceService]):
    """Маппинг :class:`WorkspaceKind` → :class:`WorkspaceService`.

    Нужен tools'ам, работающим с несколькими namespace'ами одновременно,
    чтобы вместо инжекции 2-3 конкретных сервисов они получали один
    резолвер и спрашивали у него сервис по kind. Реализует существующий
    паттерн :class:`Resolver` из ``core.patterns``.
    """


class MappingWorkspaceResolver(WorkspaceResolver):
    """Резолвер поверх :class:`Mapping`.

    Зафиксированный набор сервисов передаётся в конструкторе (обычно из
    DI). При попытке получить неизвестный kind — :class:`UnknownWorkspaceKindError`.
    """

    def __init__(self, services: Mapping[WorkspaceKind, WorkspaceService]) -> None:
        self._services: Mapping[WorkspaceKind, WorkspaceService] = dict(services)

    def resolve(self, req: WorkspaceKind) -> WorkspaceService:
        try:
            return self._services[req]
        except KeyError as e:
            raise UnknownWorkspaceKindError(req) from e


class AllowedWorkspacesSpec(Specification[WorkspaceKind]):
    """Белый список :class:`WorkspaceKind`-ов.

    ``check`` — мембершип-проверка; ``kinds`` — публичная коллекция для
    построения enum'а в JSON-schema параметра tool'а. Единый источник:
    оба пути (валидация в ``execute`` и описание схемы) используют один
    экземпляр spec'а.
    """

    def __init__(self, allowed: Iterable[WorkspaceKind]) -> None:
        self._kinds: frozenset[WorkspaceKind] = frozenset(allowed)

    def check(self, value: WorkspaceKind) -> bool:
        return value in self._kinds

    @property
    def kinds(self) -> frozenset[WorkspaceKind]:
        return self._kinds
