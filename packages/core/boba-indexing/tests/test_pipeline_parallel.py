"""Pipeline: параллельный обход источников, статистика и изоляция сбоев."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterable, AsyncIterator, Sequence

import pytest

from boba.indexing import (
    Chunk,
    Chunker,
    ChunkerId,
    ChunkId,
    ChunksDeleted,
    ChunkStream,
    FullCleanup,
    IndexerConfig,
    IndexingError,
    Metadata,
    Pipeline,
    RawDocument,
    Reader,
    ReaderId,
    ReconcileSummary,
    RequestSource,
    RunFinished,
    Section,
    SourceFailed,
    SourceId,
    Transport,
)
from boba.indexing.chunks import ChunkSummary
from boba.indexing.filter import Filter
from boba.indexing.store import IndexQuery, IndexSink
from boba.indexing.values import StringContentHash

pytestmark = pytest.mark.anyio

_FETCH_DELAY_SEC = 0.1
_PAGES = 8


@pytest.fixture(scope="module")
def anyio_backend() -> str:
    return "asyncio"


class _Request:
    """Минимальный Request: только metadata, как требует протокол."""

    def __init__(self, page: str) -> None:
        self.page = page

    @property
    def metadata(self) -> Metadata:
        return Metadata.empty()


class _Source(RequestSource[_Request]):
    def __init__(self, pages: Sequence[str]) -> None:
        self._pages = list(pages)

    async def requests(self) -> AsyncIterator[_Request]:
        for page in self._pages:
            yield _Request(page)


class _SlowTransport(Transport[_Request]):
    """Сетевая задержка на источник; считает пик одновременных загрузок."""

    def __init__(self, delay: float) -> None:
        self._delay = delay
        self.in_flight = 0
        self.peak = 0

    def source_id(self, request: _Request) -> SourceId:
        return SourceId(f"page:{request.page}")

    async def fetch(self, request: _Request) -> AsyncIterator[RawDocument]:
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        try:
            await asyncio.sleep(self._delay)
            yield RawDocument(
                handle=ChunkStream.of(request.page.encode()),
                source_id=self.source_id(request),
                metadata=Metadata.empty(),
            )
        finally:
            self.in_flight -= 1


class _Reader(Reader[str]):
    def reader_id(self) -> ReaderId:
        return ReaderId("test.reader")

    async def read(self, raw: RawDocument) -> AsyncIterator[Section[str]]:
        yield Section(
            source_id=raw.source_id,
            content=(await raw.handle.read()).decode(),
            order=0,
            metadata=raw.metadata,
        )


class _Chunker(Chunker[str]):
    def chunker_id(self) -> ChunkerId:
        return ChunkerId("test.chunker")

    async def chunk(
        self,
        sections: AsyncIterable[Section[str]],
    ) -> AsyncIterator[Chunk[str]]:
        async for section in sections:
            yield Chunk(
                chunk_id=ChunkId(f"{section.source_id}#0"),
                source_id=section.source_id,
                format_content=section.content,
                raw_content=section.content,
                chunk_index=0,
                content_hash=StringContentHash(section.content),
            )


class _Sink(IndexSink[str], IndexQuery[str]):
    """Считает принятые чанки; failing_pages роняет источник как store."""

    def __init__(self, failing: frozenset[str] = frozenset()) -> None:
        self.accepted: list[SourceId] = []
        self.cleaned = 0
        self._failing = failing

    async def reconcile(
        self,
        chunks: AsyncIterable[Chunk[str]],
        *,
        time_at_least: float,
        force: bool = False,
    ) -> ReconcileSummary:
        del time_at_least, force
        total = 0
        async for chunk in chunks:
            if str(chunk.source_id) in self._failing:
                raise IndexingError(f"store rejected {chunk.source_id}")
            self.accepted.append(chunk.source_id)
            total += 1
        return ReconcileSummary(total=total, upserted=total, unchanged=0)

    async def find(
        self,
        *,
        where: Filter | None = None,
        limit: int | None = None,
    ) -> Sequence[ChunkSummary[str]]:
        del where, limit
        return []

    async def clean(self, where: Filter) -> int:
        del where
        self.cleaned += 1
        return 3

    def narrow(self, where: Filter) -> IndexQuery[str]:
        del where
        return self


def _pipeline(
    transport: _SlowTransport,
    pages: Sequence[str],
) -> Pipeline[_Request, str]:
    return Pipeline(source=_Source(pages), transport=transport, reader=_Reader())


async def _run(
    pipeline: Pipeline[_Request, str],
    sink: _Sink,
    workers: int,
) -> list[object]:
    config: IndexerConfig[str] = IndexerConfig(
        workers=workers,
        cleanup=FullCleanup(),
    )
    stream = pipeline.index(chunker=_Chunker(), sink=sink, query=sink, config=config)
    return [item async for item in stream]


class TestParallelSources:
    async def test_sources_overlap_in_flight(self) -> None:
        """Обход идёт внахлёст: страницы не ждут друг друга по очереди."""
        transport = _SlowTransport(_FETCH_DELAY_SEC)
        pages = [str(i) for i in range(_PAGES)]
        sink = _Sink()

        started = time.monotonic()
        await _run(_pipeline(transport, pages), sink, workers=4)
        elapsed = time.monotonic() - started

        sequential = _PAGES * _FETCH_DELAY_SEC
        if elapsed >= sequential / 2:
            raise AssertionError("elapsed < sequential / 2")
        if transport.peak <= 1:
            raise AssertionError("transport.peak > 1")
        if len(sink.accepted) != _PAGES:
            raise AssertionError("len(sink.accepted) == _PAGES")

    async def test_workers_bound_is_respected(self) -> None:
        """Больше config.workers источников в полёте быть не должно."""
        transport = _SlowTransport(_FETCH_DELAY_SEC)
        pages = [str(i) for i in range(_PAGES)]

        await _run(_pipeline(transport, pages), _Sink(), workers=3)

        if transport.peak > 3:
            raise AssertionError("transport.peak <= 3")

    async def test_serial_run_keeps_one_in_flight(self) -> None:
        """workers=1 — прежнее последовательное поведение."""
        transport = _SlowTransport(0.0)
        pages = [str(i) for i in range(4)]

        await _run(_pipeline(transport, pages), _Sink(), workers=1)

        if transport.peak != 1:
            raise AssertionError("transport.peak == 1")


class TestRunOutcome:
    async def test_stats_count_every_source(self) -> None:
        transport = _SlowTransport(0.0)
        pages = [str(i) for i in range(_PAGES)]

        events = await _run(_pipeline(transport, pages), _Sink(), workers=4)

        [finished] = [e for e in events if isinstance(e, RunFinished)]
        if finished.stats.sources_processed != _PAGES:
            raise AssertionError("finished.stats.sources_processed == _PAGES")
        if finished.stats.chunks_upserted != _PAGES:
            raise AssertionError("finished.stats.chunks_upserted == _PAGES")

    async def test_cleanup_runs_after_all_sources(self) -> None:
        """Удаление устаревшего — строго последним, после всех источников."""
        transport = _SlowTransport(0.0)
        pages = [str(i) for i in range(4)]
        sink = _Sink()

        events = await _run(_pipeline(transport, pages), sink, workers=4)

        deleted_at = next(
            i for i, e in enumerate(events) if isinstance(e, ChunksDeleted)
        )
        if sink.cleaned != 1:
            raise AssertionError("sink.cleaned == 1")
        if deleted_at != len(events) - 2:
            raise AssertionError("deleted_at == len(events) - 2")  # перед RunFinished

    async def test_failed_source_does_not_stop_others(self) -> None:
        transport = _SlowTransport(0.0)
        pages = [str(i) for i in range(4)]
        sink = _Sink(failing=frozenset({"page:2"}))

        events = await _run(_pipeline(transport, pages), sink, workers=4)

        failed = [e for e in events if isinstance(e, SourceFailed)]
        if [str(e.source_id) for e in failed] != ["page:2"]:
            raise AssertionError('[str(e.source_id) for e in failed] == ["page:2"]')
        if len(sink.accepted) != len(pages) - 1:
            raise AssertionError("len(sink.accepted) == len(pages) - 1")

    async def test_workers_below_one_rejected(self) -> None:
        with pytest.raises(ValueError, match="workers"):
            IndexerConfig(workers=0)
