"""
Pipeline — сборка стадий source -> transport -> reader с двумя терминалами:
sections() и index()
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from typing import Generic, TypeVar

from boba.indexing.chunks import Chunk
from boba.indexing.errors import IndexingError
from boba.indexing.events import (
    ChunksDeleted,
    CleanupStarted,
    IndexEvent,
    IndexStats,
    IndexStatsBuilder,
    RunFinished,
    RunId,
    RunStarted,
    SourceFailed,
    SourceIndexed,
    SourceSkippedUnchanged,
    new_run_id,
)
from boba.indexing.ports import Chunker, Reader, Request, RequestSource, Transport
from boba.indexing.sections import Section, SourceId
from boba.indexing.store import (
    CleanupContext,
    CleanupStrategy,
    IndexQuery,
    IndexSink,
    NoneCleanup,
)

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

    async def index(
        self,
        *,
        chunker: Chunker[T],
        sink: IndexSink[T],
        query: IndexQuery[T],
        config: IndexerConfig[T],
    ) -> AsyncIterator[IndexEvent]:
        """
        Индексировать все источники; ленивый поток IndexEvent,
        cleanup после всех источников
        """
        run_id = new_run_id()
        run_start = time.time()
        stats = IndexStatsBuilder()
        touched: set[SourceId] = set()

        yield RunStarted(run_id=run_id, monotonic_ns=time.monotonic_ns())

        for request in self._source.requests():
            async for event in self._process_source(
                request=request,
                chunker=chunker,
                sink=sink,
                config=config,
                run_id=run_id,
                run_start=run_start,
            ):
                self._observe(event, stats=stats, touched=touched)
                yield event

        async for event in self._run_cleanup(
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

    async def run(
        self,
        *,
        chunker: Chunker[T],
        sink: IndexSink[T],
        query: IndexQuery[T],
        config: IndexerConfig[T],
    ) -> IndexStats:
        """Прогнать index до конца; вернуть итоговый IndexStats."""
        async for event in self.index(
            chunker=chunker, sink=sink, query=query, config=config
        ):
            if isinstance(event, RunFinished):
                return event.stats
        return IndexStatsBuilder().build()

    def _sections_of(self, request: ReqT) -> Iterator[Section[T]]:
        """transport -> reader для одного request'а; секции по одной."""
        for raw in self._transport.fetch(request):
            yield from self._reader.read(raw)

    async def _process_source(  # noqa: PLR0913
        self,
        *,
        request: ReqT,
        chunker: Chunker[T],
        sink: IndexSink[T],
        config: IndexerConfig[T],
        run_id: RunId,
        run_start: float,
    ) -> AsyncIterator[IndexEvent]:
        """Per-source streaming pipeline; yield ровно одно CompletedItem-событие."""
        # identity выводит транспорт, а не request — резолв доступен и когда fetch упадё
        source_id = self._transport.source_id(request)
        try:
            summary = await sink.reconcile(
                chunks=self._chunks_of(request, chunker),
                time_at_least=run_start,
                force=config.force_update,
            )
        except IndexingError as e:
            yield SourceFailed(
                run_id=run_id,
                monotonic_ns=time.monotonic_ns(),
                source_id=source_id,
                reason=str(e),
            )
            return

        if summary.upserted == 0:
            yield SourceSkippedUnchanged(
                run_id=run_id,
                monotonic_ns=time.monotonic_ns(),
                source_id=source_id,
                chunks_total=summary.total,
            )
            return

        yield SourceIndexed(
            run_id=run_id,
            monotonic_ns=time.monotonic_ns(),
            source_id=source_id,
            chunks_total=summary.total,
            chunks_upserted=summary.upserted,
            chunks_skipped=summary.unchanged,
        )

    def _chunks_of(
        self,
        request: ReqT,
        chunker: Chunker[T],
    ) -> Iterator[Chunk[T]]:
        """sections одного источника -> chunker -> yield
        чанки приходят с уже заполненным content_hash
        """
        yield from chunker.chunk(self._sections_of(request))

    async def _run_cleanup(
        self,
        *,
        query: IndexQuery[T],
        config: IndexerConfig[T],
        run_id: RunId,
        run_start: float,
        touched: set[SourceId],
    ) -> AsyncIterator[IndexEvent]:
        """Per-run cleanup: config.cleanup.execute через CleanupContext."""
        yield CleanupStarted(
            run_id=run_id,
            monotonic_ns=time.monotonic_ns(),
            strategy=type(config.cleanup).__name__,
        )

        deleted = await config.cleanup.execute(
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
