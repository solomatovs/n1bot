"""
ChromaVectorStore — реализауйия VectorStore + CollectionsAdmin поверх chromadb.

Один класс реализует 4 ABC:
- VectorStoreReader[str]:   get_by_ids / similarity_search / peek / find
- VectorStoreWriter[str]:   upsert / delete / update_metadata
- CollectionsAdminReader:   list_collections / collection_info
- CollectionsAdminWriter:   ensure_collection / delete_collection
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from itertools import islice
from typing import Any, ClassVar

from boba.indexing.chunks import Chunk, ChunkId, ChunkSummary
from boba.indexing.content_hash import StringContentHash
from boba.indexing.context import CollectionId
from boba.indexing.embedder import Embedder
from boba.indexing.filter import (
    And,
    Eq,
    Filter,
    Gt,
    Gte,
    HasAllTags,
    HasAnyTag,
    HasTag,
    In,
    Lt,
    Lte,
    Ne,
    Not,
    NotIn,
    Or,
    UnsupportedFilterError,
)
from boba.indexing.index_views import TrackingKeys
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
from chromadb.api import ClientAPI
from chromadb.api.models.Collection import Collection
from chromadb.api.types import Metadata as ChromaMetadata
from chromadb.api.types import PyEmbedding, Where

__all__ = ["ChromaVectorStore"]


class ChromaVectorStore(
    VectorStoreReader[str],
    VectorStoreWriter[str],
    CollectionsAdminReader,
    CollectionsAdminWriter,
):
    """Chroma реализация VectorStore[str] и CollectionsAdmin для индексации текстов"""

    DEFAULT_BATCH_SIZE: ClassVar[int] = 100
    BACKEND_NAME: ClassVar[str] = "chroma"

    KEY_TAG_PREFIX: ClassVar[str] = "tag."
    """Chroma-specific encoding для `Chunk.tags`: chroma не умеет set-valued
    metadata, поэтому каждый тэг разворачивается в отдельный bool-ключ:
    `Chunk.tags = {"public", "doc"}` → `{"tag.public": True, "tag.doc": True}`.
    """

    DESCRIPTION_KEY: ClassVar[str] = "description"
    """Chroma-collection-level metadata-ключ для описания коллекции."""

    # NB: wire-имена Chunk-frame полей (source_id, chunk_index, content_hash)
    # берутся из доменного `TrackingKeys` — единый источник правды на всех
    # store-impl'ов. Format-specific атрибуты (location, anchor) живут в
    # `chunk.metadata` через `ChunkKeys.*` и проходят как обычные dotted-keys.

    def __init__(
        self,
        client: ClientAPI,
        embedder: Embedder[str],
        *,
        embedding_function: Any = None,
        batch_size: int = DEFAULT_BATCH_SIZE,
    ) -> None:
        # `embedding_function` — chromadb-native EF, который пробрасывается в
        # `get_or_create_collection`, чтобы Chroma записала в persisted-конфиг
        # коллекции правильное имя EF (например, `openai`). Без него Chroma
        # подставляет DefaultEmbeddingFunction и persisted-label = `default`,
        # что потом ломает read-side (`kb_search`), который передаёт EF явно.
        # Сами вектора всё равно считает `embedder` и кладёт через
        # `coll.upsert(..., embeddings=...)`, EF на write-side не вызывается.
        self._client = client
        self._embedder = embedder
        self._embedding_function = embedding_function
        self._batch_size = batch_size

    def get_by_ids(
        self,
        collection: CollectionId,
        chunk_ids: Iterable[ChunkId],
    ) -> Iterable[Chunk[str]]:
        ids = [str(c) for c in chunk_ids]
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
            {TrackingKeys.SOURCE_ID: str(source_id)}
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

    def find(
        self,
        collection: CollectionId,
        *,
        where: Filter | None,
        limit: int | None = None,
    ) -> Iterable[ChunkSummary[str]]:
        coll = self._open(collection)
        chroma_where: Where | None = (
            self._filter_to_chroma(where) if where is not None else None
        )
        get_kwargs: dict[str, Any] = {
            "where": chroma_where,
            "include": ["documents", "metadatas"],
        }
        if limit is not None:
            get_kwargs["limit"] = limit
        result = coll.get(**get_kwargs)
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
            ids = [str(c.chunk_id) for c in batch]
            documents = [c.format_content for c in batch]
            metadatas: list[ChromaMetadata] = [self._encode_metadata(c) for c in batch]
            embeddings: list[PyEmbedding] = [
                list(v) for v in self._embedder.embed_documents(documents)
            ]
            # Полная замена metadata: chroma native upsert MERGES metadata
            # (не удаляет old keys, отсутствующие в new). Чтобы соблюсти
            # replace-контракт `VectorStoreWriter.upsert`, явно удаляем
            # перед переустановкой. delete несуществующих id — no-op.
            coll.delete(ids=ids)
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
        ids = [str(c) for c in chunk_ids]
        if not ids:
            return
        self._open(collection).delete(ids=ids)

    def update_metadata(
        self,
        collection: CollectionId,
        chunk_ids: Iterable[ChunkId],
        patch: Mapping[str, str | int | float | bool],
    ) -> None:
        ids = [str(c) for c in chunk_ids]
        if not ids:
            return
        # Chroma's collection.update merges metadata: только ключи из patch
        # перезаписываются, остальные поля чанка не трогаются. Embedding и
        # document не пересчитываются.
        patch_dict = dict(patch)
        metadatas: list[ChromaMetadata] = [patch_dict for _ in ids]
        self._open(collection).update(ids=ids, metadatas=metadatas)

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
        kwargs: dict[str, Any] = {"name": str(name), "metadata": meta}
        if self._embedding_function is not None:
            kwargs["embedding_function"] = self._embedding_function
        self._client.get_or_create_collection(**kwargs)

    def delete_collection(self, name: CollectionId) -> None:
        self._client.delete_collection(name=str(name))

    def _open(self, collection: CollectionId) -> Collection:
        kwargs: dict[str, Any] = {"name": str(collection)}
        if self._embedding_function is not None:
            kwargs["embedding_function"] = self._embedding_function
        return self._client.get_or_create_collection(**kwargs)

    def _collection_info(self, coll: Collection) -> CollectionInfo:
        meta = coll.metadata or {}
        description = str(meta.get(self.DESCRIPTION_KEY, "") or "")
        return CollectionInfo(
            name=CollectionId(coll.name),
            description=description,
            count=coll.count(),
        )

    def _encode_metadata(
        self, chunk: Chunk[str]
    ) -> dict[str, str | int | float | bool]:
        out: dict[str, str | int | float | bool] = dict(chunk.metadata.to_wire())
        out[TrackingKeys.SOURCE_ID] = chunk.source_id
        out[TrackingKeys.CHUNK_INDEX] = chunk.chunk_index
        if chunk.content_hash is not None:
            out[TrackingKeys.CONTENT_HASH] = chunk.content_hash.to_wire()
        for tag in chunk.tags:
            out[self.KEY_TAG_PREFIX + tag] = True
        return out

    def _build_chunk(
        self,
        chunk_id: str,
        content: str,
        meta: Mapping[str, Any],
    ) -> Chunk[str]:
        content_hash_wire = meta.get(TrackingKeys.CONTENT_HASH)
        # При чтении: chroma `documents` хранит только то, что эмбедилось,
        # т.е. format_content. raw_content в storage не выживает — отдаём
        # тот же текст, что и format_content (lossy round-trip; UI/citation
        # без re-fetch исходника не сможет показать pre-обогащения раздел).
        return Chunk(
            chunk_id=ChunkId(chunk_id),
            source_id=SourceId(str(meta.get(TrackingKeys.SOURCE_ID, ""))),
            format_content=content,
            raw_content=content,
            chunk_index=int(meta.get(TrackingKeys.CHUNK_INDEX, 0) or 0),
            content_hash=(
                StringContentHash(text=str(content_hash_wire))
                if content_hash_wire is not None
                else None
            ),
            metadata=self._business_metadata(meta),
            tags=self._extract_tags(meta),
        )

    def _build_summary(
        self,
        chunk_id: str,
        snippet: str,
        meta: Mapping[str, Any],
    ) -> ChunkSummary[str]:
        return ChunkSummary(
            chunk_id=ChunkId(chunk_id),
            source_id=SourceId(str(meta.get(TrackingKeys.SOURCE_ID, ""))),
            chunk_index=int(meta.get(TrackingKeys.CHUNK_INDEX, 0) or 0),
            snippet=snippet,
            metadata=self._business_metadata(meta),
            tags=self._extract_tags(meta),
        )

    @classmethod
    def _business_metadata(cls, meta: Mapping[str, Any]) -> Metadata:
        """
        Извлечь business-Metadata, отбросив system-keys
        """
        wire: dict[str, str] = {
            k: str(v)
            for k, v in meta.items()
            if cls._is_business_key(k) and v is not None
        }
        return Metadata.from_wire(wire)

    @classmethod
    def _is_business_key(cls, k: str) -> bool:
        return "." in k and not k.startswith(cls.KEY_TAG_PREFIX)

    @classmethod
    def _extract_tags(cls, meta: Mapping[str, Any]) -> frozenset[str]:
        prefix_len = len(cls.KEY_TAG_PREFIX)
        return frozenset(
            k[prefix_len:]
            for k, v in meta.items()
            if k.startswith(cls.KEY_TAG_PREFIX) and v
        )

    @classmethod
    def _filter_to_chroma(cls, f: Filter) -> Where:  # noqa: C901, PLR0911, PLR0912
        """
        Перевод Filter DSL в chroma `where`

        Поддержка: Eq/Ne/Lt/Lte/Gt/Gte/In/NotIn/And/Or + ограниченный Not
        (через де-Морган). HasTag/HasAnyTag/HasAllTags пока бросают
        UnsupportedFilterError — для них нужна tags-encoding в metadata
        """
        if isinstance(f, Eq):
            return {f.field: f.value}
        if isinstance(f, Ne):
            return {f.field: {"$ne": f.value}}
        if isinstance(f, Lt):
            return {f.field: {"$lt": f.value}}
        if isinstance(f, Lte):
            return {f.field: {"$lte": f.value}}
        if isinstance(f, Gt):
            return {f.field: {"$gt": f.value}}
        if isinstance(f, Gte):
            return {f.field: {"$gte": f.value}}
        if isinstance(f, In):
            return {f.field: {"$in": list(f.values)}}
        if isinstance(f, NotIn):
            return {f.field: {"$nin": list(f.values)}}
        if isinstance(f, And):
            if not f.filters:
                raise UnsupportedFilterError(f, cls.BACKEND_NAME, "empty And")
            if len(f.filters) == 1:
                return cls._filter_to_chroma(f.filters[0])
            return {"$and": [cls._filter_to_chroma(s) for s in f.filters]}
        if isinstance(f, Or):
            if not f.filters:
                raise UnsupportedFilterError(f, cls.BACKEND_NAME, "empty Or")
            if len(f.filters) == 1:
                return cls._filter_to_chroma(f.filters[0])
            return {"$or": [cls._filter_to_chroma(s) for s in f.filters]}
        if isinstance(f, Not):
            # Chroma не поддерживает $not напрямую
            inner = f.filter
            if isinstance(inner, Eq):
                return cls._filter_to_chroma(Ne(inner.field, inner.value))
            if isinstance(inner, Ne):
                return cls._filter_to_chroma(Eq(inner.field, inner.value))
            if isinstance(inner, Lt):
                return cls._filter_to_chroma(Gte(inner.field, inner.value))
            if isinstance(inner, Lte):
                return cls._filter_to_chroma(Gt(inner.field, inner.value))
            if isinstance(inner, Gt):
                return cls._filter_to_chroma(Lte(inner.field, inner.value))
            if isinstance(inner, Gte):
                return cls._filter_to_chroma(Lt(inner.field, inner.value))
            if isinstance(inner, In):
                return cls._filter_to_chroma(NotIn(inner.field, inner.values))
            if isinstance(inner, NotIn):
                return cls._filter_to_chroma(In(inner.field, inner.values))
            raise UnsupportedFilterError(
                f, cls.BACKEND_NAME, "Not over composite не разворачивается"
            )
        if isinstance(f, HasTag):
            return {cls.KEY_TAG_PREFIX + f.tag: True}
        if isinstance(f, HasAnyTag):
            if not f.tags:
                raise UnsupportedFilterError(
                    f, cls.BACKEND_NAME, "пустой список тэгов в HasAnyTag"
                )
            if len(f.tags) == 1:
                return {cls.KEY_TAG_PREFIX + f.tags[0]: True}
            return {"$or": [{cls.KEY_TAG_PREFIX + t: True} for t in f.tags]}
        if isinstance(f, HasAllTags):
            if not f.tags:
                raise UnsupportedFilterError(
                    f, cls.BACKEND_NAME, "пустой список тэгов в HasAllTags"
                )
            if len(f.tags) == 1:
                return {cls.KEY_TAG_PREFIX + f.tags[0]: True}
            return {"$and": [{cls.KEY_TAG_PREFIX + t: True} for t in f.tags]}
        raise UnsupportedFilterError(
            f, cls.BACKEND_NAME, f"unknown filter type {type(f).__name__}"
        )

    def _batched(self, chunks: Iterable[Chunk[str]]) -> Iterable[list[Chunk[str]]]:
        it = iter(chunks)
        while True:
            batch = list(islice(it, self._batch_size))
            if not batch:
                return
            yield batch
