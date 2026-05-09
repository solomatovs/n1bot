"""
IndexEvent — события одного прогона `Indexer.stream()`

Базовые категории (две — минимально-достаточный набор):

1. PhaseTransition — границы фаз процесса

2. CompletedItem — атомарный завершённый item

fatal ошибки просто выбрасывают exception и прерывают поток
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

from boba.patterns import UuId

__all__ = [
    "BaseIndexEvent",
    "CompletedItem",
    "IndexEvent",
    "PhaseTransition",
    "RunId",
    "Severity",
]


class RunId(UuId):
    """Идентификатор одного прогона `Indexer.stream(...)` на 1 вызов"""


class Severity(StrEnum):
    """Уровень события для логгера / UI."""

    INFO = "info"
    WARN = "warn"
    ERROR = "error"


@dataclass(frozen=True)
class BaseIndexEvent(ABC):
    """Базовый класс всех событий индексации."""

    run_id: RunId
    monotonic_ns: int
    """
    Время от monotonic-часов backend для измерения длительности фаз"""

    @classmethod
    @abstractmethod
    def name(cls) -> str:
        """Стабильное имя event-типа для serialization / matching в sink"""
        ...


@dataclass(frozen=True)
class PhaseTransition(BaseIndexEvent, ABC):
    """
    Граница фазы run индексации

    Реализации: RunStarted, BatchStarted, CleanupStarted, RunFinished
    """

    @abstractmethod
    def label(self) -> str:
        """Короткий human-readable ярлык"""
        ...

    def details(self) -> Mapping[str, str]:
        """
        Опциональные дополнительные поля для log вывода
        """
        return {}

    def severity(self) -> Severity:
        return Severity.INFO


@dataclass(frozen=True)
class CompletedItem(BaseIndexEvent, ABC):
    """
    Атомарный завершённый item (success или skip-after-error)

    Реализации: SourceIndexed, SourceFailed, BatchUpserted, ChunksDeleted,
    SourceSkippedUnchanged

    Severity варьируется для каждой реализации:
    INFO для успехов
    WARN для skip после нефатальной ошибки
    ERROR не используется
    """

    @abstractmethod
    def headline(self) -> str:
        """Краткое описание item-результата для logs/UI."""
        ...

    def details(self) -> Mapping[str, str]:
        """Опциональные дополнительные поля (source_id, n_chunks и т.п.)."""
        return {}

    def severity(self) -> Severity:
        return Severity.INFO


IndexEvent = PhaseTransition | CompletedItem
"""Sealed union всех событий индексации."""
