"""Наблюдательные обёртки прогона: логируют, но остаются ленивыми."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterable, AsyncIterator, Iterable

import pytest

from boba.indexing import (
    Chunk,
    Chunker,
    ChunkerId,
    ChunkId,
    ChunkStream,
    Metadata,
    RawDocument,
    Reader,
    ReaderId,
    Section,
    SourceId,
)
from boba.indexing.values import StringContentHash
from boba.tool.kb.indexing_log import (
    IngestProgress,
    LoggingChunker,
    LoggingReader,
)

pytestmark = pytest.mark.anyio


async def _astream(
    items: Iterable[Section[str]],
) -> AsyncIterator[Section[str]]:
    """Готовые секции как поток — вход чанкера только асинхронный."""
    for item in items:
        yield item


_SOURCE = SourceId("https://confl/download/attachments/42/report.pdf")


@pytest.fixture(scope="module")
def anyio_backend() -> str:
    return "asyncio"


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

    async def read(self, value: RawDocument) -> AsyncIterator[Section[str]]:
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

    async def chunk(
        self,
        sections: AsyncIterable[Section[str]],
    ) -> AsyncIterator[Chunk[str]]:
        index = 0
        async for section in sections:
            self.emitted += 1
            yield Chunk(
                chunk_id=ChunkId(f"c{index}"),
                source_id=section.source_id,
                format_content=section.content,
                raw_content=section.content,
                chunk_index=index,
                content_hash=StringContentHash(f"h{index}"),
            )
            index += 1


def _progress() -> IngestProgress:
    return IngestProgress(logging.getLogger("test"))


def _raw() -> RawDocument:
    return RawDocument(
        handle=ChunkStream.of(b""),
        source_id=_SOURCE,
        metadata=Metadata.empty(),
    )


def _sections(count: int) -> AsyncIterator[Section[str]]:
    return _astream(
        Section(
            source_id=_SOURCE,
            content=f"section {order}",
            order=order,
            metadata=Metadata.empty(),
        )
        for order in range(count)
    )


class TestLoggingReader:
    async def test_stays_lazy(self) -> None:
        inner = _CountingReader(total=10)
        stream = LoggingReader(inner, logging.getLogger("test")).read(_raw())
        await anext(stream)
        # обёртка не имеет права вычитать ридер вперёд потребителя
        if inner.emitted != 1:
            raise AssertionError("inner.emitted == 1")

    async def test_passes_every_section_through(self) -> None:
        inner = _CountingReader(total=4)
        reader = LoggingReader(inner, logging.getLogger("test"))
        sections = [section async for section in reader.read(_raw())]
        if len(sections) != 4:
            raise AssertionError("len(sections) == 4")

    def test_keeps_inner_reader_id(self) -> None:
        inner = _CountingReader(total=1)
        reader = LoggingReader(inner, logging.getLogger("test"))
        if reader.reader_id() != inner.reader_id():
            raise AssertionError("reader.reader_id() == inner.reader_id()")


class TestLoggingChunker:
    async def test_stays_lazy(self) -> None:
        inner = _CountingChunker()
        chunker = LoggingChunker(inner, logging.getLogger("test"), _progress())
        stream = chunker.chunk(_sections(10))
        await anext(stream)
        if inner.emitted != 1:
            raise AssertionError("inner.emitted == 1")

    async def test_ticks_every_n_chunks(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        chunker = LoggingChunker(
            _CountingChunker(), logging.getLogger("test"), _progress()
        )
        with caplog.at_level(logging.INFO, logger="test"):
            produced = [
                item
                async for item in chunker.chunk(
                    _sections(LoggingChunker.EVERY * 2),
                )
            ]
        ticks = [r for r in caplog.records if "chunks so far" in r.getMessage()]
        if len(produced) != LoggingChunker.EVERY * 2:
            raise AssertionError("len(produced) == LoggingChunker.EVERY * 2")
        if len(ticks) != 2:
            raise AssertionError("len(ticks) == 2")

    def test_keeps_inner_chunker_id(self) -> None:
        inner = _CountingChunker()
        chunker = LoggingChunker(inner, logging.getLogger("test"), _progress())
        if chunker.chunker_id() != inner.chunker_id():
            raise AssertionError("chunker.chunker_id() == inner.chunker_id()")
