"""Pipeline: параллельный обход источников, реестр, статистика и изоляция сбоев."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterable, AsyncIterator, Sequence
from dataclasses import replace

import pytest

from boba.indexing import (
    Chunk,
    Chunker,
    ChunkerId,
    ChunkId,
    ChunkStream,
    CleanupStarted,
    IndexerConfig,
    IndexingError,
    Metadata,
    Pipeline,
    RawDocument,
    Reader,
    ReaderId,
    ReconcileSummary,
    Request,
    RequestSource,
    RunFinished,
    RunScope,
    Section,
    SourceFailed,
    SourceGone,
    SourceId,
    SourceKind,
    SourceLedger,
    SourceMark,
    SourceProbe,
    SourceRecord,
    SourceSkippedUnchanged,
    Transport,
    TransportKeys,
)
from boba.indexing.store import IndexSink
from boba.indexing.values import StringContentHash

pytestmark = pytest.mark.anyio

_FETCH_DELAY_SEC = 0.1
_PAGES = 8
_STAMP = "test"
_SCOPE = "test-scope"


@pytest.fixture(scope="module")
def anyio_backend() -> str:
    return "asyncio"


class _Request(Request):
    """Минимальный Request: metadata и отметка версии."""

    def __init__(self, page: str, version: int = 1, body: str = "") -> None:
        self.page = page
        self.version = version
        self.body = body or page

    @property
    def metadata(self) -> Metadata:
        return Metadata.empty()

    @property
    def mark(self) -> SourceMark:
        return SourceMark(fingerprint=f"v{self.version}")


class _Source(RequestSource[_Request]):
    def __init__(self, requests: Sequence[_Request]) -> None:
        self._requests = list(requests)

    async def requests(self) -> AsyncIterator[_Request]:
        for request in self._requests:
            yield request


class _SlowTransport(Transport[_Request]):
    """Сетевая задержка на источник; считает пик одновременных загрузок."""

    def __init__(self, delay: float) -> None:
        self._delay = delay
        self.in_flight = 0
        self.peak = 0
        self.fetched: list[str] = []

    def source_id(self, request: _Request) -> SourceId:
        return SourceId(f"page:{request.page}")

    async def fetch(self, request: _Request) -> AsyncIterator[RawDocument]:
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        self.fetched.append(request.page)
        try:
            await asyncio.sleep(self._delay)
            yield RawDocument(
                handle=ChunkStream.of(request.body.encode()),
                source_id=self.source_id(request),
                metadata=Metadata.empty().set(
                    TransportKeys.BODY_HASH, f"hash:{request.body}"
                ),
            )
        finally:
            self.in_flight -= 1


class _Reader(Reader[str]):
    def __init__(self) -> None:
        self.read_pages: list[SourceId] = []

    def reader_id(self) -> ReaderId:
        return ReaderId("test.reader")

    async def read(self, raw: RawDocument) -> AsyncIterator[Section[str]]:
        self.read_pages.append(raw.source_id)
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


class _Sink(IndexSink[str]):
    """Считает принятые чанки; failing роняет источник как store."""

    def __init__(self, failing: frozenset[str] = frozenset()) -> None:
        self.accepted: list[SourceId] = []
        self.forgotten: list[SourceId] = []
        self._failing = failing

    async def reconcile(self, chunks: AsyncIterable[Chunk[str]]) -> ReconcileSummary:
        total = 0
        async for chunk in chunks:
            if str(chunk.source_id) in self._failing:
                raise IndexingError(f"store rejected {chunk.source_id}")
            self.accepted.append(chunk.source_id)
            total += 1
        return ReconcileSummary(total=total, upserted=total, unchanged=0)

    async def forget(self, source_id: SourceId, *, from_index: int) -> int:
        if from_index > 0:
            return 0

        self.forgotten.append(source_id)
        return 1


class _Ledger(SourceLedger):
    def __init__(self) -> None:
        self.records: dict[SourceId, SourceRecord] = {}

    async def lookup(self, source_id: SourceId) -> SourceRecord | None:
        return self.records.get(source_id)

    async def touch(
        self, source_ids: Sequence[SourceId], *, at: float, scope: RunScope
    ) -> None:
        for source_id in source_ids:
            record = self.records.get(source_id)
            if record is None:
                continue

            owned = scope.scope or record.scope
            self.records[source_id] = replace(
                record, seen_at=at, seen_run=scope.run, scope=owned
            )

    async def record(self, record: SourceRecord) -> None:
        self.records[record.source_id] = record

    async def orphans(self, run: str) -> AsyncIterator[SourceRecord]:
        for record in list(self.records.values()):
            if record.parent is None or record.seen_run == run:
                continue

            parent = self.records.get(record.parent)
            if parent is None or parent.seen_run != run:
                continue

            yield record

    async def unseen_roots(self, scope: str, run: str) -> AsyncIterator[SourceRecord]:
        for record in list(self.records.values()):
            if record.parent is not None:
                continue

            if record.scope != scope or record.seen_run == run:
                continue

            yield record

    async def children(self, parent: SourceId) -> AsyncIterator[SourceRecord]:
        for record in list(self.records.values()):
            if record.parent == parent:
                yield record

    async def forget(self, source_id: SourceId) -> None:
        self.records.pop(source_id, None)


class _Probe(SourceProbe):
    """Всё невиденное считается исчезнувшим; помнит, сколько раз спрашивали."""

    def __init__(self) -> None:
        self.batches: list[int] = []

    async def gone(self, records: Sequence[SourceRecord]) -> Sequence[SourceId]:
        self.batches.append(len(records))
        gone: list[SourceId] = []
        for record in records:
            gone.append(record.source_id)

        return gone


class _Stand:
    def __init__(
        self, transport: _SlowTransport, failing: frozenset[str] = frozenset()
    ):
        self.transport = transport
        self.reader = _Reader()
        self.sink = _Sink(failing)
        self.ledger = _Ledger()
        self.probe = _Probe()

    def pipeline(self, requests: Sequence[_Request]) -> Pipeline[_Request, str]:
        return Pipeline(
            source=_Source(requests),
            transport=self.transport,
            reader=self.reader,
            ledger=self.ledger,
            probe=self.probe,
        )

    async def run(self, requests: Sequence[_Request], *, workers: int) -> list[object]:
        config: IndexerConfig[str] = IndexerConfig(
            workers=workers, stamp=_STAMP, scope=_SCOPE
        )
        stream = self.pipeline(requests).index(
            chunker=_Chunker(), sink=self.sink, config=config
        )
        return [item async for item in stream]


def _pages(count: int, version: int = 1, body: str = "") -> list[_Request]:
    return [_Request(str(i), version, body) for i in range(count)]


class TestParallelSources:
    async def test_sources_overlap_in_flight(self) -> None:
        """Обход идёт внахлёст: страницы не ждут друг друга по очереди."""
        stand = _Stand(_SlowTransport(_FETCH_DELAY_SEC))

        started = time.monotonic()
        await stand.run(_pages(_PAGES), workers=4)
        elapsed = time.monotonic() - started

        sequential = _PAGES * _FETCH_DELAY_SEC
        if elapsed >= sequential / 2:
            raise AssertionError("elapsed < sequential / 2")
        if stand.transport.peak <= 1:
            raise AssertionError("transport.peak > 1")
        if len(stand.sink.accepted) != _PAGES:
            raise AssertionError("len(sink.accepted) == _PAGES")

    async def test_workers_bound_is_respected(self) -> None:
        """Больше config.workers источников в полёте быть не должно."""
        stand = _Stand(_SlowTransport(_FETCH_DELAY_SEC))

        await stand.run(_pages(_PAGES), workers=3)

        if stand.transport.peak > 3:
            raise AssertionError("transport.peak <= 3")

    async def test_serial_run_keeps_one_in_flight(self) -> None:
        """workers=1 — прежнее последовательное поведение."""
        stand = _Stand(_SlowTransport(0.0))

        await stand.run(_pages(4), workers=1)

        if stand.transport.peak != 1:
            raise AssertionError("transport.peak == 1")


class TestLedger:
    async def test_unchanged_fingerprint_skips_fetch(self) -> None:
        stand = _Stand(_SlowTransport(0.0))
        await stand.run(_pages(4), workers=2)
        stand.transport.fetched.clear()

        events = await stand.run(_pages(4), workers=2)

        skipped = [e for e in events if isinstance(e, SourceSkippedUnchanged)]
        if len(skipped) != 4:
            raise AssertionError("every source skipped by fingerprint")
        if stand.transport.fetched:
            raise AssertionError(f"nothing fetched: {stand.transport.fetched}")

    async def test_new_version_with_same_body_is_not_parsed(self) -> None:
        stand = _Stand(_SlowTransport(0.0))
        await stand.run(_pages(2), workers=2)
        stand.reader.read_pages.clear()

        await stand.run(_pages(2, version=2), workers=2)

        if len(stand.transport.fetched) != 4:
            raise AssertionError("new version is fetched again")
        if stand.reader.read_pages:
            raise AssertionError(
                f"same body must not be parsed: {stand.reader.read_pages}"
            )
        if stand.ledger.records[SourceId("page:0")].fingerprint != "v2":
            raise AssertionError("fingerprint follows the new version")

    async def test_other_stamp_reindexes(self) -> None:
        stand = _Stand(_SlowTransport(0.0))
        await stand.run(_pages(2), workers=2)
        stand.reader.read_pages.clear()

        config: IndexerConfig[str] = IndexerConfig(
            workers=2, stamp="other", scope=_SCOPE
        )
        stream = stand.pipeline(_pages(2)).index(
            chunker=_Chunker(), sink=stand.sink, config=config
        )
        _ = [item async for item in stream]

        if len(stand.reader.read_pages) != 2:
            raise AssertionError("other stamp must parse everything again")


class TestRunOutcome:
    async def test_stats_count_every_source(self) -> None:
        stand = _Stand(_SlowTransport(0.0))

        events = await stand.run(_pages(_PAGES), workers=4)

        [finished] = [e for e in events if isinstance(e, RunFinished)]
        roots = finished.stats.roots
        if roots.seen != _PAGES:
            raise AssertionError(f"every source seen: {roots}")
        if roots.indexed != _PAGES:
            raise AssertionError(f"every source indexed: {roots}")
        if roots.chunks_upserted != _PAGES:
            raise AssertionError(f"a chunk per source: {roots}")
        if finished.stats.children.seen != 0:
            raise AssertionError("no children in this run")

    async def test_unseen_roots_are_probed_after_all_sources(self) -> None:
        """Исчезнувшее снимается строго последним, после всех источников."""
        stand = _Stand(_SlowTransport(0.0))
        await stand.run(_pages(4), workers=4)

        events = await stand.run(_pages(2), workers=4)

        gone = [e for e in events if isinstance(e, SourceGone)]
        if sorted(str(e.source_id) for e in gone) != ["page:2", "page:3"]:
            raise AssertionError(f"unseen pages must go: {gone}")
        for event in gone:
            if event.kind is not SourceKind.ROOT:
                raise AssertionError(f"pages are root sources: {event}")
        if stand.probe.batches != [2]:
            raise AssertionError(f"one probe batch of two: {stand.probe.batches}")

        cleanup_at = next(
            i for i, e in enumerate(events) if isinstance(e, CleanupStarted)
        )
        first_gone = next(i for i, e in enumerate(events) if isinstance(e, SourceGone))
        if first_gone < cleanup_at:
            raise AssertionError("gone events come after cleanup start")
        if not isinstance(events[-1], RunFinished):
            raise AssertionError("run finished last")
        if sorted(str(s) for s in stand.sink.forgotten) != ["page:2", "page:3"]:
            raise AssertionError(
                f"chunks of gone pages forgotten: {stand.sink.forgotten}"
            )

    async def test_failed_source_does_not_stop_others(self) -> None:
        stand = _Stand(_SlowTransport(0.0), failing=frozenset({"page:2"}))

        events = await stand.run(_pages(4), workers=4)

        failed = [e for e in events if isinstance(e, SourceFailed)]
        if [str(e.source_id) for e in failed] != ["page:2"]:
            raise AssertionError('[str(e.source_id) for e in failed] == ["page:2"]')
        if len(stand.sink.accepted) != 3:
            raise AssertionError("len(sink.accepted) == 3")

    async def test_failed_source_is_kept_by_cleanup(self) -> None:
        stand = _Stand(_SlowTransport(0.0))
        await stand.run(_pages(3), workers=2)
        stand.sink = _Sink(failing=frozenset({"page:1"}))

        events = await stand.run(_pages(3, version=2, body="changed"), workers=2)

        failed = [e for e in events if isinstance(e, SourceFailed)]
        if len(failed) != 1:
            raise AssertionError(f"one failed source: {failed}")
        gone = [e for e in events if isinstance(e, SourceGone)]
        if gone:
            raise AssertionError(f"failed source must not be treated as gone: {gone}")

    async def test_workers_below_one_rejected(self) -> None:
        with pytest.raises(ValueError, match="workers"):
            IndexerConfig(workers=0, stamp=_STAMP)
