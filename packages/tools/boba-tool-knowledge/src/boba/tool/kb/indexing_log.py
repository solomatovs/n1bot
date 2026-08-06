"""Логирование прогона: IndexEvent-поток плюс наблюдательные обёртки стадий.

Pipeline эмитит события по завершении source'а, поэтому долгие стадии внутри
него (скачивание вложения, OCR, эмбеддинг) выглядят как зависание. Обёртки
здесь логируют стадию на входе — чтение и чанкование — не собирая поток:
и Reader, и Chunker остаются ленивыми генераторами.

Логов в boba-indexing/boba-text нет намеренно, поэтому наблюдение живёт
здесь, в слое инструмента.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterable, AsyncIterator, Sequence
from typing import ClassVar, Generic, TypeVar

from boba.indexing import (
    Chunk,
    Chunker,
    ChunkerId,
    CompletedItem,
    IndexEvent,
    IndexStats,
    IndexStatsBuilder,
    RawDocument,
    Reader,
    ReaderId,
    ReaderKeys,
    RunFinished,
    Section,
    Severity,
    TransportKeys,
)
from boba.indexing.ports import Embedder

__all__ = [
    "LoggedIndexRun",
    "LoggingChunker",
    "LoggingEmbedder",
    "LoggingReader",
]

T = TypeVar("T")


class LoggedIndexRun:
    """Слив IndexEvent-потока с per-event логированием; возвращает IndexStats."""

    _LEVELS: ClassVar[dict[Severity, int]] = {
        Severity.INFO: logging.INFO,
        Severity.WARN: logging.WARNING,
        Severity.ERROR: logging.ERROR,
    }

    @staticmethod
    async def drain(
        events: AsyncIterable[IndexEvent],
        logger: logging.Logger,
    ) -> IndexStats:
        """Потребить поток Pipeline.index(...), пишет каждое событие в logger."""
        stats = IndexStatsBuilder().build()
        async for event in events:
            LoggedIndexRun._emit(logger, event)
            if isinstance(event, RunFinished):
                stats = event.stats
        return stats

    @staticmethod
    def _emit(logger: logging.Logger, event: IndexEvent) -> None:
        """Одна log-строка на событие: headline() для item'ов, label() для фаз."""
        message = (
            event.headline() if isinstance(event, CompletedItem) else event.label()
        )
        logger.log(LoggedIndexRun._LEVELS[event.severity()], "%s", message)


class LoggingReader(Reader[T], Generic[T]):
    """Обёртка Reader'а: пишет, что за документ пошёл в разбор и чем кончился."""

    def __init__(self, inner: Reader[T], logger: logging.Logger) -> None:
        self._inner = inner
        self._logger = logger

    def reader_id(self) -> ReaderId:
        return self._inner.reader_id()

    async def read(self, value: RawDocument) -> AsyncIterator[Section[T]]:
        title = value.metadata.get(ReaderKeys.PAGE_TITLE) or ""
        content_type = value.metadata.get(TransportKeys.CONTENT_TYPE) or "?"
        self._logger.info(
            "read start: %s [%s] reader=%s src=%s",
            title,
            content_type,
            self._inner.reader_id(),
            value.source_id,
        )
        sections = 0
        async for section in self._inner.read(value):
            sections += 1
            yield section
        self._logger.info("read done: %s -> %d sections", title or "?", sections)


class LoggingEmbedder(Embedder[T], Generic[T]):
    """Обёртка Embedder'а: батч эмбеддится минуты, и это самая тихая стадия."""

    def __init__(self, inner: Embedder[T], logger: logging.Logger) -> None:
        self._inner = inner
        self._logger = logger

    async def embed_documents(
        self,
        contents: Sequence[T],
    ) -> Sequence[Sequence[float]]:
        self._logger.info("embedding %d chunks", len(contents))
        vectors = await self._inner.embed_documents(contents)
        self._logger.info("embedded %d chunks", len(vectors))
        return vectors

    async def embed_query(self, content: T) -> Sequence[float]:
        return await self._inner.embed_query(content)

    def dim(self) -> int:
        return self._inner.dim()


class LoggingChunker(Chunker[T], Generic[T]):
    """Обёртка Chunker'а: тик каждые EVERY чанков, чтобы прогресс был виден."""

    EVERY: ClassVar[int] = 25

    def __init__(self, inner: Chunker[T], logger: logging.Logger) -> None:
        self._inner = inner
        self._logger = logger

    def chunker_id(self) -> ChunkerId:
        return self._inner.chunker_id()

    async def chunk(
        self,
        sections: AsyncIterable[Section[T]],
    ) -> AsyncIterator[Chunk[T]]:
        produced = 0
        async for chunk in self._inner.chunk(sections):
            produced += 1
            if produced % self.EVERY == 0:
                self._logger.info(
                    "chunking: %d chunks so far, src=%s",
                    produced,
                    chunk.source_id,
                )
            yield chunk
        if produced:
            self._logger.info("chunking done: %d chunks", produced)
