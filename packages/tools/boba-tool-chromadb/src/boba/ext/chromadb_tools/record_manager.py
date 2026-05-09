"""ChromaRecordManager — RecordManager поверх отдельной служебной Chroma-коллекции.

Хранение:
  - одна shared chroma-коллекция (по умолчанию `boba_records`);
  - id каждой записи композитный: `f"{namespace}::{key}"` — изолирует
    namespace'ы внутри одной физической коллекции;
  - embedding = `[0.0]` (плейсхолдер, search не используется);
  - metadata = `{namespace, key, group_id, updated_at}` — для list_keys фильтров.

Класс реализует все три ABC (Reader / Writer / Admin); RecordsAdmin.create_schema
просто гарантирует существование служебной коллекции.
"""

from __future__ import annotations

import time
from collections.abc import Iterable
from typing import Any, ClassVar

from chromadb.api import ClientAPI
from chromadb.api.models.Collection import Collection

from boba.indexing.context import NamespaceId
from boba.indexing.records import (
    ListKeysQuery,
    RecordEntry,
    RecordManagerReader,
    RecordManagerWriter,
    RecordsAdmin,
)

__all__ = ["ChromaRecordManager"]


class ChromaRecordManager(RecordManagerReader, RecordManagerWriter, RecordsAdmin):
    """RecordManager + RecordsAdmin поверх служебной Chroma-коллекции."""

    DEFAULT_COLLECTION_NAME: ClassVar[str] = "boba_records"
    SEPARATOR: ClassVar[str] = "::"
    PLACEHOLDER_EMBEDDING: ClassVar[tuple[float, ...]] = (0.0,)

    KEY_NAMESPACE: ClassVar[str] = "namespace"
    KEY_KEY: ClassVar[str] = "key"
    KEY_GROUP_ID: ClassVar[str] = "group_id"
    KEY_UPDATED_AT: ClassVar[str] = "updated_at"

    def __init__(
        self,
        client: ClientAPI,
        collection_name: str = DEFAULT_COLLECTION_NAME,
    ) -> None:
        self._client = client
        self._collection_name = collection_name


    def create_schema(self) -> None:
        self._client.get_or_create_collection(name=self._collection_name)

    def exists(
        self,
        namespace: NamespaceId,
        keys: Iterable[str],
    ) -> Iterable[bool]:
        ids = [self._make_id(namespace, k) for k in keys]
        if not ids:
            return
        result = self._open().get(ids=ids, include=["metadatas"])
        found = set(result.get("ids") or [])
        for cid in ids:
            yield cid in found

    def list_keys(
        self,
        namespace: NamespaceId,
        query: ListKeysQuery,
    ) -> Iterable[str]:
        where = self._build_where(namespace, query)
        get_kwargs: dict[str, Any] = {"where": where, "include": ["metadatas"]}
        if query.limit is not None:
            get_kwargs["limit"] = query.limit
        result = self._open().get(**get_kwargs)
        metadatas = result.get("metadatas") or []
        ids = result.get("ids") or []
        for cid, meta in zip(ids, metadatas, strict=False):
            key_value = (meta or {}).get(self.KEY_KEY)
            yield str(key_value) if key_value is not None else self._strip_ns(namespace, cid)

    def get_time(self) -> float:
        return time.time()

    def update(
        self,
        namespace: NamespaceId,
        entries: Iterable[RecordEntry],
        *,
        time_at_least: float,
    ) -> None:
        entries_list = list(entries)
        if not entries_list:
            return
        ids = [self._make_id(namespace, e.key) for e in entries_list]
        metadatas: list[dict[str, str | int | float | bool]] = [
            {
                self.KEY_NAMESPACE: namespace.to_wire(),
                self.KEY_KEY: e.key,
                self.KEY_GROUP_ID: e.group_id or "",
                self.KEY_UPDATED_AT: float(time_at_least),
            }
            for e in entries_list
        ]
        embeddings = [list(self.PLACEHOLDER_EMBEDDING)] * len(ids)
        documents = [""] * len(ids)

        self._open().upsert(
            ids=ids,
            documents=documents,
            metadatas=metadatas,
            embeddings=embeddings,
        )

    def delete_keys(
        self,
        namespace: NamespaceId,
        keys: Iterable[str],
    ) -> None:
        ids = [self._make_id(namespace, k) for k in keys]
        if not ids:
            return
        self._open().delete(ids=ids)

    def _open(self) -> Collection:
        return self._client.get_or_create_collection(name=self._collection_name)

    @classmethod
    def _make_id(cls, namespace: NamespaceId, key: str) -> str:
        return f"{namespace.to_wire()}{cls.SEPARATOR}{key}"

    @classmethod
    def _strip_ns(cls, namespace: NamespaceId, composite_id: str) -> str:
        prefix = f"{namespace.to_wire()}{cls.SEPARATOR}"
        if composite_id.startswith(prefix):
            return composite_id[len(prefix) :]
        return composite_id

    @classmethod
    def _build_where(
        cls,
        namespace: NamespaceId,
        query: ListKeysQuery,
    ) -> dict[str, Any]:
        clauses: list[dict[str, Any]] = [{cls.KEY_NAMESPACE: namespace.to_wire()}]

        if query.group_ids is not None:
            group_ids = list(query.group_ids)
            if group_ids:
                clauses.append({cls.KEY_GROUP_ID: {"$in": group_ids}})

        if query.before is not None:
            clauses.append({cls.KEY_UPDATED_AT: {"$lt": float(query.before)}})

        if query.after is not None:
            clauses.append({cls.KEY_UPDATED_AT: {"$gt": float(query.after)}})

        if len(clauses) == 1:
            return clauses[0]

        return {"$and": clauses}
