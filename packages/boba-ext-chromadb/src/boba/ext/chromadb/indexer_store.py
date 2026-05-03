"""ChromadbPersistStore: StreamSink[Chunk] поверх PersistentClient."""

from __future__ import annotations

import logging
from collections.abc import Iterable

from boba.ext.chromadb.config import ChromaExtConfig
from boba.indexing import (
    Chunk,
    CollectionInfo,
    IndexerExtensionContext,
    IndexingContext,
    Store,
    StoreFactory,
    StoreId,
)

__all__ = ["ChromadbPersistStore", "ChromadbPersistStoreFactory"]

logger = logging.getLogger(__name__)


class ChromadbPersistStore(Store):
    """Локальный persistent ChromaDB store.

    Буферизирует Chunk'и и сбрасывает в `flush()`. Per-collection кеш
    Chroma-collection объектов внутри одного экземпляра Store.
    """

    def __init__(self, persist_path: str) -> None:
        # ленивый импорт chromadb — не падать при импорте пакета без deps
        import chromadb  # noqa: PLC0415

        self._client = chromadb.PersistentClient(path=persist_path)
        # buffer: per-collection, чтобы flush мог группировать upsert.
        self._buffer: dict[str, list[Chunk]] = {}
        logger.info("ChromadbPersistStore opened persist_path=%r", persist_path)

    def name(self) -> str:
        return "ChromadbPersistStore"

    def store_id(self) -> StoreId:
        return StoreId("ext.chromadb_persist")

    def reset(self) -> None:
        self._buffer.clear()

    def ensure_target(
        self, ctx: IndexingContext, description: str | None
    ) -> None:
        existing = {c.name for c in self._client.list_collections()}
        if ctx.collection in existing:
            return
        metadata: dict[str, str] = {}
        if description:
            metadata["description"] = description
        self._client.create_collection(
            name=ctx.collection,
            metadata=metadata or None,
        )

    def handle(self, ctx: IndexingContext, event: Chunk) -> None:
        bucket = self._buffer.setdefault(ctx.collection, [])
        bucket.append(event)
        if len(bucket) >= _BATCH_SIZE:
            self._upsert_to(ctx.collection, bucket)
            bucket.clear()

    def flush(self, ctx: IndexingContext) -> None:
        del ctx
        for collection_name, chunks in list(self._buffer.items()):
            if chunks:
                self._upsert_to(collection_name, chunks)
        self._buffer.clear()

    def delete_by_source(
        self, ctx: IndexingContext, source_id: str
    ) -> int:
        col = self._client.get_collection(name=ctx.collection)
        existing = col.get(where={"source_id": source_id})
        ids = existing.get("ids") or []
        if not ids:
            return 0
        col.delete(ids=ids)
        return len(ids)

    def list_source_ids(self, ctx: IndexingContext) -> Iterable[str]:
        col = self._client.get_collection(name=ctx.collection)
        existing = col.get(include=["metadatas"])
        out: set[str] = set()
        for m in existing.get("metadatas") or []:
            sid = m.get("source_id")
            if isinstance(sid, str) and sid:
                out.add(sid)
        return out

    def list_collections(self) -> Iterable[CollectionInfo]:
        return [self._summarize(c) for c in self._client.list_collections()]

    def collection_info(self, name: str) -> CollectionInfo:
        return self._summarize(self._client.get_collection(name=name))

    def delete_collection(self, name: str) -> None:
        self._client.delete_collection(name=name)

    def _summarize(self, c) -> CollectionInfo:  # type: ignore[no-untyped-def]
        metadata = c.metadata or {}
        description = metadata.get("description", "") if metadata else ""
        try:
            count = c.count()
        except Exception as e:
            logger.warning("count() failed for %r: %s", c.name, e)
            count = -1
        return CollectionInfo(
            name=c.name,
            description=description if isinstance(description, str) else "",
            count=count,
        )

    def _upsert_to(self, collection_name: str, chunks: list[Chunk]) -> None:
        col = self._client.get_collection(name=collection_name)
        ids = [c.chunk_id for c in chunks]
        documents = [c.text for c in chunks]
        metadatas = [_meta(c) for c in chunks]
        col.upsert(
            ids=ids,
            documents=documents,
            metadatas=metadatas,  # pyright: ignore[reportArgumentType]
        )


_BATCH_SIZE = 256


def _meta(c: Chunk) -> dict[str, str]:
    """Build metadata dict с source_id/anchor/chunk_index."""
    out: dict[str, str] = {
        **c.metadata,
        "source_id": c.source_id,
        "chunk_index": str(c.chunk_index),
    }
    if c.anchor:
        out["anchor"] = c.anchor
    return out


class ChromadbPersistStoreFactory(StoreFactory):
    """Читает [ext.chromadb] и собирает ChromadbPersistStore."""

    def id(self) -> StoreId:
        return StoreId("ext.chromadb_persist")

    def produce(self, ctx: IndexerExtensionContext) -> Store:
        from boba.ext.chromadb.config import ChromadbSection  # noqa: PLC0415

        cfg: ChromaExtConfig = ctx.config.section(ChromadbSection)
        if not cfg.persist_path:
            msg = "ext.chromadb.persist_path is required for ChromadbPersistStore"
            raise ValueError(msg)
        return ChromadbPersistStore(cfg.persist_path)
