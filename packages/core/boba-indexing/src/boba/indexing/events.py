"""События прогона и накопление статистики."""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import NewType
from uuid import UUID

from boba.indexing.sections import SourceId

__all__ = [
    "BaseIndexEvent",
    "BatchStarted",
    "BatchUpserted",
    "ChunksDeleted",
    "CleanupStarted",
    "CompletedItem",
    "IndexEvent",
    "IndexStats",
    "IndexStatsBuilder",
    "PhaseTransition",
    "RunFinished",
    "RunId",
    "RunStarted",
    "Severity",
    "SourceFailed",
    "SourceGone",
    "SourceIndexed",
    "SourceKind",
    "SourceSkippedUnchanged",
    "SourceTally",
    "TallyBuilder",
    "new_run_id",
]

RunId = NewType("RunId", UUID)
"""Идентификатор одного прогона Pipeline.index(...) на 1 вызов."""


def new_run_id() -> RunId:
    """Свежий RunId."""
    return RunId(uuid.uuid4())


class SourceKind(StrEnum):
    """Вид источника: корневой или числящийся за родителем.

    Различие доменное, а не предметное: у дочернего источника заполнен
    SourceMark.parent, и живёт он ровно столько, сколько живёт родитель.
    Счёт по видам ведётся врозь, иначе в итоге прогона не видно, что именно
    не доехало до индекса.
    """

    ROOT = "root"
    CHILD = "child"

    @classmethod
    def of(cls, parent: object | None) -> SourceKind:
        if parent is None:
            return cls.ROOT

        return cls.CHILD


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
    """Время от monotonic-часов backend для измерения длительности фаз."""

    @classmethod
    @abstractmethod
    def name(cls) -> str:
        """Стабильное имя event-типа для serialization / matching в sink"""
        ...


@dataclass(frozen=True)
class PhaseTransition(BaseIndexEvent, ABC):
    """Граница фазы run индексации (RunStarted, BatchStarted, CleanupStarted,
    RunFinished).
    """

    @abstractmethod
    def label(self) -> str:
        """Короткий human-readable ярлык"""
        ...

    def details(self) -> Mapping[str, str]:
        """Опциональные дополнительные поля для log вывода."""
        return {}

    def severity(self) -> Severity:
        return Severity.INFO


@dataclass(frozen=True)
class CompletedItem(BaseIndexEvent, ABC):
    """Атомарный завершённый item (success или skip-after-error); severity задаёт
    реализация.
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
    """Старт cleanup-фазы: поиск источников, которых обход не видел."""

    @classmethod
    def name(cls) -> str:
        return "cleanup.started"

    def label(self) -> str:
        return "cleanup started"


@dataclass(frozen=True)
class RunFinished(PhaseTransition):
    """Финальный event run'а с агрегированной статистикой."""

    stats: IndexStats

    @classmethod
    def name(cls) -> str:
        return "run.finished"

    def label(self) -> str:
        roots = self.stats.roots
        children = self.stats.children
        return (
            f"run finished: roots {roots.indexed} indexed, "
            f"{roots.unchanged} unchanged, {roots.failed} failed; "
            f"children {children.indexed} indexed, "
            f"{children.unchanged} unchanged, {children.failed} failed"
        )

    def details(self) -> Mapping[str, str]:
        fields: dict[str, str] = {}
        for kind in SourceKind:
            tally = self.stats.tally_of(kind)
            fields[f"{kind.value}_seen"] = str(tally.seen)
            fields[f"{kind.value}_indexed"] = str(tally.indexed)
            fields[f"{kind.value}_unchanged"] = str(tally.unchanged)
            fields[f"{kind.value}_failed"] = str(tally.failed)
            fields[f"{kind.value}_deleted"] = str(tally.deleted)

        return fields


@dataclass(frozen=True)
class SourceIndexed(CompletedItem):
    """Один source-id успешно проиндексирован."""

    source_id: SourceId
    chunks_total: int
    chunks_upserted: int
    chunks_skipped: int
    kind: SourceKind

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
            "kind": self.kind.value,
            "chunks_total": str(self.chunks_total),
            "chunks_upserted": str(self.chunks_upserted),
            "chunks_skipped": str(self.chunks_skipped),
        }


@dataclass(frozen=True)
class SourceFailed(CompletedItem):
    """Один source-id не удалось обработать (нефатально)."""

    source_id: SourceId
    reason: str
    kind: SourceKind

    @classmethod
    def name(cls) -> str:
        return "source.failed"

    def headline(self) -> str:
        return f"failed {self.source_id}: {self.reason}"

    def details(self) -> Mapping[str, str]:
        return {
            "source_id": self.source_id,
            "kind": self.kind.value,
            "reason": self.reason,
        }

    def severity(self) -> Severity:
        return Severity.WARN


@dataclass(frozen=True)
class SourceSkippedUnchanged(CompletedItem):
    """Source целиком skip — все chunks уже проиндексированы (hash match)."""

    source_id: SourceId
    chunks_total: int
    kind: SourceKind

    @classmethod
    def name(cls) -> str:
        return "source.skipped_unchanged"

    def headline(self) -> str:
        return f"unchanged {self.source_id} ({self.chunks_total} chunks)"

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
    """Хвост чанков реиндексированного источника снят: источник стал короче."""

    source_id: SourceId
    count: int
    kind: SourceKind

    @classmethod
    def name(cls) -> str:
        return "chunks.deleted"

    def headline(self) -> str:
        return f"deleted {self.count} stale chunks of {self.source_id}"

    def details(self) -> Mapping[str, str]:
        return {"source_id": self.source_id, "count": str(self.count)}


@dataclass(frozen=True)
class SourceGone(CompletedItem):
    """Источника больше нет в источнике данных: его чанки и запись сняты."""

    source_id: SourceId
    chunks_deleted: int
    kind: SourceKind

    @classmethod
    def name(cls) -> str:
        return "source.gone"

    def headline(self) -> str:
        return f"gone {self.source_id} ({self.chunks_deleted} chunks deleted)"

    def details(self) -> Mapping[str, str]:
        return {
            "source_id": self.source_id,
            "chunks_deleted": str(self.chunks_deleted),
        }


@dataclass(frozen=True)
class SourceTally:
    """Счёт источников одного вида за прогон.

    seen — сколько дошло до конвейера, из них indexed получили новые чанки,
    unchanged совпали с индексом, failed сорвались. deleted сняты очисткой как
    исчезнувшие у источника данных.
    """

    seen: int = 0
    indexed: int = 0
    unchanged: int = 0
    failed: int = 0
    deleted: int = 0
    chunks_upserted: int = 0
    chunks_deleted: int = 0


@dataclass(frozen=True)
class IndexStats:
    """Сводка одного Pipeline.run() / .index(): корни и дети врозь."""

    roots: SourceTally
    children: SourceTally

    def chunks_upserted(self) -> int:
        return self.roots.chunks_upserted + self.children.chunks_upserted

    def chunks_deleted(self) -> int:
        return self.roots.chunks_deleted + self.children.chunks_deleted

    def failed(self) -> int:
        return self.roots.failed + self.children.failed

    def tally_of(self, kind: SourceKind) -> SourceTally:
        if kind is SourceKind.ROOT:
            return self.roots

        return self.children


@dataclass
class TallyBuilder:
    """Мутабельный счёт одного вида источников."""

    seen: int = 0
    indexed: int = 0
    unchanged: int = 0
    failed: int = 0
    deleted: int = 0
    chunks_upserted: int = 0
    chunks_deleted: int = 0

    def build(self) -> SourceTally:
        return SourceTally(
            seen=self.seen,
            indexed=self.indexed,
            unchanged=self.unchanged,
            failed=self.failed,
            deleted=self.deleted,
            chunks_upserted=self.chunks_upserted,
            chunks_deleted=self.chunks_deleted,
        )


@dataclass
class IndexStatsBuilder:
    """Мутабельный аккумулятор IndexStats, обновляемый из event stream'а.

    Конвейер даёт ровно одно завершающее событие на источник, поэтому
    источники считаются счётчиком, без множества увиденных id.
    """

    roots: TallyBuilder = field(default_factory=TallyBuilder)
    children: TallyBuilder = field(default_factory=TallyBuilder)

    def of(self, kind: SourceKind) -> TallyBuilder:
        if kind is SourceKind.ROOT:
            return self.roots

        return self.children

    def build(self) -> IndexStats:
        return IndexStats(roots=self.roots.build(), children=self.children.build())
