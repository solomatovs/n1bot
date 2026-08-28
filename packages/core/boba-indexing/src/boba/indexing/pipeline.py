"""
Pipeline — сборка стадий source -> transport -> reader с двумя терминалами:
sections() и index()

Источники обходятся параллельно: в полёте не больше config.workers штук.
Стадии соединены async-потоками, поэтому ожидание сети и записи одного
источника перекрывается работой остальных. Синхронную часть (разбор документа,
инференс эмбеддера) уносят с loop'а сами реализации портов — конвейер лишь
задаёт, сколько источников идёт одновременно.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
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


@dataclass(frozen=True, kw_only=True)
class IndexerConfig(Generic[T]):
    """Параметры одного прогона Pipeline.index / Pipeline.run."""

    workers: int
    """Сколько источников обрабатывается одновременно"""

    cleanup: CleanupStrategy = field(default_factory=NoneCleanup)
    force_update: bool = False

    skip_failed: bool = True
    """Сорвавшийся источник даёт SourceFailed и прогон идёт дальше;
    False — первая же ошибка роняет прогон"""

    def __post_init__(self) -> None:
        if self.workers < 1:
            msg = f"IndexerConfig.workers must be >= 1, got {self.workers}"
            raise ValueError(msg)


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

    async def sections(self) -> AsyncIterator[Section[T]]:
        """Плоский поток Section[T] по всем источникам; без записи в индекс."""
        async for request in self._source.requests():
            async for section in self._sections_of(request):
                yield section

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

        async for event in self._index_sources(
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

    async def _index_sources(
        self,
        *,
        chunker: Chunker[T],
        sink: IndexSink[T],
        config: IndexerConfig[T],
        run_id: RunId,
        run_start: float,
    ) -> AsyncIterator[IndexEvent]:
        """Обход источников по config.workers штук в полёте; событие — как готово."""
        pending: set[asyncio.Task[IndexEvent]] = set()
        try:
            async for request in self._source.requests():
                pending.add(
                    asyncio.create_task(
                        self._process_source(
                            request=request,
                            chunker=chunker,
                            sink=sink,
                            config=config,
                            run_id=run_id,
                            run_start=run_start,
                        ),
                    ),
                )
                if len(pending) < config.workers:
                    continue
                events, pending = await self._harvest(pending)
                for event in events:
                    yield event

            while pending:
                events, pending = await self._harvest(pending)
                for event in events:
                    yield event
        finally:
            await self._drop(pending)

    @staticmethod
    async def _harvest(
        pending: set[asyncio.Task[IndexEvent]],
    ) -> tuple[list[IndexEvent], set[asyncio.Task[IndexEvent]]]:
        """Дождаться первого доработавшего источника: его события и остаток."""
        done, rest = await asyncio.wait(
            pending,
            return_when=asyncio.FIRST_COMPLETED,
        )
        return [task.result() for task in done], rest

    @staticmethod
    async def _drop(pending: set[asyncio.Task[IndexEvent]]) -> None:
        """Снять недоработавшие источники: потребитель ушёл или упал."""
        if not pending:
            return
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)

    async def _process_source(  # noqa: PLR0913
        self,
        *,
        request: ReqT,
        chunker: Chunker[T],
        sink: IndexSink[T],
        config: IndexerConfig[T],
        run_id: RunId,
        run_start: float,
    ) -> IndexEvent:
        """Per-source streaming pipeline; ровно одно CompletedItem-событие."""
        # identity выводит транспорт, а не request — резолв доступен и когда fetch упадё
        source_id = self._transport.source_id(request)
        try:
            summary = await sink.reconcile(
                chunks=self._chunks_of(request, chunker),
                time_at_least=run_start,
                force=config.force_update,
            )
        except IndexingError as e:
            if not config.skip_failed:
                raise

            return SourceFailed(
                run_id=run_id,
                monotonic_ns=time.monotonic_ns(),
                source_id=source_id,
                reason=str(e),
            )

        if summary.upserted == 0:
            return SourceSkippedUnchanged(
                run_id=run_id,
                monotonic_ns=time.monotonic_ns(),
                source_id=source_id,
                chunks_total=summary.total,
            )

        return SourceIndexed(
            run_id=run_id,
            monotonic_ns=time.monotonic_ns(),
            source_id=source_id,
            chunks_total=summary.total,
            chunks_upserted=summary.upserted,
            chunks_skipped=summary.unchanged,
        )

    async def _sections_of(self, request: ReqT) -> AsyncIterator[Section[T]]:
        """transport -> reader для одного request'а; секции по одной."""
        async for raw in self._transport.fetch(request):
            async for section in self._reader.read(raw):
                yield section

    def _chunks_of(
        self,
        request: ReqT,
        chunker: Chunker[T],
    ) -> AsyncIterator[Chunk[T]]:
        """sections одного источника -> chunker
        чанки приходят с уже заполненным content_hash
        """
        return chunker.chunk(self._sections_of(request))

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
