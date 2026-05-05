"""ChromadbPersistStore: StreamSink[Chunk] поверх PersistentClient."""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Any

from boba.indexing import (
    Chunk,
    ChunkSummary,
    CollectionInfo,
    IndexingContext,
    SearchHit,
    Store,
    StoreId,
)

__all__ = ["ChromadbPersistStore"]

logger = logging.getLogger(__name__)

_BATCH_SIZE = 256


class ChromadbPersistStore(Store):
    """Локальный persistent ChromaDB store.

    Буферизирует Chunk'и per-collection и сбрасывает batched-upsert'ом
    при заполнении или явном `flush()`.
    """

    def __init__(
        self,
        persist_path: str,
        *,
        embedding_function: Any = None,
    ) -> None:
        # ленивый импорт chromadb — не падать при импорте пакета без deps
        import chromadb  # noqa: PLC0415

        self._client = chromadb.PersistentClient(path=persist_path)
        self._embedding_function = embedding_function
        self._buffer: dict[str, list[Chunk]] = {}
        logger.info(
            "ChromadbPersistStore opened persist_path=%r ef=%s",
            persist_path,
            type(embedding_function).__name__ if embedding_function else "default",
        )

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
            embedding_function=self._embedding_function,
        )

    def _get_collection(self, name: str):  # type: ignore[no-untyped-def]
        return self._client.get_collection(
            name=name,
            embedding_function=self._embedding_function,
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
        col = self._get_collection(ctx.collection)
        existing = col.get(where={"source_id": source_id})
        ids = existing.get("ids") or []
        if not ids:
            return 0
        col.delete(ids=ids)
        return len(ids)

    def peek_chunks(
        self,
        ctx: IndexingContext,
        *,
        source_id: str | None = None,
        limit: int = 20,
        snippet_chars: int = 200,
    ) -> Iterable[ChunkSummary]:
        try:
            col = self._get_collection(ctx.collection)
        except Exception as e:
            logger.warning("peek_chunks: collection not found: %s", e)
            return
        kwargs: dict[str, object] = {
            "include": ["metadatas", "documents"],
            "limit": limit,
        }
        if source_id:
            kwargs["where"] = {"source_id": source_id}
        result = col.get(**kwargs)  # type: ignore[arg-type]
        ids = result.get("ids") or []
        metas = result.get("metadatas") or []
        docs = result.get("documents") or []
        items = list(zip(ids, metas, docs, strict=False))
        if source_id:
            items.sort(key=lambda t: _meta_int(t[1], "chunk_index"))
        for cid, raw_meta, doc in items:
            meta = raw_meta or {}
            yield ChunkSummary(
                chunk_id=str(cid),
                source_id=str(meta.get("source_id") or ""),
                anchor=_meta_str_or_none(meta, "anchor"),
                chunk_index=_meta_int(meta, "chunk_index"),
                snippet=_truncate(str(doc or ""), snippet_chars),
                metadata={k: str(v) for k, v in meta.items()},
            )

    def search(
        self,
        ctx: IndexingContext,
        *,
        query: str,
        top_k: int = 5,
        snippet_chars: int = 200,
    ) -> Iterable[SearchHit]:
        try:
            col = self._get_collection(ctx.collection)
        except Exception as e:
            logger.warning("search: collection not found: %s", e)
            return
        raw = col.query(query_texts=[query], n_results=top_k)
        ids = (raw.get("ids") or [[]])[0]
        documents = (raw.get("documents") or [[]])[0]
        metadatas = (raw.get("metadatas") or [[]])[0]
        distances = (raw.get("distances") or [[]])[0]
        for i, doc_id in enumerate(ids):
            doc = documents[i] if i < len(documents) else ""
            md = metadatas[i] if i < len(metadatas) else None
            dist = distances[i] if i < len(distances) else 0.0
            yield SearchHit(
                id=str(doc_id),
                distance=float(dist),
                snippet=_truncate(str(doc or ""), snippet_chars),
                metadata={k: str(v) for k, v in (md or {}).items()},
            )

    def embedding_dim(self, ctx: IndexingContext) -> int:
        try:
            col = self._get_collection(ctx.collection)
        except Exception:
            return 0
        result = col.get(limit=1, include=["embeddings"])
        embs = result.get("embeddings")
        if embs is None or len(embs) == 0:
            return 0
        first = embs[0]
        if first is None:
            return 0
        return len(first)

    def list_source_ids(self, ctx: IndexingContext) -> Iterable[str]:
        col = self._get_collection(ctx.collection)
        existing = col.get(include=["metadatas"])
        out: set[str] = set()
        for m in existing.get("metadatas") or []:
            sid = m.get("source_id")
            if isinstance(sid, str) and sid:
                out.add(sid)
        return out

    def content_hash_for(
        self, ctx: IndexingContext, source_id: str
    ) -> str:
        """Достать content_hash из metadata любого чанка этого source_id.

        Все чанки одного source_id пишутся одной транзакцией с одним hash'ом,
        поэтому достаточно прочитать один.
        """
        try:
            col = self._get_collection(ctx.collection)
        except Exception:
            return ""
        result = col.get(
            where={"source_id": source_id},
            include=["metadatas"],
            limit=1,
        )
        metas = result.get("metadatas") or []
        if not metas:
            return ""
        h = metas[0].get("content_hash")
        return str(h) if isinstance(h, (str, int)) else ""

    def list_collections(self) -> Iterable[CollectionInfo]:
        return [self._summarize(c) for c in self._client.list_collections()]

    def collection_info(self, name: str) -> CollectionInfo:
        return self._summarize(self._get_collection(name))

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
        col = self._get_collection(collection_name)
        ids = [c.chunk_id for c in chunks]
        documents = [c.text for c in chunks]
        metadatas = [_meta(c) for c in chunks]
        col.upsert(
            ids=ids,
            documents=documents,
            metadatas=metadatas,  # pyright: ignore[reportArgumentType]
        )


def _meta_int(meta: object, key: str) -> int:
    if not isinstance(meta, dict):
        return 0
    raw = meta.get(key)
    try:
        return int(raw) if raw is not None else 0
    except (TypeError, ValueError):
        return 0


def _meta_str_or_none(meta: object, key: str) -> str | None:
    if not isinstance(meta, dict):
        return None
    v = meta.get(key)
    return str(v) if v else None


def _truncate(text: str, max_chars: int) -> str:
    text = text.strip()
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1].rstrip() + "…"


def _meta(c: Chunk) -> dict[str, str]:
    """Build metadata dict с source_id/anchor/chunk_index/content_hash."""
    out: dict[str, str] = {
        **c.metadata,
        "source_id": c.source_id,
        "chunk_index": str(c.chunk_index),
    }
    if c.anchor:
        out["anchor"] = c.anchor
    if c.content_hash:
        out["content_hash"] = c.content_hash
    return out
