"""Границы прогона ingest для тестов: хранилище и реестр в памяти, нулевой эмбеддер.

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
    SourceLedger,
    SourceRecord,
)
from boba.indexing.ports import Embedder

__all__ = ["MemoryChunkStore", "MemorySourceLedger", "TextReader", "ZeroEmbedder"]


class MemorySourceLedger(SourceLedger):
    """Реестр источников в памяти: граница postgres для быстрых тестов."""

    def __init__(self) -> None:
        self.records: dict[SourceId, SourceRecord] = {}

    async def lookup(self, source_id: SourceId) -> SourceRecord | None:
        return self.records.get(source_id)

    async def touch(self, source_ids: Sequence[SourceId], *, at: float) -> None:
        for source_id in source_ids:
            record = self.records.get(source_id)
            if record is None:
                continue

            self.records[source_id] = SourceRecord(
                source_id=record.source_id,
                parent=record.parent,
                fingerprint=record.fingerprint,
                content_hash=record.content_hash,
                grade=record.grade,
                stamp=record.stamp,
                seen_at=at,
                indexed_at=record.indexed_at,
            )

    async def record(self, record: SourceRecord) -> None:
        self.records[record.source_id] = record

    async def unseen(self, *, before: float) -> AsyncIterator[SourceRecord]:
        for record in list(self.records.values()):
            if record.seen_at < before:
                yield record

    async def children(self, parent: SourceId) -> AsyncIterator[SourceRecord]:
        for record in list(self.records.values()):
            if record.parent == parent:
                yield record

    async def forget(self, source_id: SourceId) -> None:
        self.records.pop(source_id, None)


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

    async def delete_by_source(
        self,
        collection: CollectionId,
        source_id: SourceId,
        *,
        from_index: int,
    ) -> int:
        doomed: list[ChunkId] = []
        for chunk in self.chunks.values():
            if chunk.source_id != source_id:
                continue

            if chunk.chunk_index < from_index:
                continue

            doomed.append(chunk.chunk_id)

        for chunk_id in doomed:
            del self.chunks[chunk_id]

        return len(doomed)


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
