"""Абстракция журнала истории событий."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime

from boba.domain.agent.events import AgentEvent
from boba.domain.agent.models import RequestId
from boba.domain.core.errors import TerminalError
from boba.domain.core.patterns import UuId


class HistoryError(TerminalError):
    """Базовая ошибка ``HistoryService``.

    Контракт ошибок: публичные методы ``HistoryService`` ОБЯЗАНЫ оборачивать
    любые внутренние исключения (ошибки хранилища, сериализации и т.п.) в
    потомков этого класса. Исходное исключение доступно через ``__cause__``.
    """

    def __init__(self, reason: Exception, ctx: str = "") -> None:
        self.reason = reason
        self.ctx = ctx
        prefix = self._prefix()
        msg = f"{prefix}: {reason}"
        if ctx:
            msg = f"{prefix} ({ctx}): {reason}"
        super().__init__(msg)

    def _prefix(self) -> str:
        return "History error"


class HistoryWriteError(HistoryError):
    """Не удалось создать или записать файл истории."""

    def _prefix(self) -> str:
        return "Cannot write history"


class HistoryReadError(HistoryError):
    """Не удалось прочитать журнал истории."""

    def _prefix(self) -> str:
        return "Cannot read history"


class EntryId(UuId):
    """
    Идентификатор записи в журнале.
    """


@dataclass(frozen=True)
class HistoryEntry:
    """Одна запись в журнале истории."""

    id: EntryId
    parent_id: EntryId | None
    request_id: RequestId
    timestamp: datetime
    event: AgentEvent


class HistoryService(ABC):
    """
    Журнал истории событий workspace'а.

    request_id отслеживается автоматически по UserQueryReceived.
    parent_id (цепочка) отслеживается автоматически по последней записи.

    Контракт ошибок: реализации ОБЯЗАНЫ наружу бросать только потомков
    ``HistoryError`` (``HistoryWriteError`` / ``HistoryReadError``).
    Внутренние исключения (ошибки хранилища, сериализации и т.п.)
    должны быть обёрнуты; исходное — через ``__cause__``.
    """

    @abstractmethod
    def append(self, event: AgentEvent) -> HistoryEntry:
        """
        Добавить событие в журнал.
        Возвращает созданную запись.

        Raises:
            HistoryWriteError: не удалось записать событие.
        """
        ...

    @abstractmethod
    def entries(self, *, reverse: bool = False) -> Iterator[HistoryEntry]:
        """
        Итерация по журналу. Повреждённые/незавершённые записи пропускаются.

        Raises:
            HistoryReadError: не удалось прочитать журнал.
        """
        ...
