"""
Chunk[T] - атомарный кусок контента для индексирования в vector store
с metadata и местоположением в исходном документе

Generic над content-типом:
    TextChunker → Chunk[str]
    ImageChunker → Chunk[bytes]
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Generic, Self, TypeVar

from boba.indexing.content_hash import ContentHash
from boba.indexing.metadata import Metadata
from boba.indexing.sections import SourceId
from boba.patterns import StrId

__all__ = ["Chunk", "ChunkId", "ChunkLocation", "ChunkSummary"]

T = TypeVar("T")


class ChunkId(StrId):
    """Стабильный составной id чанка для idempotent re-index.

    Каноническая форма wire-id: `{digest_prefix}:{chunk_index}`.
    Конструируется из digest через `ChunkId.from_digest(...)`
    это единая точка форматирования ChunkId
    """

    @classmethod
    def from_digest(
        cls,
        digest: str,
        chunk_index: int,
        prefix_length: int,
    ) -> Self:
        """Truncation digest'а до prefix_length + ':{chunk_index}'."""
        return cls(f"{digest[:prefix_length]}:{chunk_index}")


@dataclass(frozen=True)
class ChunkLocation:
    """
    Положение чанка в исходном content

    `start`/`end` — в естественных единицах T:
        char offsets для str,
        byte offsets для bytes
        индексы для list-like.

    `start` включительно, `end` исключительно (полуинтервал).
    """

    start: int
    end: int


@dataclass(frozen=True)
class Chunk(Generic[T]):
    """Один кусок индексируемого контента — единица хранения VectorStore.

    Поля:

    `chunk_id`     — уникальный id чанка в VectorStore.
                    не auto-generated как Serial в postgres а именно application-level
                    (тот же source/anchor → тот же id из прогона в прогон).
                    Делается ChunkIdStrategy.

    `source_id`    — id source-документа, из которого вырезан чанк
                     (`Request.source_id`). Несколько чанков одного документа
                     имеют одинаковый source_id.

    `content`      — собственно контент чанка (текст, байты, …); тип задаётся
                     generic-параметром `T`. Именно он попадает в Embedder
                     и, в большинстве backend'ов, в `document` поле Store.

    `location`     — положение чанка в `Section.content` (offset'ы start/end);
                     нужно для UI-citations и подсветки.

    `anchor`       — стабильный якорь внутри source-документа (heading-id,
                     fragment, page-section). `None` у плоских документов
                     (PlainTextReader всегда даёт `None`); для Markdown/HTML
                     — `"#section-1.2"` и т.п. Используется AnchorBasedChunkId
                     для стабильности chunk_id'ов через re-index.

    `chunk_index`  — порядковый номер чанка внутри своего `source_id`;
                     детерминирует chunk_id (`{digest}:{chunk_index}`)
                     и обеспечивает уникальность даже при одинаковом anchor'е.

    `content_hash` — fingerprint содержимого, для idempotency-check'а в
                     `IndexSink.reconcile` (skip-if-unchanged). `None` пока
                     pipeline не enrich'нул чанк через `KeyEncoder.encode` —
                     Chunker про KeyEncoder не знает, hash проставляет
                     пайплайн (StreamingIndexer) перед reconcile.

    `metadata`     — произвольная business-Metadata, проброшенная Reader'ом
                     и Chunker'ом (`transport.etag`, `reader.doc_type`,
                     `chunker.chunk_summary` …). Никаких системных tracking-
                     полей view-импл сюда не пишет.

    `tags`         — множество тэгов чанка, индексируемое view-импл'ом
                     отдельно для быстрой фильтрации через
                     `HasTag`/`HasAnyTag`/`HasAllTags`. Reader/Chunker могут их
                     проставлять (категория документа, security-метка); view-
                     импл сохраняет в Store и индексирует для поиска.
    """

    chunk_id: ChunkId
    source_id: SourceId
    content: T
    location: ChunkLocation
    anchor: str | None = None
    chunk_index: int = 0
    content_hash: ContentHash | None = None
    metadata: Metadata = field(default_factory=Metadata.empty)
    tags: frozenset[str] = field(default_factory=frozenset)


@dataclass(frozen=True)
class ChunkSummary(Generic[T]):
    """Лёгкая read-only сводка чанка — то что возвращает `IndexQuery.find`.

    То же что `Chunk[T]`, но БЕЗ `content` (вместо него — `snippet`) и
    БЕЗ `content_hash` (тот не нужен потребителю поиска). Используется
    когда полный content тяжёлый таскать (большие документы, картинки),
    а нужен только метаданные + сниппет для UI/citations.

    Поля:

    `chunk_id`    — id чанка в VectorStore (как в Chunk).
    `source_id`   — id source-документа.
    `location`    — offset'ы в source.content.
    `anchor`      — якорь в документе (см. Chunk.anchor).
    `chunk_index` — порядковый номер.
    `snippet`     — preview контента: для T=str — обрезанная/highlighted
                    выдержка; для T=bytes — thumbnail или сжатый образец.
                    Backend сам решает, что положить (полный content или
                    обрезку); `Chunk.content` ≠ `ChunkSummary.snippet`
                    в общем случае.
    `metadata`    — business-Metadata (как в Chunk).
    `tags`        — тэги (как в Chunk).
    """

    chunk_id: ChunkId
    source_id: SourceId
    location: ChunkLocation
    anchor: str | None
    chunk_index: int
    snippet: T
    metadata: Metadata = field(default_factory=Metadata.empty)
    tags: frozenset[str] = field(default_factory=frozenset)
