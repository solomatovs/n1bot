"""Наблюдательные обёртки прогона: логируют, но остаются ленивыми."""

from __future__ import annotations

import logging
from collections.abc import Iterable, Iterator
from io import BytesIO

import pytest

from boba.indexing import (
    Chunk,
    Chunker,
    ChunkerId,
    ChunkId,
    Metadata,
    RawDocument,
    Reader,
    ReaderId,
    Section,
    SourceId,
)
from boba.indexing.values import StringContentHash
from boba.tool.kb.indexing_log import LoggingChunker, LoggingReader

_SOURCE = SourceId("https://confl/download/attachments/42/report.pdf")


@pytest.fixture(autouse=True)
def chainlit_context() -> None:
    pass


class _CountingReader(Reader[str]):
    """Reader, который считает, сколько секций у него уже забрали."""

    def __init__(self, total: int) -> None:
        self.emitted = 0
        self._total = total

    def reader_id(self) -> ReaderId:
        return ReaderId("test.counting")

    def read(self, value: RawDocument) -> Iterable[Section[str]]:
        for order in range(self._total):
            self.emitted += 1
            yield Section(
                source_id=value.source_id,
                content=f"section {order}",
                order=order,
                metadata=value.metadata,
            )


class _CountingChunker(Chunker[str]):
    """Chunker, который считает, сколько чанков у него уже забрали."""

    def __init__(self) -> None:
        self.emitted = 0

    def chunker_id(self) -> ChunkerId:
        return ChunkerId("test.counting")

    def chunk(self, sections: Iterable[Section[str]]) -> Iterator[Chunk[str]]:
        for index, section in enumerate(sections):
            self.emitted += 1
            yield Chunk(
                chunk_id=ChunkId(f"c{index}"),
                source_id=section.source_id,
                format_content=section.content,
                raw_content=section.content,
                chunk_index=index,
                content_hash=StringContentHash(f"h{index}"),
            )


def _raw() -> RawDocument:
    return RawDocument(
        handle=BytesIO(b""),
        source_id=_SOURCE,
        metadata=Metadata.empty(),
    )


def _sections(count: int) -> Iterator[Section[str]]:
    for order in range(count):
        yield Section(
            source_id=_SOURCE,
            content=f"section {order}",
            order=order,
            metadata=Metadata.empty(),
        )


class TestLoggingReader:
    def test_stays_lazy(self) -> None:
        inner = _CountingReader(total=10)
        stream = iter(LoggingReader(inner, logging.getLogger("test")).read(_raw()))
        next(stream)
        # обёртка не имеет права вычитать ридер вперёд потребителя
        assert inner.emitted == 1

    def test_passes_every_section_through(self) -> None:
        inner = _CountingReader(total=4)
        reader = LoggingReader(inner, logging.getLogger("test"))
        assert len(list(reader.read(_raw()))) == 4

    def test_keeps_inner_reader_id(self) -> None:
        inner = _CountingReader(total=1)
        reader = LoggingReader(inner, logging.getLogger("test"))
        assert reader.reader_id() == inner.reader_id()


class TestLoggingChunker:
    def test_stays_lazy(self) -> None:
        inner = _CountingChunker()
        chunker = LoggingChunker(inner, logging.getLogger("test"))
        stream = iter(chunker.chunk(_sections(10)))
        next(stream)
        assert inner.emitted == 1

    def test_ticks_every_n_chunks(self, caplog: pytest.LogCaptureFixture) -> None:
        chunker = LoggingChunker(_CountingChunker(), logging.getLogger("test"))
        with caplog.at_level(logging.INFO, logger="test"):
            produced = list(chunker.chunk(_sections(LoggingChunker.EVERY * 2)))
        ticks = [r for r in caplog.records if "chunks so far" in r.getMessage()]
        assert len(produced) == LoggingChunker.EVERY * 2
        assert len(ticks) == 2

    def test_keeps_inner_chunker_id(self) -> None:
        inner = _CountingChunker()
        chunker = LoggingChunker(inner, logging.getLogger("test"))
        assert chunker.chunker_id() == inner.chunker_id()
