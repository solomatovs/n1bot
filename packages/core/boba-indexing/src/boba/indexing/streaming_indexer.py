"""StreamingIndexer — concrete `Indexer[ReqT, T]`, оркестратор индексации документов.

Per-source pipeline собран как ленивая цепочка генераторов
чанки текут от transport до reconcile по одному:

    request
      └─ transport.stream(ctx, [request])    → Iterable[RawDocument]
          └─ decoders[0..N].convert(raw)     → RawDocument (по порядку)
              └─ reader.convert(decoded)     → Iterable[Section[T]]
                  └─ chunker.stream(ctx, ...) → Iterable[Chunk[T]]
                                                (content_hash уже заполнен)
                      └─ sink.narrow(...).reconcile(chunks, ...)
                              ↓
                          ReconcileSummary

`decoders` — опциональная цепочка `RawDocument → RawDocument`-преобразований
между transport и reader (JSON-payload → HTML-handle, base64 → bytes,
gzip-decompress, и т.п.). По умолчанию пустой кортеж — identity.

`reconcile` принимает `Iterable[Chunk[T]]`
Реализация сама решает, потреблять поток chunk-за-chunk'ом или собирать в list внутри.
Снаружи StreamingIndexer не выделяет промежуточных буферов.

Run-level state (`stats`, `touched_sources`) выводится из event stream'а
единственным `_observe(event)`-callback'ом — нет накопления параллельно
с эмитом события. Каждый emit идёт через `_observe → yield`, поэтому к
моменту `RunFinished` стат и touched-set синхронны с уже выехавшим из
`stream()` потоком.

Per-source flow завершается ровно одним CompletedItem-event'ом:
SourceIndexed | SourceSkippedUnchanged | SourceFailed.

Per-run cleanup: `config.cleanup.execute(CleanupContext)` после всех
sources. Cleanup получает `IndexQuery` через CleanupContext.query —
стратегия делает только filter-based ops (`clean(where=...)`), не видит
Store напрямую и не имеет write-доступа.

`force_update=True` - означает что надо в любом случае выполнить re-embed

StreamingIndexer не знает про scope (collection / namespace / tag / ...) —
он инкапсулирован в view-impl, привязанных в его конструкторе
"""

from __future__ import annotations

import time
from collections.abc import Iterator, Sequence
from typing import TypeVar

from boba.indexing.chunker import Chunker
from boba.indexing.chunks import Chunk
from boba.indexing.cleanup import CleanupContext
from boba.indexing.context import PipelineContext
from boba.indexing.decoder import Decoder
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
from boba.indexing.indexer import Indexer, IndexerConfig
from boba.indexing.reader import Reader
from boba.indexing.request import Request, RequestSource
from boba.indexing.sections import Section, SourceId
from boba.indexing.stats import IndexStats, IndexStatsBuilder
from boba.indexing.transport import Transport

__all__ = ["StreamingIndexer"]

ReqT = TypeVar("ReqT", bound=Request)
T = TypeVar("T")


class StreamingIndexer(Indexer[ReqT, T]):
    """
    Streaming индексатор документов
    Оркестратор который собирает стадии воединно
    """

    def __init__(  # noqa: PLR0913 — оркестратор честно нуждается в этих deps
        self,
        request_source: RequestSource[ReqT],
        transport: Transport[ReqT],
        reader: Reader[T],
        chunker: Chunker[T],
        sink: IndexSink[T],
        query: IndexQuery[T],
        decoders: Sequence[Decoder] = (),
    ) -> None:
        self._request_source = request_source
        self._transport = transport
        self._reader = reader
        self._chunker = chunker
        self._sink = sink
        self._query = query
        self._decoders: tuple[Decoder, ...] = tuple(decoders)

    def stream(
        self,
        ctx: PipelineContext,
        config: IndexerConfig[T],
    ) -> Iterator[IndexEvent]:
        run_id = new_run_id()
        run_start = time.time()
        stats = IndexStatsBuilder()
        touched_sources: set[SourceId] = set()

        yield RunStarted(run_id=run_id, monotonic_ns=time.monotonic_ns())

        for request in self._request_source.stream(ctx):
            for event in self._process_source(
                ctx=ctx,
                config=config,
                request=request,
                run_id=run_id,
                run_start=run_start,
            ):
                self._observe(event, stats=stats, touched=touched_sources)
                yield event

        for event in self._run_cleanup(
            config=config,
            run_id=run_id,
            run_start=run_start,
            touched_sources=touched_sources,
        ):
            self._observe(event, stats=stats, touched=touched_sources)
            yield event

        yield RunFinished(
            run_id=run_id,
            monotonic_ns=time.monotonic_ns(),
            stats=stats.build(),
        )

    def invoke(
        self,
        ctx: PipelineContext,
        config: IndexerConfig[T],
    ) -> IndexStats:
        for event in self.stream(ctx, config):
            if isinstance(event, RunFinished):
                return event.stats

        return IndexStatsBuilder().build()

    def _process_source(
        self,
        *,
        ctx: PipelineContext,
        config: IndexerConfig[T],
        request: ReqT,
        run_id: RunId,
        run_start: float,
    ) -> Iterator[IndexEvent]:
        """
        Per-source streaming pipeline
        Yield ровно одно CompletedItem-событие

        Все чанки в этом вызове относятся к одному `source_id`
        их идентичность несут поля самих Chunk'ов (chunk_id, source_id, …),
        поэтому никакого scope-narrow перед reconcile делать не нужно.
        """
        try:
            # запускаю обновление chunk'ов
            # run_start передается как время текущего прохода pipeline'а
            # и позволяет выполнять инкрементальное обновление
            summary = self._sink.reconcile(
                chunks=self._chunks_stream(ctx, request, config),
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

    def _chunks_stream(
        self,
        ctx: PipelineContext,
        request: ReqT,
        config: IndexerConfig[T],
    ) -> Iterator[Chunk[T]]:
        """transport → reader → chunker → yield.

        `Chunker.stream` обязан эмитить чанки с уже заполненным
        `content_hash` (через свой injected `KeyEncoder[T]`). Никаких
        post-enrichment'ов на этом уровне нет.
        """
        del config
        sections = self._sections_stream(ctx, request)
        yield from self._chunker.stream(ctx, sections)

    def _sections_stream(
        self,
        ctx: PipelineContext,
        request: ReqT,
    ) -> Iterator[Section[T]]:
        """transport → decoders (in order) → reader → yield Section[T]'ы по одной.

        `decoders` — цепочка `RawDocument → RawDocument`-преобразований
        (например, JSON-payload → HTML-handle); применяются по порядку,
        пустой список = identity.
        """
        for raw_doc in self._transport.stream(ctx, [request]):
            decoded = raw_doc
            for decoder in self._decoders:
                decoded = decoder.convert(decoded)
            yield from self._reader.convert(decoded)

    def _run_cleanup(
        self,
        *,
        config: IndexerConfig[T],
        run_id: RunId,
        run_start: float,
        touched_sources: set[SourceId],
    ) -> Iterator[IndexEvent]:
        yield CleanupStarted(
            run_id=run_id,
            monotonic_ns=time.monotonic_ns(),
            strategy=type(config.cleanup).__name__,
        )

        cleanup_ctx = CleanupContext(
            query=self._query,
            run_start=run_start,
            touched_sources=frozenset(touched_sources),
        )

        deleted = config.cleanup.execute(cleanup_ctx)

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
        """
        Единственная точка обновления stats и touched_sources.
        По одному вызову на каждый эмиченный event
        """
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
