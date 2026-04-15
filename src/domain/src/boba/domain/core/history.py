"""Абстракция журнала истории событий."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import Iterator
from uuid import UUID

from boba.domain.agent.events import AgentEvent
from boba.domain.core.patterns import Id


class EntryId(Id[UUID]):
    """Идентификатор записи в журнале."""


class RequestId(Id[UUID]):
    """Идентификатор запроса пользователя. Группирует события одного запроса."""


@dataclass(frozen=True)
class HistoryEntry:
    """Одна запись в журнале истории."""

    id: EntryId
    parent_id: EntryId | None
    request_id: RequestId
    timestamp: datetime
    event: AgentEvent


class HistoryService(ABC):
    """Журнал истории событий workspace'а."""

    @abstractmethod
    def append(self, request_id: RequestId, event: AgentEvent) -> HistoryEntry:
        """Добавить событие в журнал. Возвращает созданную запись."""
        ...

    @abstractmethod
    def entries(self) -> Iterator[HistoryEntry]:
        """Все записи в порядке добавления."""
        ...

    @abstractmethod
    def entries_by_request(self, request_id: RequestId) -> Iterator[HistoryEntry]:
        """Записи конкретного запроса."""
        ...

    @abstractmethod
    def requests(self) -> Iterator[RequestId]:
        """Список всех request_id в хронологическом порядке."""
        ...
