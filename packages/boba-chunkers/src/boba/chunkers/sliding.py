"""
SlidingChunker — фабрика с cекций SectionChunker[str] где нет anchor (заголовка)

Подходит для плоских документов без явных heading:
    raw text
    code
    log-файлы
"""

from __future__ import annotations

from dataclasses import dataclass

from boba.indexing import (
    ChunkerId,
    DigestPrefix,
    FixedDigestPrefix,
    KeyEncoder,
    OverlapCharSplitter,
    SectionChunker,
    Sha256TextEncoder,
    SourceBasedChunkId,
)

__all__ = [
    "DEFAULT_CHUNK_OVERLAP",
    "DEFAULT_CHUNK_SIZE",
    "DEFAULT_DIGEST_PREFIX_CHARS",
    "SlidingChunkerConfig",
    "sliding_chunker",
]


DEFAULT_CHUNK_SIZE = 1000
DEFAULT_CHUNK_OVERLAP = 200
DEFAULT_DIGEST_PREFIX_CHARS = 12


@dataclass(frozen=True)
class SlidingChunkerConfig:
    """Конфиг sliding_chunker'а."""

    chunk_size: int = DEFAULT_CHUNK_SIZE
    chunk_overlap: int = DEFAULT_CHUNK_OVERLAP
    digest_prefix_chars: int = DEFAULT_DIGEST_PREFIX_CHARS


def sliding_chunker(
    config: SlidingChunkerConfig,
    *,
    encoder: KeyEncoder[str] | None = None,
    prefix: DigestPrefix | None = None,
) -> SectionChunker[str]:
    """
    Фабрика `SectionChunker[str]` для плоских документов без anchor'ов.

    **Схема** (сборка):
    ```python
    config ──→ OverlapCharSplitter(chunk_size, chunk_overlap)   ─┐
                                                                 ├─→ SectionChunker(chunker_id="sliding")
               SourceBasedChunkId(encoder, prefix)               ─┘
    ```

    ChunkId привязан к `source_id` + хешу контента (anchor отсутствует).
    Изменение контента документа ⇒ новые chunk_id'ы; стабильный source_id
    с тем же контентом ⇒ те же id (idempotent re-index).

    Подходит для: raw text, code, log-файлы.

    **Пример**:
    ```python
    chunker = sliding_chunker(SlidingChunkerConfig(chunk_size=8, chunk_overlap=2))

    sections = iter([
        Section(
            source_id=SourceId("log:1"),
            content="ab cd ef gh ij",   # 14 chars → 2 chunks при chunk_size=8, overlap=2
            anchor=None,                # у плоских документов anchor отсутствует
            order=0,
        ),
    ])

    # Section → 2 chunks; chunk_id привязан к source_id + content-hash (anchor нет).
    list(chunker.stream(ctx, sections)) == [
        Chunk(
            chunk_id=ChunkId("db85588cbf22:0"),       # новое: digest(source_id+content) + ":{chunk_index}"
            source_id=SourceId("log:1"),              # pass из Section
            content="ab cd ef",                       # новое: фрагмент Section.content
            location=ChunkLocation(start=0, end=8),   # новое: offset в Section.content
            anchor=None,                              # pass: anchor у плоских документов отсутствует
            chunk_index=0,                            # новое: per-source counter
            content_hash=None,                        # новое: ставится дальше pipeline'ом
            metadata=Metadata.empty(),                # pass из Section.metadata
            tags=frozenset(),
        ),
        Chunk(
            chunk_id=ChunkId("db85588cbf22:1"),       # тот же digest — content секции тот же
            source_id=SourceId("log:1"),
            content="ef gh ij",
            location=ChunkLocation(start=6, end=14),  # overlap=2 → пересечение с :0
            anchor=None,
            chunk_index=1,
            content_hash=None,
            metadata=Metadata.empty(),
            tags=frozenset(),
        ),
    ]
    ```
    """  # noqa: E501
    return SectionChunker(
        chunker_id=ChunkerId("sliding"),
        splitter=OverlapCharSplitter(
            chunk_size=config.chunk_size,
            chunk_overlap=config.chunk_overlap,
        ),
        id_strategy=SourceBasedChunkId(
            encoder=encoder if encoder is not None else Sha256TextEncoder(),
            prefix=(
                prefix
                if prefix is not None
                else FixedDigestPrefix(config.digest_prefix_chars)
            ),
        ),
    )
