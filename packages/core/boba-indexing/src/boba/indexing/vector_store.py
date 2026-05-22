"""VectorStore[T] + CollectionsAdmin — все чистые абстракции работы с векторной базой.

Две ортогональные оси:

1. Document-уровень — VectorStore[T] (чанки внутри коллекции):
   - VectorStoreReader[T]: get_by_ids, similarity_search, peek, find, diff_by_hash
   - VectorStoreWriter[T]: upsert, update_metadata, delete
   - VectorStore[T]: композиция

2. Collection-уровень — CollectionsAdmin (CRUD над коллекциями целиком):
   - CollectionsAdminReader: list_collections, collection_info
   - CollectionsAdminWriter: ensure_collection, delete_collection
   - CollectionsAdmin: композиция

Коллекция идентифицируется явным параметром `collection: CollectionId` в
каждом методе VectorStore.

Один store-instance обслуживает много коллекций.

Pipeline-уровень встраивается отдельной обёрткой - `boba.indexing.chunk_sink.ChunkSink`

Embedder[T] НЕ входит в контракт Store: store принимает уже готовый
embedding (через `EmbeddedChunk[T]` на write и `Sequence[float]` на
similarity-search). Embedder инжектится в pipeline-orchestrator
(`CollectionScopedView`, `NamespacedView` и т.п.).

Конкретный backend обычно реализует и VectorStore[T], и CollectionsAdmin
одной сущностью: например ChromaDBClient — единый класс, реализующий оба
интерфейса.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Generic, TypeVar

from boba.indexing.chunks import Chunk, ChunkId, ChunkSummary, EmbeddedChunk
from boba.indexing.content_hash import ContentHash
from boba.indexing.context import CollectionId
from boba.indexing.filter import Filter
from boba.indexing.metadata import Metadata
from boba.indexing.sections import SourceId

__all__ = [
    "CollectionInfo",
    "CollectionsAdminReader",
    "CollectionsAdminWriter",
    "HashDiff",
    "SearchHit",
    "VectorStoreReader",
    "VectorStoreWriter",
]

T = TypeVar("T")


@dataclass(frozen=True)
class SearchHit(Generic[T]):
    """
    Один результат поиска над `VectorStore[T]`

    Содержит:
    - `chunk_id`: id найденного чанка, через него можно получить полный
      `Chunk[T]` через `VectorStoreReader.get_by_ids([chunk_id])`

    - `distance`: метрика proximity к query. Семантика **зависит от backend'а**:
      у Chroma — squared-L2 / cosine-distance (меньше = ближе);
      у других провайдеров может быть similarity-score (больше = ближе).
      Backend документирует свою метрику; единого контракта нет.

    - `snippet`: preview выдержка content'а чанка (тип совпадает с T `VectorStore[T]`)
      Для T=str — обрезанный/highlighted текст для UI;
      для T=bytes — thumbnail или сжатый sample.
      Не обязательно равно полному `Chunk.content` — backend решает что класть.

    - `metadata`: метадата чанка (`source_id`, `anchor`, transport/reader/
      chunker-keys и т.п.) для отрисовки в UI без re-fetch'а Chunk'а.
    """

    chunk_id: ChunkId
    distance: float
    snippet: T
    metadata: Metadata = field(default_factory=Metadata.empty)


@dataclass(frozen=True)
class HashDiff:
    """План действий для батча кандидатов после сверки по content_hash.

    `to_upsert` — chunk_id'ы, для которых нужно посчитать embedding и
                  записать в Store: либо отсутствуют в коллекции, либо
                  существуют, но с другим content_hash. Order сохраняется
                  относительно порядка кандидатов на входе.
    `unchanged` — chunk_id'ы уже в Store с тем же content_hash. Store
                  только бампает `updated_at` через `update_metadata` —
                  re-embed и payload не трогаем.

    `to_delete` отсутствует намеренно: per-run cleanup устаревших чанков —
    отдельная задача `CleanupStrategy`, работает на per-run уровне через
    `Lt(updated_at, run_start)`, а не по per-batch diff'у.
    """

    to_upsert: list[ChunkId]
    unchanged: list[ChunkId]


@dataclass(frozen=True)
class CollectionInfo:
    """Логическая группа векторов в Store (collection в Chroma/Qdrant и т.п.)."""

    name: CollectionId
    description: str
    count: int


class VectorStoreReader(ABC, Generic[T]):
    """Read-side порт: search + inspect документов в коллекции."""

    @abstractmethod
    def get_by_ids(
        self,
        collection: CollectionId,
        chunk_ids: Iterable[ChunkId],
    ) -> Iterable[Chunk[T]]:
        """Получить чанки по id из коллекции; пропускает несуществующие."""
        ...

    @abstractmethod
    def similarity_search(
        self,
        collection: CollectionId,
        *,
        query_embedding: Sequence[float],
        k: int,
    ) -> Iterable[SearchHit[T]]:
        """Семантический поиск top-k по уже посчитанному embedding'у запроса.

        Caller отвечает за `Embedder.embed_query(...)` сам: Store не знает
        про `Embedder` и не делает embedding на read-стороне. Размерность
        `query_embedding` должна совпадать с embedding'ом коллекции;
        несовпадение — ошибка backend'а.
        """
        ...

    @abstractmethod
    def peek(
        self,
        collection: CollectionId,
        *,
        source_id: SourceId | None,
        limit: int,
    ) -> Iterable[ChunkSummary[T]]:
        """
        Admin-просмотр: до limit ChunkSummary
        source_id=None — без фильтра
        """
        ...

    @abstractmethod
    def find(
        self,
        collection: CollectionId,
        *,
        where: Filter | None,
        limit: int | None = None,
    ) -> Iterable[ChunkSummary[T]]:
        """
        Backend-агностичный поиск по фильтру (Filter DSL).

        `where=None`  — без фильтрации (вся коллекция, ограничено limit'ом).
        `limit=None`  — без лимита (caller сам отвечает за риски на больших данных).

        Backend переводит Filter в свой нативный язык запроса; для непереводимых
        предикатов бросает `UnsupportedFilterError`.

        Используется IndexQuery для cleanup-find, scope-aware find и т.п.
        """
        ...

    @abstractmethod
    def diff_by_hash(
        self,
        collection: CollectionId,
        candidates: Iterable[tuple[ChunkId, ContentHash]],
    ) -> HashDiff:
        """
        Сравнить кандидатов со Store по content_hash; вернуть план записи.

        `candidates` — пары `(chunk_id, content_hash)`, обычно один батч,
        приехавший в `IndexSink.reconcile`. `content_hash` гарантированно
        non-null: его обязан вычислить Chunker через свой `KeyEncoder[T]`
        в момент эмиссии чанка. Backend читает только `(chunk_id,
        content_hash)` — без payload и embedding'а; типично один SELECT
        по `chunk_id = ANY(...)`.

        Семантика split'а:
          - chunk_id отсутствует в коллекции → `to_upsert`
          - chunk_id есть, hash совпадает    → `unchanged`
          - chunk_id есть, hash отличается   → `to_upsert`

        Order сохраняется относительно `candidates`. `to_delete` сюда не
        входит — это работа `CleanupStrategy`.
        """
        ...


class VectorStoreWriter(ABC, Generic[T]):
    """Write-side порт: upsert/delete документов в коллекции."""

    @abstractmethod
    def upsert(
        self,
        collection: CollectionId,
        chunks: Iterable[EmbeddedChunk[T]],
    ) -> None:
        """
        Bulk-upsert чанков в коллекцию.

        Принимает `EmbeddedChunk[T]` — chunk + уже посчитанный embedding.
        Embedder в Store больше не инжектится: caller сам вызывает
        `Embedder.embed_documents(...)` (обычно — в `IndexSink.reconcile`).

        Полная замена по chunk_id: existing запись (если есть) перезаписывается
        целиком — embedding, document, metadata. Ключи metadata, бывшие у старой
        записи но отсутствующие в новой, удаляются (это важно для tracking-полей
        вроде tag.X и для произвольной business-Metadata).

        Для частичного обновления metadata без re-embed — используй
        `update_metadata` (там merge-семантика).

        Atomic per batch не гарантируется.
        """
        ...

    @abstractmethod
    def update_metadata(
        self,
        collection: CollectionId,
        chunk_ids: Iterable[ChunkId],
        patch: Mapping[str, str | int | float | bool],
    ) -> None:
        """
        Patch-обновление metadata у указанных chunk'ов БЕЗ re-embed.

        Только перечисленные в `patch` ключи перезаписываются; остальные
        metadata-поля чанка остаются нетронутыми. embedding и document
        не перерасчитываются.

        Используется IndexSink для refresh `updated_at` в skip-case
        (чанк не изменился, но касался текущего прогона — нужно пометить как seen).
        """
        ...

    @abstractmethod
    def delete(
        self,
        collection: CollectionId,
        chunk_ids: Iterable[ChunkId],
    ) -> None:
        """
        Удалить чанки по id из коллекции
        несуществующие игнорируются
        """
        ...


class CollectionsAdminReader(ABC):
    """Read-side admin: перечисление и инспекция коллекций."""

    @abstractmethod
    def list_collections(self) -> Iterable[CollectionInfo]:
        """Все коллекции в backend'е."""
        ...

    @abstractmethod
    def collection_info(self, name: CollectionId) -> CollectionInfo:
        """Сводка одной коллекции по имени."""
        ...


class CollectionsAdminWriter(ABC):
    """Write-side admin: создание и удаление коллекций."""

    @abstractmethod
    def ensure_collection(
        self,
        name: CollectionId,
        *,
        description: str | None,
    ) -> None:
        """Создать коллекцию name, если отсутствует. Idempotent."""
        ...

    @abstractmethod
    def delete_collection(self, name: CollectionId) -> None:
        """Удалить коллекцию целиком."""
        ...
