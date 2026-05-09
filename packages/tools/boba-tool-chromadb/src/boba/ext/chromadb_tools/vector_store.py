"""
ChromaVectorStore — VectorStore + CollectionsAdmin.

Один класс реализует 4 ABC:
- VectorStoreReader[str]:   get_by_ids / similarity_search / peek
- VectorStoreWriter[str]:   upsert / delete
- CollectionsAdminReader:   list_collections / collection_info
- CollectionsAdminWriter:   ensure_collection / delete_collection

Chunk-payload в Chroma:
  document   = chunk.content (str)
  embedding  = embedder.embed_documents([content])
  metadata   = chunk.metadata.to_wire() ∪ reserved keys (_source_id, _anchor,
               _chunk_index, _loc_start, _loc_end, _content_hash)

При обратной сборке Chunk reserved keys фильтруются из business-Metadata —
поэтому ключи Metadata не должны начинаться с `_` (chroma не поддерживает
вложенные структуры).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from itertools import islice
from typing import Any, ClassVar

from chromadb.api import ClientAPI
from chromadb.api.models.Collection import Collection
from chromadb.api.types import Metadata as ChromaMetadata
from chromadb.api.types import PyEmbedding, Where

from boba.indexing.chunks import Chunk, ChunkId, ChunkLocation, ChunkSummary
from boba.indexing.content_hash import StringContentHash
from boba.indexing.context import CollectionId
from boba.indexing.embedder import Embedder
from boba.indexing.metadata import Metadata
from boba.indexing.sections import SourceId
from boba.indexing.vector_store import (
    CollectionInfo,
    CollectionsAdminReader,
    CollectionsAdminWriter,
    SearchHit,
    VectorStoreReader,
    VectorStoreWriter,
)

__all__ = ["ChromaVectorStore"]


class ChromaVectorStore(
    VectorStoreReader[str],
    VectorStoreWriter[str],
    CollectionsAdminReader,
    CollectionsAdminWriter,
):
    """Chroma-impl VectorStore[str] и CollectionsAdmin для индексации текстов."""

    DEFAULT_BATCH_SIZE: ClassVar[int] = 100

    KEY_SOURCE_ID: ClassVar[str] = "_source_id"
    KEY_ANCHOR: ClassVar[str] = "_anchor"
    KEY_CHUNK_INDEX: ClassVar[str] = "_chunk_index"
    KEY_LOC_START: ClassVar[str] = "_loc_start"
    KEY_LOC_END: ClassVar[str] = "_loc_end"
    KEY_CONTENT_HASH: ClassVar[str] = "_content_hash"
    DESCRIPTION_KEY: ClassVar[str] = "description"

    _RESERVED_KEYS: ClassVar[frozenset[str]] = frozenset(
        {
            KEY_SOURCE_ID,
            KEY_ANCHOR,
            KEY_CHUNK_INDEX,
            KEY_LOC_START,
            KEY_LOC_END,
            KEY_CONTENT_HASH,
        }
    )

    def __init__(
        self,
        client: ClientAPI,
        embedder: Embedder[str],
        batch_size: int = DEFAULT_BATCH_SIZE,
    ) -> None:
        self._client = client
        self._embedder = embedder
        self._batch_size = batch_size


    def get_by_ids(
        self,
        collection: CollectionId,
        chunk_ids: Iterable[ChunkId],
    ) -> Iterable[Chunk[str]]:
        ids = [c.to_wire() for c in chunk_ids]
        if not ids:
            return
        coll = self._open(collection)
        result = coll.get(ids=ids, include=["documents", "metadatas"])
        ret_ids = result.get("ids") or []
        documents = result.get("documents") or []
        metadatas = result.get("metadatas") or []
        for cid, doc, meta in zip(ret_ids, documents, metadatas, strict=False):
            yield self._build_chunk(cid, doc or "", meta or {})

    def similarity_search(
        self,
        collection: CollectionId,
        *,
        query: str,
        k: int,
    ) -> Iterable[SearchHit[str]]:
        coll = self._open(collection)
        embedding = list(self._embedder.embed_query(query))
        result = coll.query(
            query_embeddings=[embedding],
            n_results=k,
            include=["documents", "metadatas", "distances"],
        )
        ids = (result.get("ids") or [[]])[0]
        documents = (result.get("documents") or [[]])[0]
        distances = (result.get("distances") or [[]])[0]
        metadatas = (result.get("metadatas") or [[]])[0]
        for cid, doc, dist, meta in zip(
            ids, documents, distances, metadatas, strict=False
        ):
            yield SearchHit(
                chunk_id=ChunkId(cid),
                distance=float(dist),
                snippet=doc or "",
                metadata=self._business_metadata(meta or {}),
            )

    def peek(
        self,
        collection: CollectionId,
        *,
        source_id: SourceId | None,
        limit: int,
    ) -> Iterable[ChunkSummary[str]]:
        coll = self._open(collection)
        where: Where | None = (
            {self.KEY_SOURCE_ID: source_id.to_wire()}
            if source_id is not None
            else None
        )
        result = coll.get(
            where=where,
            limit=limit,
            include=["documents", "metadatas"],
        )
        ids = result.get("ids") or []
        documents = result.get("documents") or []
        metadatas = result.get("metadatas") or []
        for cid, doc, meta in zip(ids, documents, metadatas, strict=False):
            yield self._build_summary(cid, doc or "", meta or {})


    def upsert(
        self,
        collection: CollectionId,
        chunks: Iterable[Chunk[str]],
    ) -> None:
        coll = self._open(collection)
        for batch in self._batched(chunks):
            ids = [c.chunk_id.to_wire() for c in batch]
            documents = [c.content for c in batch]
            metadatas: list[ChromaMetadata] = [
                self._encode_metadata(c) for c in batch
            ]
            embeddings: list[PyEmbedding] = [
                list(v) for v in self._embedder.embed_documents(documents)
            ]
            coll.upsert(
                ids=ids,
                documents=documents,
                metadatas=metadatas,
                embeddings=embeddings,
            )

    def delete(
        self,
        collection: CollectionId,
        chunk_ids: Iterable[ChunkId],
    ) -> None:
        ids = [c.to_wire() for c in chunk_ids]
        if not ids:
            return
        self._open(collection).delete(ids=ids)

    def list_collections(self) -> Iterable[CollectionInfo]:
        for coll in self._client.list_collections():
            yield self._collection_info(coll)

    def collection_info(self, name: CollectionId) -> CollectionInfo:
        return self._collection_info(self._open(name))

    def ensure_collection(
        self,
        name: CollectionId,
        *,
        description: str | None,
    ) -> None:
        meta = {self.DESCRIPTION_KEY: description} if description else None
        self._client.get_or_create_collection(name=name.to_wire(), metadata=meta)

    def delete_collection(self, name: CollectionId) -> None:
        self._client.delete_collection(name=name.to_wire())

    def _open(self, collection: CollectionId) -> Collection:
        return self._client.get_or_create_collection(name=collection.to_wire())

    def _collection_info(self, coll: Collection) -> CollectionInfo:
        meta = coll.metadata or {}
        description = str(meta.get(self.DESCRIPTION_KEY, "") or "")
        return CollectionInfo(
            name=CollectionId(coll.name),
            description=description,
            count=coll.count(),
        )

    def _encode_metadata(self, chunk: Chunk[str]) -> dict[str, str | int | float]:
        out: dict[str, str | int | float] = dict(chunk.metadata.to_wire())
        out[self.KEY_SOURCE_ID] = chunk.source_id.to_wire()
        out[self.KEY_ANCHOR] = chunk.anchor or ""
        out[self.KEY_CHUNK_INDEX] = chunk.chunk_index
        out[self.KEY_LOC_START] = chunk.location.start
        out[self.KEY_LOC_END] = chunk.location.end
        if chunk.content_hash is not None:
            out[self.KEY_CONTENT_HASH] = chunk.content_hash.to_wire()
        return out

    def _build_chunk(
        self,
        chunk_id: str,
        content: str,
        meta: Mapping[str, Any],
    ) -> Chunk[str]:
        anchor = str(meta.get(self.KEY_ANCHOR, "") or "")
        content_hash_wire = meta.get(self.KEY_CONTENT_HASH)
        return Chunk(
            chunk_id=ChunkId(chunk_id),
            source_id=SourceId(str(meta.get(self.KEY_SOURCE_ID, ""))),
            content=content,
            location=ChunkLocation(
                start=int(meta.get(self.KEY_LOC_START, 0) or 0),
                end=int(meta.get(self.KEY_LOC_END, 0) or 0),
            ),
            anchor=anchor or None,
            chunk_index=int(meta.get(self.KEY_CHUNK_INDEX, 0) or 0),
            content_hash=(
                StringContentHash(text=str(content_hash_wire))
                if content_hash_wire is not None
                else None
            ),
            metadata=self._business_metadata(meta),
        )

    def _build_summary(
        self,
        chunk_id: str,
        snippet: str,
        meta: Mapping[str, Any],
    ) -> ChunkSummary[str]:
        anchor = str(meta.get(self.KEY_ANCHOR, "") or "")
        return ChunkSummary(
            chunk_id=ChunkId(chunk_id),
            source_id=SourceId(str(meta.get(self.KEY_SOURCE_ID, ""))),
            location=ChunkLocation(
                start=int(meta.get(self.KEY_LOC_START, 0) or 0),
                end=int(meta.get(self.KEY_LOC_END, 0) or 0),
            ),
            anchor=anchor or None,
            chunk_index=int(meta.get(self.KEY_CHUNK_INDEX, 0) or 0),
            snippet=snippet,
            metadata=self._business_metadata(meta),
        )

    @classmethod
    def _business_metadata(cls, meta: Mapping[str, Any]) -> Metadata:
        wire: dict[str, str] = {
            k: str(v)
            for k, v in meta.items()
            if k not in cls._RESERVED_KEYS and v is not None
        }
        return Metadata.from_wire(wire)

    def _batched(self, chunks: Iterable[Chunk[str]]) -> Iterable[list[Chunk[str]]]:
        it = iter(chunks)
        while True:
            batch = list(islice(it, self._batch_size))
            if not batch:
                return
            yield batch
