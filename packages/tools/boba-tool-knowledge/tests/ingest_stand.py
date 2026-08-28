"""Границы прогона ingest для тестов: хранилище в памяти и нулевой эмбеддер.

Всё остальное в тестах остаётся настоящим — Pipeline, транспорт Confluence,
чанкер и обёртки наблюдения.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterable, Mapping, Sequence

from boba.indexing import (
    Chunk,
    ChunkId,
    ChunkStore,
    ChunkSummary,
    CollectionId,
    ContentHash,
    EmbeddedChunk,
    Filter,
    HashDiff,
    RawDocument,
    Reader,
    ReaderId,
    Section,
    SourceId,
)
from boba.indexing.ports import Embedder

__all__ = ["MemoryChunkStore", "TextReader", "ZeroEmbedder"]


class MemoryChunkStore(ChunkStore[str]):
    """Хранилище чанков в памяти: граница postgres, всё остальное настоящее."""

    def __init__(self) -> None:
        self.chunks: dict[ChunkId, EmbeddedChunk[str]] = {}

    async def get_by_ids(
        self,
        collection: CollectionId,
        chunk_ids: Iterable[ChunkId],
    ) -> Sequence[Chunk[str]]:
        found: list[Chunk[str]] = []
        for chunk_id in chunk_ids:
            stored = self.chunks.get(chunk_id)
            if stored is None:
                continue

            found.append(
                Chunk(
                    chunk_id=stored.chunk_id,
                    source_id=stored.source_id,
                    format_content=stored.format_content,
                    raw_content=stored.raw_content,
                    chunk_index=stored.chunk_index,
                    content_hash=stored.content_hash,
                    metadata=stored.metadata,
                    tags=stored.tags,
                )
            )

        return found

    async def peek(
        self,
        collection: CollectionId,
        *,
        source_id: SourceId | None,
        limit: int,
    ) -> Sequence[ChunkSummary[str]]:
        return []

    async def find(
        self,
        collection: CollectionId,
        *,
        where: Filter | None,
        limit: int | None = None,
    ) -> Sequence[ChunkSummary[str]]:
        return []

    async def diff_by_hash(
        self,
        collection: CollectionId,
        candidates: Iterable[tuple[ChunkId, ContentHash]],
    ) -> HashDiff:
        to_upsert: list[ChunkId] = []
        unchanged: list[ChunkId] = []
        for chunk_id, content_hash in candidates:
            stored = self.chunks.get(chunk_id)
            if stored is not None and stored.content_hash == content_hash:
                unchanged.append(chunk_id)
                continue

            to_upsert.append(chunk_id)

        return HashDiff(to_upsert=to_upsert, unchanged=unchanged)

    async def upsert(
        self,
        collection: CollectionId,
        chunks: Iterable[EmbeddedChunk[str]],
    ) -> None:
        for chunk in chunks:
            self.chunks[chunk.chunk_id] = chunk

    async def update_metadata(
        self,
        collection: CollectionId,
        chunk_ids: Iterable[ChunkId],
        patch: Mapping[str, str | int | float | bool],
    ) -> None:
        return None

    async def delete(
        self,
        collection: CollectionId,
        chunk_ids: Iterable[ChunkId],
    ) -> None:
        for chunk_id in chunk_ids:
            self.chunks.pop(chunk_id, None)


class ZeroEmbedder(Embedder[str]):
    """Граница модели: вектор нужного размера без загрузки эмбеддера."""

    DIM = 4

    async def embed_documents(
        self,
        contents: Sequence[str],
    ) -> Sequence[Sequence[float]]:
        vectors: list[Sequence[float]] = []
        for _content in contents:
            vectors.append([0.0] * self.DIM)

        return vectors

    async def embed_query(self, content: str) -> Sequence[float]:
        return [0.0] * self.DIM

    def dim(self) -> int:
        return self.DIM


class TextReader(Reader[str]):
    """Одна секция на документ: разбор HTML тут не проверяется."""

    async def read(self, value: RawDocument) -> AsyncIterator[Section[str]]:
        payload = await value.handle.read()
        yield Section(
            source_id=value.source_id,
            content=payload.decode("utf-8", errors="replace"),
            order=0,
            metadata=value.metadata,
        )

    def reader_id(self) -> ReaderId:
        return ReaderId("test.text")
