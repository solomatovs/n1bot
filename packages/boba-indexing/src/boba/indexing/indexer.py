"""
Indexer[ReqT, T] — идемпотентный оркестратор для полного pipeline'а индексации:
Request[ReqT] →
    Transport →
        RawDocument →
            Reader[T] →
                Section[T] →
                    Chunk[T] →
                        IndexSink.reconcile →
                            VectorStore[T] + Embedder[T]

За одну сессию запуска `Indexer.stream(ctx, config)` обязан выполнить:

1. для каждого Chunk[T] вычислить hash через `KeyEncoder[T]` → отдать
   в `IndexSink.reconcile()` — он сам решит skip vs upsert по idempotency-check'у;

2. новые/изменённые → upsert в `VectorStore[T]` + refresh tracking-metadata
   автоматически внутри `IndexSink.reconcile`;

3. cleanup в конце прогона через `IndexerConfig.cleanup` (CleanupStrategy).

API выполнен в режиме stream-style (как Agent.stream()):
    `stream()` yield `IndexEvent`
    observability (для наблюдателя) видит события:
        RunStarted / SourceIndexed / BatchUpserted /
        CleanupStarted / RunFinished и т.п.
    Caller итерирует и реагирует (UI / metrics / log).

В отличаи от AgentEvent, фатальные ошибки полностью прерывают поток (Fatal-errors)
отдельной Terminal-event-категории нет.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Generic, TypeVar

from boba.indexing.cleanup import CleanupStrategy, NoneCleanup
from boba.indexing.context import PipelineContext
from boba.indexing.events import IndexEvent
from boba.indexing.key_encoder import KeyEncoder
from boba.indexing.request import Request
from boba.indexing.stats import IndexStats

__all__ = ["Indexer", "IndexerConfig"]

ReqT = TypeVar("ReqT", bound=Request)
T = TypeVar("T")


@dataclass(frozen=True)
class IndexerConfig(Generic[T]):
    """Параметры одного прогона Indexer.stream."""

    key_encoder: KeyEncoder[T]
    cleanup: CleanupStrategy = field(default_factory=NoneCleanup)
    batch_size: int = 100
    force_update: bool = False


class Indexer(ABC, Generic[ReqT, T]):
    """Оркестратор: parse → chunk → hash → skip-or-upsert → cleanup."""

    @abstractmethod
    def stream(
        self,
        ctx: PipelineContext,
        config: IndexerConfig[T],
    ) -> Iterator[IndexEvent]:
        """
        Прогнать индексацию; ленивый итератор IndexEvent.

        Caller обычно итерирует для UI/metrics/log. Aggregate IndexStats
        обычно прилетает финальным CompletedItem-событием (RunFinished).

        Реализация решает что делать при ошибках per-source, к примеру:
        - skip + count + emit SourceFailed-CompletedItem
        - retry (для временных ошибок)
        - raise (для fatal — config / schema / catastrophic backend-fail)
        """
        ...

    @abstractmethod
    def invoke(
        self,
        ctx: PipelineContext,
        config: IndexerConfig[T],
    ) -> IndexStats:
        """
        Прогнать индексацию до конца; вернуть агрегированный IndexStats.

        Convenience-аналог `stream()` для caller'ов, которым observability
        не нужна — нужен только итоговый отчёт. Concrete impl обычно
        реализует через `stream()` + аккумуляцию в IndexStatsBuilder.
        """
        ...
