"""Адаптер ChromaDB для CLI: write-операции (upsert/delete) и служебные."""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CollectionSummary:
    name: str
    description: str
    count: int


class VectorStore:
    """Тонкая обёртка над PersistentClient для операций индексирования."""

    def __init__(self, persist_path: str) -> None:
        # ленивый импорт chromadb — не падать при импорте пакета без deps
        import chromadb  # noqa: PLC0415

        self._client = chromadb.PersistentClient(path=persist_path)
        logger.info("VectorStore opened persist_path=%r", persist_path)

    def list_collections(self) -> list[CollectionSummary]:
        return [self._summarize(c) for c in self._client.list_collections()]

    def get_collection_summary(self, name: str) -> CollectionSummary:
        return self._summarize(self._client.get_collection(name=name))

    def _summarize(self, c) -> CollectionSummary:  # type: ignore[no-untyped-def]
        metadata = c.metadata or {}
        description = metadata.get("description", "") if metadata else ""
        try:
            count = c.count()
        except Exception as e:
            logger.warning("count() failed for %r: %s", c.name, e)
            count = -1
        return CollectionSummary(
            name=c.name,
            description=description if isinstance(description, str) else "",
            count=count,
        )

    def get_or_create_collection(
        self,
        name: str,
        description: str | None,
    ):
        """Найти или создать коллекцию; description пишется только при создании."""
        existing = {c.name: c for c in self._client.list_collections()}
        if name in existing:
            return self._client.get_collection(name=name)
        metadata: dict[str, str] = {}
        if description:
            metadata["description"] = description
        return self._client.create_collection(
            name=name,
            metadata=metadata or None,
        )

    def delete_collection(self, name: str) -> None:
        self._client.delete_collection(name=name)

    def delete_by_source(self, collection_name: str, source_path: str) -> int:
        """Удалить все чанки с metadata[source_path]==source_path; вернуть кол-во удалённых."""
        col = self._client.get_collection(name=collection_name)
        existing = col.get(where={"source_path": source_path})
        ids = existing.get("ids") or []
        if not ids:
            return 0
        col.delete(ids=ids)
        return len(ids)

    def upsert_chunks(
        self,
        collection_name: str,
        chunks: Sequence[tuple[str, str, Mapping[str, str]]],
    ) -> None:
        """chunks — список (id, document_text, metadata)."""
        if not chunks:
            return
        col = self._client.get_collection(name=collection_name)
        ids = [c[0] for c in chunks]
        documents = [c[1] for c in chunks]
        # dict[str, str] ⊂ chromadb.Metadata, но list-инвариантность требует ignore
        metadatas = [dict(c[2]) for c in chunks]
        col.upsert(
            ids=ids,
            documents=documents,
            metadatas=metadatas,  # pyright: ignore[reportArgumentType]
        )
