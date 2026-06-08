"""
Pipeline — единая сборка стадий индексации.

Один проход по источнику: ReqT -> RawDocument -> Section[T]. Дальше — два
терминала, не пересекающихся между собой:

    sections()             -> Iterator[Section[T]]   read-only (поиск, CLI, tool)
    index(chunker, sink…)  -> Iterator[IndexEvent]   индексация в Store

Read-spine (source -> transport -> reader) общий; chunker/sink/query нужны
только терминалу index, поэтому они — аргументы метода, а не конструктора.
Тот, кому нужны только секции, не собирает chunker и sink.

Transport владеет lifecycle handle'а (with в его generator'е); reader
читает, но не закрывает. Поток ленивый — чанки текут от transport до reconcile
по одному, промежуточных буферов Pipeline не выделяет.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Generic, TypeVar

from boba.indexing.chunker import Chunker
from boba.indexing.chunks import Chunk
from boba.indexing.cleanup import CleanupContext, CleanupStrategy, NoneCleanup
from boba.indexing.errors import IndexingError
from boba.indexing.events import (
    ChunksDeleted,
    CleanupStarted,
    IndexEvent,
    RunFinished,
    RunId,
    RunStarted,
    SourceFailed,
    SourceIndexed,
    SourceSkippedUnchanged,
    new_run_id,
)
from boba.indexing.index_views import IndexQuery, IndexSink
from boba.indexing.reader import Reader
from boba.indexing.request import Request, RequestSource
from boba.indexing.sections import Section, SourceId
from boba.indexing.stats import IndexStats, IndexStatsBuilder
from boba.indexing.transport import Transport

__all__ = ["IndexerConfig", "Pipeline"]

ReqT = TypeVar("ReqT", bound=Request)
T = TypeVar("T")


@dataclass(frozen=True)
class IndexerConfig(Generic[T]):
    """Параметры одного прогона Pipeline.index / Pipeline.run."""

    cleanup: CleanupStrategy = field(default_factory=NoneCleanup)
    force_update: bool = False


class Pipeline(Generic[ReqT, T]):
    """Сборка стадий source -> transport -> reader с двумя терминалами."""

    def __init__(
        self,
        *,
        source: RequestSource[ReqT],
        transport: Transport[ReqT],
        reader: Reader[T],
    ) -> None:
        self._source = source
        self._transport = transport
        self._reader = reader

    def sections(self) -> Iterator[Section[T]]:
        """Плоский поток Section[T] по всем источникам; без записи в индекс."""
        for request in self._source.requests():
            yield from self._sections_of(request)

    def index(
        self,
        *,
        chunker: Chunker[T],
        sink: IndexSink[T],
        query: IndexQuery[T],
        config: IndexerConfig[T],
    ) -> Iterator[IndexEvent]:
        """
        Индексировать все источники; ленивый поток IndexEvent.

        Per-source flow завершается ровно одним CompletedItem-событием
        (SourceIndexed | SourceSkippedUnchanged | SourceFailed). Per-run cleanup
        идёт после всех источников через config.cleanup.

        Run-level state (stats, touched_sources) выводится из самого event-потока
        единственным _observe-callback'ом — к моменту RunFinished стат
        синхронен с уже выехавшим потоком.
        """
        run_id = new_run_id()
        run_start = time.time()
        stats = IndexStatsBuilder()
        touched: set[SourceId] = set()

        yield RunStarted(run_id=run_id, monotonic_ns=time.monotonic_ns())

        for request in self._source.requests():
            for event in self._process_source(
                request=request,
                chunker=chunker,
                sink=sink,
                config=config,
                run_id=run_id,
                run_start=run_start,
            ):
                self._observe(event, stats=stats, touched=touched)
                yield event

        for event in self._run_cleanup(
            query=query,
            config=config,
            run_id=run_id,
            run_start=run_start,
            touched=touched,
        ):
            self._observe(event, stats=stats, touched=touched)
            yield event

        yield RunFinished(
            run_id=run_id,
            monotonic_ns=time.monotonic_ns(),
            stats=stats.build(),
        )

    def run(
        self,
        *,
        chunker: Chunker[T],
        sink: IndexSink[T],
        query: IndexQuery[T],
        config: IndexerConfig[T],
    ) -> IndexStats:
        """Прогнать index до конца; вернуть итоговый IndexStats."""
        for event in self.index(
            chunker=chunker, sink=sink, query=query, config=config
        ):
            if isinstance(event, RunFinished):
                return event.stats
        return IndexStatsBuilder().build()

    def _sections_of(self, request: ReqT) -> Iterator[Section[T]]:
        """transport -> reader для одного request'а; секции по одной."""
        for raw in self._transport.fetch(request):
            yield from self._reader.read(raw)

    def _process_source(
        self,
        *,
        request: ReqT,
        chunker: Chunker[T],
        sink: IndexSink[T],
        config: IndexerConfig[T],
        run_id: RunId,
        run_start: float,
    ) -> Iterator[IndexEvent]:
        """
        Per-source streaming pipeline; yield ровно одно CompletedItem-событие.

        Все чанки в этом вызове относятся к одному source_id — их идентичность
        несут поля самих Chunk'ов, поэтому scope-narrow перед reconcile не нужен.
        run_start передаётся как время прохода и включает инкрементальное
        обновление.
        """
        try:
            summary = sink.reconcile(
                chunks=self._chunks_of(request, chunker),
                time_at_least=run_start,
                force=config.force_update,
            )
        except IndexingError as e:
            yield SourceFailed(
                run_id=run_id,
                monotonic_ns=time.monotonic_ns(),
                source_id=request.source_id,
                reason=str(e),
            )
            return

        if summary.upserted == 0:
            yield SourceSkippedUnchanged(
                run_id=run_id,
                monotonic_ns=time.monotonic_ns(),
                source_id=request.source_id,
                chunks_total=summary.total,
            )
            return

        yield SourceIndexed(
            run_id=run_id,
            monotonic_ns=time.monotonic_ns(),
            source_id=request.source_id,
            chunks_total=summary.total,
            chunks_upserted=summary.upserted,
            chunks_skipped=summary.unchanged,
        )

    def _chunks_of(
        self,
        request: ReqT,
        chunker: Chunker[T],
    ) -> Iterator[Chunk[T]]:
        """sections одного источника -> chunker -> yield.

        Chunker обязан эмитить чанки с уже заполненным content_hash (через свой
        injected KeyEncoder[T]); post-enrichment'а на этом уровне нет.
        """
        yield from chunker.chunk(self._sections_of(request))

    def _run_cleanup(
        self,
        *,
        query: IndexQuery[T],
        config: IndexerConfig[T],
        run_id: RunId,
        run_start: float,
        touched: set[SourceId],
    ) -> Iterator[IndexEvent]:
        """Per-run cleanup: config.cleanup.execute через CleanupContext."""
        yield CleanupStarted(
            run_id=run_id,
            monotonic_ns=time.monotonic_ns(),
            strategy=type(config.cleanup).__name__,
        )

        deleted = config.cleanup.execute(
            CleanupContext(
                query=query,
                run_start=run_start,
                touched_sources=frozenset(touched),
            )
        )

        yield ChunksDeleted(
            run_id=run_id,
            monotonic_ns=time.monotonic_ns(),
            count=deleted,
        )

    @staticmethod
    def _observe(
        event: IndexEvent,
        *,
        stats: IndexStatsBuilder,
        touched: set[SourceId],
    ) -> None:
        """Единственная точка обновления stats и touched; один вызов на event."""
        if isinstance(event, SourceIndexed):
            stats.source_seen(event.source_id)
            for _ in range(event.chunks_upserted):
                stats.chunk_upserted()
            touched.add(event.source_id)

        elif isinstance(event, SourceSkippedUnchanged):
            stats.source_seen(event.source_id)
            stats.source_skipped_unchanged()
            touched.add(event.source_id)

        elif isinstance(event, SourceFailed):
            stats.source_failed()

        elif isinstance(event, ChunksDeleted):
            stats.chunks_deleted_add(event.count)
