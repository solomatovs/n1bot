"""
IndexEvent — события одного прогона Pipeline.index()

Базовые категории (две — минимально-достаточный набор):

1. PhaseTransition — границы фаз процесса

2. CompletedItem — атомарный завершённый item

fatal ошибки просто выбрасывают exception и прерывают поток
"""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import NewType
from uuid import UUID

from boba.indexing.sections import SourceId
from boba.indexing.stats import IndexStats

__all__ = [
    "BaseIndexEvent",
    "BatchStarted",
    "BatchUpserted",
    "ChunksDeleted",
    "CleanupStarted",
    "CompletedItem",
    "IndexEvent",
    "PhaseTransition",
    "RunFinished",
    "RunId",
    "RunStarted",
    "Severity",
    "SourceFailed",
    "SourceIndexed",
    "SourceSkippedUnchanged",
    "new_run_id",
]


RunId = NewType("RunId", UUID)
"""Идентификатор одного прогона Pipeline.index(...) на 1 вызов."""


def new_run_id() -> RunId:
    """Свежий RunId."""
    return RunId(uuid.uuid4())


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


@dataclass(frozen=True)
class RunStarted(PhaseTransition):
    """Pipeline.index() начал работу: первый event каждого run'а."""

    @classmethod
    def name(cls) -> str:
        return "run.started"

    def label(self) -> str:
        return "run started"


@dataclass(frozen=True)
class BatchStarted(PhaseTransition):
    """Старт upsert-батча в ChunkStore."""

    batch_index: int
    size: int

    @classmethod
    def name(cls) -> str:
        return "batch.started"

    def label(self) -> str:
        return f"batch {self.batch_index} started ({self.size} chunks)"

    def details(self) -> Mapping[str, str]:
        return {
            "batch_index": str(self.batch_index),
            "size": str(self.size),
        }


@dataclass(frozen=True)
class CleanupStarted(PhaseTransition):
    """Старт cleanup-фазы (удаление stale записей)."""

    strategy: str

    @classmethod
    def name(cls) -> str:
        return "cleanup.started"

    def label(self) -> str:
        return f"cleanup started ({self.strategy})"

    def details(self) -> Mapping[str, str]:
        return {"strategy": self.strategy}


@dataclass(frozen=True)
class RunFinished(PhaseTransition):
    """Финальный event run'а с агрегированной статистикой."""

    stats: IndexStats

    @classmethod
    def name(cls) -> str:
        return "run.finished"

    def label(self) -> str:
        s = self.stats
        return (
            f"run finished: {s.sources_processed} src "
            f"({s.chunks_upserted} upserted, {s.chunks_deleted} deleted)"
        )

    def details(self) -> Mapping[str, str]:
        s = self.stats
        return {
            "sources_processed": str(s.sources_processed),
            "sources_failed": str(s.sources_failed),
            "sources_skipped_unchanged": str(s.sources_skipped_unchanged),
            "chunks_upserted": str(s.chunks_upserted),
            "chunks_deleted": str(s.chunks_deleted),
        }


@dataclass(frozen=True)
class SourceIndexed(CompletedItem):
    """Один source-id успешно проиндексирован."""

    source_id: SourceId
    chunks_total: int
    chunks_upserted: int
    chunks_skipped: int

    @classmethod
    def name(cls) -> str:
        return "source.indexed"

    def headline(self) -> str:
        return (
            f"indexed {self.source_id} "
            f"({self.chunks_upserted}/{self.chunks_total} upserted, "
            f"{self.chunks_skipped} skipped)"
        )

    def details(self) -> Mapping[str, str]:
        return {
            "source_id": self.source_id,
            "chunks_total": str(self.chunks_total),
            "chunks_upserted": str(self.chunks_upserted),
            "chunks_skipped": str(self.chunks_skipped),
        }


@dataclass(frozen=True)
class SourceFailed(CompletedItem):
    """Один source-id не удалось обработать (нефатально)."""

    source_id: SourceId
    reason: str

    @classmethod
    def name(cls) -> str:
        return "source.failed"

    def headline(self) -> str:
        return f"failed {self.source_id}: {self.reason}"

    def details(self) -> Mapping[str, str]:
        return {
            "source_id": self.source_id,
            "reason": self.reason,
        }

    def severity(self) -> Severity:
        return Severity.WARN


@dataclass(frozen=True)
class SourceSkippedUnchanged(CompletedItem):
    """Source целиком skip — все chunks уже проиндексированы (hash match)."""

    source_id: SourceId
    chunks_total: int

    @classmethod
    def name(cls) -> str:
        return "source.skipped_unchanged"

    def headline(self) -> str:
        return (
            f"unchanged {self.source_id} ({self.chunks_total} chunks)"
        )

    def details(self) -> Mapping[str, str]:
        return {
            "source_id": self.source_id,
            "chunks_total": str(self.chunks_total),
        }


@dataclass(frozen=True)
class BatchUpserted(CompletedItem):
    """Батч чанков успешно upsert'нут в ChunkStore."""

    batch_index: int
    count: int

    @classmethod
    def name(cls) -> str:
        return "batch.upserted"

    def headline(self) -> str:
        return f"upserted batch {self.batch_index} ({self.count} chunks)"

    def details(self) -> Mapping[str, str]:
        return {
            "batch_index": str(self.batch_index),
            "count": str(self.count),
        }


@dataclass(frozen=True)
class ChunksDeleted(CompletedItem):
    """Stale chunks удалены в cleanup-фазе."""

    count: int

    @classmethod
    def name(cls) -> str:
        return "chunks.deleted"

    def headline(self) -> str:
        return f"deleted {self.count} stale chunks"

    def details(self) -> Mapping[str, str]:
        return {"count": str(self.count)}
