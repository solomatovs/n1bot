"""Store: StreamSink[IndexingContext, Chunk] + sync/admin операции."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable

from boba.indexing.chunks import Chunk, ChunkSummary
from boba.indexing.collections import CollectionInfo
from boba.indexing.context import IndexingContext
from boba.patterns import StreamSink, StrId

__all__ = ["Store", "StoreId"]


class StoreId(StrId):
    """Идентификатор Store-реализации (например 'ext.chromadb_persist')."""


class Store(StreamSink[IndexingContext, Chunk], ABC):
    """Терминальный потребитель Chunk'ов с побочным эффектом upsert.

    `handle` пишет один Chunk в `ctx.collection`. Реализация может
    буферизировать и сбрасывать в `flush()` / `reset()`.

    `ensure_target` — опциональный hook перед началом потока (создание
    коллекции/индекса с описанием).

    `delete_by_source` и `list_source_ids` — для idempotent re-index и
    sync с Source.list_source_ids().
    """

    @abstractmethod
    def store_id(self) -> StoreId: ...

    def ensure_target(
        self, ctx: IndexingContext, description: str | None
    ) -> None:
        """Создать целевую коллекцию, если отсутствует. Default — no-op."""
        del ctx, description

    def flush(self, ctx: IndexingContext) -> None:
        """Сбросить буфер upsert'ов. Default — no-op."""
        del ctx

    @abstractmethod
    def delete_by_source(
        self, ctx: IndexingContext, source_id: str
    ) -> int:
        """Удалить все чанки с этим source_id; вернуть число удалённых."""
        ...

    @abstractmethod
    def list_source_ids(self, ctx: IndexingContext) -> Iterable[str]:
        """Все source_id, известные Store'у в текущем контексте."""
        ...

    @abstractmethod
    def peek_chunks(
        self,
        ctx: IndexingContext,
        *,
        source_id: str | None = None,
        limit: int = 20,
        snippet_chars: int = 200,
    ) -> Iterable[ChunkSummary]:
        """Read-only inspect: вернуть до `limit` ChunkSummary из коллекции.

        `source_id=None` — без фильтра, все чанки коллекции (упорядочивание
        не гарантировано). С `source_id` — только чанки этого документа,
        упорядочены по `chunk_index`.

        Метод используется операторским CLI (action=show). Не для агента
        и не для индексации.
        """
        ...

    def content_hash_for(
        self, ctx: IndexingContext, source_id: str
    ) -> str:
        """Текущий content_hash чанков этого source_id; пусто = неизвестно/нет.

        Используется Pipeline для skip-if-unchanged: если новый item.content_hash
        равен этому, переиндексация пропускается.

        Default-реализация — пустая строка (не поддерживает incremental;
        Pipeline всегда сделает delete+upsert).
        """
        del ctx, source_id
        return ""

    @abstractmethod
    def list_collections(self) -> Iterable[CollectionInfo]:
        """Все коллекции в Store. Admin-операция — без IndexingContext."""
        ...

    @abstractmethod
    def collection_info(self, name: str) -> CollectionInfo:
        """Сводка одной коллекции по имени."""
        ...

    @abstractmethod
    def delete_collection(self, name: str) -> None:
        """Удалить коллекцию целиком."""
        ...
