"""Адаптер ChromaDB для CLI: write-операции (upsert, delete) и
служебные (list, get-or-create, delete-by-source).

Read-tools для агента живут в отдельном пакете ``boba-ext-chromadb``
и работают только на чтение. CLI намеренно не зависит на тот пакет —
оператор может запустить CLI на машине, где extension не установлен.
Backend client (``chromadb.PersistentClient``) общий, поэтому БД
видят оба процесса.
"""

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
    """Тонкая обёртка над :class:`chromadb.PersistentClient` для
    операций индексирования.
    """

    def __init__(self, persist_path: str) -> None:
        # chromadb импортируем лениво, чтобы импорт пакета без deps
        # (например для поиска CLI-команд через --help) не падал.
        import chromadb  # noqa: PLC0415

        self._client = chromadb.PersistentClient(path=persist_path)
        logger.info("VectorStore opened persist_path=%r", persist_path)

    def list_collections(self) -> list[CollectionSummary]:
        out: list[CollectionSummary] = []
        for c in self._client.list_collections():
            metadata = c.metadata or {}
            description = metadata.get("description", "") if metadata else ""
            try:
                count = c.count()
            except Exception as e:
                # битая коллекция не должна валить весь list
                logger.warning("count() failed for %r: %s", c.name, e)
                count = -1
            out.append(
                CollectionSummary(
                    name=c.name,
                    description=description if isinstance(description, str) else "",
                    count=count,
                )
            )
        return out

    def get_or_create_collection(
        self,
        name: str,
        description: str | None,
    ):
        """Возвращает существующую коллекцию или создаёт новую.

        ``description`` записывается в ``metadata["description"]``
        **только при создании**. Для существующей коллекции описание
        не перезаписывается, чтобы операторские правки не потерялись
        при `index --description ...` повторно.
        """
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
        """Удаляет все чанки, у которых ``metadata["source_path"]``
        совпадает с заданным. Возвращает кол-во удалённых.

        Используется в `_indexer` для idempotent reindex: перед upsert
        чанков файла стираем его старые чанки, чтобы исчезнувшие из
        файла куски не оставались в БД.
        """
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
        """``chunks`` — список ``(id, document_text, metadata)``.

        Принимает уже подготовленные id'ы (см. ``_indexer._chunk_id``),
        чтобы повторный upsert был idempotent под одной и той же
        парой (source_path, chunk_index).
        """
        if not chunks:
            return
        col = self._client.get_collection(name=collection_name)
        ids = [c[0] for c in chunks]
        documents = [c[1] for c in chunks]
        # chromadb Metadata = Mapping[str, str|int|float|bool|None|...].
        # Наш dict[str, str] — частный случай, но list-инвариантность
        # требует явного ignore: значения времени выполнения валидны.
        metadatas = [dict(c[2]) for c in chunks]
        col.upsert(  # type: ignore[arg-type]
            ids=ids, documents=documents, metadatas=metadatas
        )
