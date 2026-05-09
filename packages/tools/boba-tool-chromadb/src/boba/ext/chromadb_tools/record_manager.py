"""ChromaRecordManager — RecordManager поверх служебной Chroma-коллекции + VectorStore.

Конструктор принимает `VectorStoreWriter[str]` как зависимость; чанки пишутся
туда, в собственной Chroma-коллекции `boba_records` лежит только tracking
(namespace, chunk_id, content_hash, group_id, updated_at).

`reconcile()` — единственная точка записи:
  - сравнивает текущий `Chunk.content_hash` с stored (idempotency check);
  - изменившиеся / новые отдаёт в `vector_store.upsert(collection, ...)`;
  - tracking всех чанков обновляется (даже unchanged — для refresh updated_at).

`delete()` — атомарно: vector_store.delete(...) + tracking-cleanup.
`list_stale()` — фильтр по tracking-метаданным.

Tracking-collection (по умолчанию `boba_records`) держит:
  - id записи композитный: `f"{namespace}::{chunk_id}"` — изоляция namespace'ов
    в одной физической коллекции
  - embedding = `[0.0]` (placeholder, search не используется)
  - metadata = {namespace, key, group_id, updated_at, content_hash}

Класс реализует все три ABC (Reader / Writer / Admin); RecordsAdmin.create_schema
гарантирует существование служебной коллекции (chunks-collection — задача caller'а
через `VectorStore.ensure_collection`).
"""

from __future__ import annotations

import time
from collections.abc import Iterable
from typing import Any, ClassVar

from chromadb.api import ClientAPI
from chromadb.api.models.Collection import Collection
from chromadb.api.types import Metadata as ChromaMetadata
from chromadb.api.types import PyEmbedding

from boba.indexing.chunks import Chunk, ChunkId
from boba.indexing.context import CollectionId, NamespaceId
from boba.indexing.records import (
    ReconcileSummary,
    RecordManagerReader,
    RecordManagerWriter,
    RecordsAdmin,
)
from boba.indexing.sections import SourceId
from boba.indexing.vector_store import VectorStoreWriter

__all__ = ["ChromaRecordManager"]


class ChromaRecordManager(
    RecordManagerReader,
    RecordManagerWriter[str],
    RecordsAdmin,
):
    """RecordManager поверх Chroma-коллекции tracking'а + внешнего VectorStore."""

    DEFAULT_COLLECTION_NAME: ClassVar[str] = "boba_records"
    SEPARATOR: ClassVar[str] = "::"
    PLACEHOLDER_EMBEDDING: ClassVar[tuple[float, ...]] = (0.0,)

    KEY_NAMESPACE: ClassVar[str] = "namespace"
    KEY_KEY: ClassVar[str] = "key"
    KEY_GROUP_ID: ClassVar[str] = "group_id"
    KEY_UPDATED_AT: ClassVar[str] = "updated_at"
    KEY_CONTENT_HASH: ClassVar[str] = "content_hash"

    def __init__(
        self,
        client: ClientAPI,
        collection: CollectionId,
        vector_store: VectorStoreWriter[str],
        records_collection_name: str = DEFAULT_COLLECTION_NAME,
    ) -> None:
        self._client = client
        self._collection = collection
        self._vector_store = vector_store
        self._records_collection_name = records_collection_name

    def create_schema(self) -> None:
        self._client.get_or_create_collection(name=self._records_collection_name)

    def get_time(self) -> float:
        return time.time()

    def list_stale(
        self,
        namespace: NamespaceId,
        *,
        before: float,
        group_ids: Iterable[SourceId] | None = None,
    ) -> Iterable[ChunkId]:
        where = self._build_stale_where(namespace, before, group_ids)
        if where is None:
            return
        result = self._records().get(where=where, include=["metadatas"])
        ids = result.get("ids") or []
        metadatas = result.get("metadatas") or []
        for cid, meta in zip(ids, metadatas, strict=False):
            key_value = (meta or {}).get(self.KEY_KEY)
            if key_value is not None:
                yield ChunkId(str(key_value))
            else:
                yield ChunkId(self._strip_ns(namespace, cid))

    def reconcile(
        self,
        namespace: NamespaceId,
        chunks: Iterable[Chunk[str]],
        *,
        time_at_least: float,
        force: bool = False,
    ) -> ReconcileSummary:
        chunks_list = list(chunks)
        if not chunks_list:
            return ReconcileSummary(total=0, upserted=0, unchanged=0)

        if force:
            dirty_chunks = chunks_list
            unchanged = 0
        else:
            stored_hashes = self._read_stored_hashes(namespace, chunks_list)
            dirty_chunks = []
            for c in chunks_list:
                stored = stored_hashes.get(self._make_id(namespace, c.chunk_id))
                current = self._wire_hash(c)
                if stored is None or stored != current:
                    dirty_chunks.append(c)
            unchanged = len(chunks_list) - len(dirty_chunks)

        if dirty_chunks:
            self._vector_store.upsert(self._collection, dirty_chunks)

        self._refresh_tracking(namespace, chunks_list, time_at_least)

        return ReconcileSummary(
            total=len(chunks_list),
            upserted=len(dirty_chunks),
            unchanged=unchanged,
        )

    def delete(
        self,
        namespace: NamespaceId,
        chunk_ids: Iterable[ChunkId],
    ) -> None:
        ids_list = list(chunk_ids)
        if not ids_list:
            return
        self._vector_store.delete(self._collection, ids_list)
        composite = list({self._make_id(namespace, k) for k in ids_list})
        self._records().delete(ids=composite)

    def _records(self) -> Collection:
        return self._client.get_or_create_collection(name=self._records_collection_name)

    @classmethod
    def _make_id(cls, namespace: NamespaceId, chunk_id: ChunkId) -> str:
        return f"{namespace.to_wire()}{cls.SEPARATOR}{chunk_id.to_wire()}"

    @classmethod
    def _strip_ns(cls, namespace: NamespaceId, composite_id: str) -> str:
        prefix = f"{namespace.to_wire()}{cls.SEPARATOR}"
        if composite_id.startswith(prefix):
            return composite_id[len(prefix) :]
        return composite_id

    @staticmethod
    def _wire_hash(chunk: Chunk[str]) -> str:
        if chunk.content_hash is None:
            return ""
        return chunk.content_hash.to_wire()

    def _read_stored_hashes(
        self,
        namespace: NamespaceId,
        chunks: list[Chunk[str]],
    ) -> dict[str, str]:
        ids = [self._make_id(namespace, c.chunk_id) for c in chunks]
        unique_ids = list(dict.fromkeys(ids))
        if not unique_ids:
            return {}
        result = self._records().get(ids=unique_ids, include=["metadatas"])
        ret_ids = result.get("ids") or []
        metadatas = result.get("metadatas") or []
        out: dict[str, str] = {}
        for cid, meta in zip(ret_ids, metadatas, strict=False):
            value = (meta or {}).get(self.KEY_CONTENT_HASH)
            out[cid] = str(value) if value is not None else ""
        return out

    def _refresh_tracking(
        self,
        namespace: NamespaceId,
        chunks: list[Chunk[str]],
        time_at_least: float,
    ) -> None:
        deduped: dict[str, Chunk[str]] = {}
        for c in chunks:
            deduped[self._make_id(namespace, c.chunk_id)] = c
        if not deduped:
            return
        ids = list(deduped.keys())
        metadatas: list[ChromaMetadata] = [
            {
                self.KEY_NAMESPACE: namespace.to_wire(),
                self.KEY_KEY: c.chunk_id.to_wire(),
                self.KEY_GROUP_ID: c.source_id.to_wire(),
                self.KEY_UPDATED_AT: float(time_at_least),
                self.KEY_CONTENT_HASH: self._wire_hash(c),
            }
            for c in deduped.values()
        ]
        embeddings: list[PyEmbedding] = [
            list(self.PLACEHOLDER_EMBEDDING) for _ in ids
        ]
        documents = [""] * len(ids)
        self._records().upsert(
            ids=ids,
            documents=documents,
            metadatas=metadatas,
            embeddings=embeddings,
        )

    def _build_stale_where(
        self,
        namespace: NamespaceId,
        before: float,
        group_ids: Iterable[SourceId] | None,
    ) -> dict[str, Any] | None:
        clauses: list[dict[str, Any]] = [
            {self.KEY_NAMESPACE: namespace.to_wire()},
            {self.KEY_UPDATED_AT: {"$lt": float(before)}},
        ]
        if group_ids is not None:
            wires = [s.to_wire() for s in group_ids]
            if not wires:
                return None
            clauses.append({self.KEY_GROUP_ID: {"$in": wires}})

        if len(clauses) == 1:
            return clauses[0]
        return {"$and": clauses}
