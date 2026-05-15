"""
Chunk[T] — атомарный кусок контента для индексирования в vector store.

Generic над content-типом:
    TextChunker  → Chunk[str]
    ImageChunker → Chunk[bytes]

Контракт двух content-полей:

- `format_content` — финальная версия для embedder/LLM. Это то, что попадает
  в `Embedder.embed_documents(...)` и в поле `document` стора. Может включать
  обогащение (heading-prefix, реплицированный header таблицы, и т.п.).
- `raw_content`   — оригинальный фрагмент исходника, без обогащения. Для
  citation/UI (показать пользователю «вот откуда взялся ответ»).

Format-specific атрибуты — `location`, `anchor` — живут в `metadata` через
типизированные `ChunkKeys.*`. Не каждый формат может их предоставить
(HTML, например, без lxml-sourceline-tracking не даёт честных char-offset'ов),
поэтому в DTO они не required-полями.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import ClassVar, Generic, NewType, TypeVar

from boba.indexing.content_hash import ContentHash
from boba.indexing.location import ChunkLocation
from boba.indexing.metadata import Metadata, MetadataKey
from boba.indexing.sections import SourceId

__all__ = [
    "Chunk",
    "ChunkId",
    "ChunkKeys",
    "ChunkLocation",
    "ChunkSummary",
    "chunk_id_from_digest",
]

T = TypeVar("T")


ChunkId = NewType("ChunkId", str)
"""Стабильный составной id чанка для idempotent re-index.

Канонический wire-формат: `{digest_prefix}:{chunk_index}`.
Конструируется через `chunk_id_from_digest(...)` — единая точка форматирования.
"""


def chunk_id_from_digest(
    digest: str, chunk_index: int, prefix_length: int,
) -> ChunkId:
    """Скомпоновать ChunkId из digest'а и индекса чанка."""
    return ChunkId(f"{digest[:prefix_length]}:{chunk_index}")


class ChunkKeys:
    """Стандартные `MetadataKey`-и для chunk-level атрибутов.

    Format-specific Chunker'ы пишут эти ключи **только если** соответствующий
    формат может их корректно вычислить. Consumer (UI, citation, store) читает
    через `chunk.metadata.get(ChunkKeys.X)` и обрабатывает `None` как «формат
    не предоставил эту информацию».
    """

    LOCATION_START: ClassVar[MetadataKey[int]] = MetadataKey(
        name="chunk.location.start",
        decode=int,
        encode=str,
    )
    """Char/byte-offset начала чанка в исходном (decoded) документе."""

    LOCATION_END: ClassVar[MetadataKey[int]] = MetadataKey(
        name="chunk.location.end",
        decode=int,
        encode=str,
    )
    """Char/byte-offset конца чанка в исходном (decoded) документе."""

    ANCHOR: ClassVar[MetadataKey[str]] = MetadataKey(
        name="chunk.anchor",
        decode=str,
        encode=str,
    )
    """Якорь в source-документе (heading-id, fragment, html-id)."""


@dataclass(frozen=True)
class Chunk(Generic[T]):
    """Один кусок индексируемого контента — единица хранения VectorStore.

    Поля:

    `chunk_id`       — уникальный id чанка в VectorStore. Application-level
                       (тот же source/anchor → тот же id из прогона в прогон).
                       Считается `ChunkIdStrategy`.
    `source_id`      — id source-документа, из которого вырезан чанк. Несколько
                       чанков одного документа имеют одинаковый source_id.
    `format_content` — версия для embedder/LLM (с обогащением). Это и кладётся
                       в Embedder и в `document` поле Store.
    `raw_content`    — оригинальный фрагмент source'а, без обогащения. Для
                       UI-citation / deep-link reconstruction.
    `chunk_index`    — порядковый номер чанка внутри своего `source_id`;
                       детерминирует chunk_id (`{digest}:{chunk_index}`).
    `content_hash`   — fingerprint, для idempotency-check'а в
                       `IndexSink.reconcile`. `None` пока pipeline не enrich'нул
                       чанк через `KeyEncoder.encode`.
    `metadata`       — произвольная Metadata: пробрасывается из Section
                       (`section.metadata + section.to_chunk_metadata()`),
                       плюс chunker-добавки. Format-specific атрибуты типа
                       `ChunkKeys.LOCATION_*` / `ChunkKeys.ANCHOR` — здесь.
    `tags`           — множество тэгов; пробрасываются от Reader/Section.
    """

    chunk_id: ChunkId
    source_id: SourceId
    format_content: T
    raw_content: T
    chunk_index: int = 0
    content_hash: ContentHash | None = None
    metadata: Metadata = field(default_factory=Metadata.empty)
    tags: frozenset[str] = field(default_factory=frozenset)


@dataclass(frozen=True)
class ChunkSummary(Generic[T]):
    """Лёгкая read-only сводка чанка — то что возвращает `IndexQuery.find`.

    То же что `Chunk[T]`, но БЕЗ `format_content` / `raw_content` (вместо
    них — `snippet`) и БЕЗ `content_hash`. Используется когда полный
    content тяжёлый таскать (большие документы, картинки), а нужны
    metadata + сниппет для UI/citations.

    Backend сам решает, что положить в `snippet`: для T=str — обрезанная
    или highlighted-выдержка; для T=bytes — thumbnail.
    """

    chunk_id: ChunkId
    source_id: SourceId
    snippet: T
    chunk_index: int = 0
    metadata: Metadata = field(default_factory=Metadata.empty)
    tags: frozenset[str] = field(default_factory=frozenset)
