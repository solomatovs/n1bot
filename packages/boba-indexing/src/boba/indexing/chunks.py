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
from boba.indexing.location import ChunkLocation
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
class Chunk(Generic[T]):
    """Один кусок индексируемого контента — единица хранения VectorStore.

    Поля:

    `chunk_id`         — уникальный id чанка в VectorStore.
                        Не auto-generated как Serial в postgres — именно
                        application-level (тот же source/anchor → тот же id
                        из прогона в прогон). Делается `ChunkIdStrategy`.

    `source_id`        — id source-документа, из которого вырезан чанк
                         (`Request.source_id`). Несколько чанков одного документа
                         имеют одинаковый source_id.

    `format_content`   — версия контента для LLM/embedding: может включать
                         реплицированный контекст (например, header таблицы
                         перед data-row'ами) или композицию из соседних секций
                         (heading + следующий параграф). Это то, что попадает
                         в Embedder и в `document` поле Store.

    `raw_content`      — оригинальный slice исходного raw документа (без
                         обогащения). `original[location.start:location.end]
                         == raw_content`. Используется для UI-citations и
                         deep-link reconstruction.

    `location`         — offset чанка в исходном raw документе. Соответствует
                         `raw_content`.

    `section_location` — offset родительской `Section` в исходном raw документе.
                         Позволяет LLM/UI ориентироваться: «этот чанк — часть
                         секции `[section_location.start..end]`».

    `section_type`     — canonical alias типа родительской Section
                         (`"heading"`, `"paragraph"`, `"code_fence"`, `"table"`,
                         `"list"`, ...). Из `Section.SECTION_TYPE`. Может быть
                         пользовательским значением для domain-extensions.

    `anchor`           — стабильный якорь внутри source-документа (heading-id,
                         fragment, page-section). `None` у плоских документов;
                         для heading-aware Reader'ов — slug или html-id.
                         Используется `AnchorBasedChunkId` для стабильности
                         chunk_id'ов и для UI-deeplinks.

    `chunk_index`      — порядковый номер чанка внутри своего `source_id`;
                         детерминирует chunk_id (`{digest}:{chunk_index}`)
                         и обеспечивает уникальность даже при одинаковом anchor'е.

    `content_hash`     — fingerprint содержимого, для idempotency-check'а в
                         `IndexSink.reconcile` (skip-if-unchanged). `None` пока
                         pipeline не enrich'нул чанк через `KeyEncoder.encode`.

    `metadata`         — произвольная business-Metadata: пробрасывается из Section
                         (Section.metadata + Section.to_chunk_metadata()) плюс
                         chunker-добавки (OVERFLOW_REASON, TABLE_ROW_RANGE, …).

    `tags`             — множество тэгов; пробрасываются от Reader/Section.
    """

    chunk_id: ChunkId
    source_id: SourceId
    # `content` оставлен для transition — старый SectionChunker ещё пишет сюда.
    # После полной миграции на StructuralChunker удалится; consumers перейдут
    # на `format_content` / `raw_content`.
    content: T = ""  # type: ignore[assignment]
    format_content: T = ""  # type: ignore[assignment]
    raw_content: T = ""  # type: ignore[assignment]
    location: ChunkLocation = field(
        default_factory=lambda: ChunkLocation(start=0, end=0),
    )
    section_location: ChunkLocation = field(
        default_factory=lambda: ChunkLocation(start=0, end=0),
    )
    section_type: str = "section"
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
