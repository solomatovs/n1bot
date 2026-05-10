"""
HeadingChunker — фабрика секций SectionChunker[str] в которой есть
к чему привязать чанк, некий якорь сообщения - anchor

Подходит для форматов где есть anchor (якорь), это
    Markdown
    HTML
    Confluence storage
"""

from __future__ import annotations

from dataclasses import dataclass

from boba.indexing import (
    AnchorBasedChunkId,
    ChunkerId,
    DigestPrefix,
    KeyEncoder,
    OverlapCharSplitter,
    SectionChunker,
)

__all__ = [
    "DEFAULT_CHUNK_OVERLAP",
    "DEFAULT_CHUNK_SIZE",
    "DEFAULT_DIGEST_PREFIX_CHARS",
    "HeadingChunkerConfig",
    "heading_chunker",
]


DEFAULT_CHUNK_SIZE = 1500
DEFAULT_CHUNK_OVERLAP = 150
DEFAULT_DIGEST_PREFIX_CHARS = 12


@dataclass(frozen=True)
class HeadingChunkerConfig:
    """Конфиг heading_chunker"""

    chunk_size: int = DEFAULT_CHUNK_SIZE
    chunk_overlap: int = DEFAULT_CHUNK_OVERLAP
    digest_prefix_chars: int = DEFAULT_DIGEST_PREFIX_CHARS


def heading_chunker(
    config: HeadingChunkerConfig,
    encoder: KeyEncoder[str],
    prefix: DigestPrefix,
) -> SectionChunker[str]:
    """
    Фабрика `SectionChunker[str]` для документов с anchor'ами (Markdown, HTML, …).

    **Схема** (сборка):
    ```python
    config ──→ OverlapCharSplitter(chunk_size, chunk_overlap)   ─┐
                                                                 ├─→ SectionChunker(chunker_id="heading")
               AnchorBasedChunkId(encoder, prefix)               ─┘
    ```

    ChunkId привязан к `Section.anchor` (heading-id, fragment) — re-index
    с тем же anchor'ом и тем же контентом даёт идентичные chunk_id'ы;
    переименование heading'а ⇒ новый id (старый удалится reconcile'ом).

    **Пример**:
    ```python
    chunker = heading_chunker(
        HeadingChunkerConfig(chunk_size=8, chunk_overlap=2),
        encoder=Sha256TextEncoder(),
        prefix=FixedDigestPrefix(12),
    )

    sections = iter([
        Section(
            source_id=SourceId("doc1"),
            content="ab cd ef gh",   # 11 chars → 2 chunks при chunk_size=8, overlap=2
            anchor="#intro",
            order=0,
        ),
    ])

    # Section #intro → 2 chunks; chunk_id привязан к anchor (digest одинаков).
    list(chunker.stream(ctx, sections)) == [
        Chunk(
            chunk_id=ChunkId("536a3432d124:0"),       # новое: digest(anchor) + ":{chunk_index}"
            source_id=SourceId("doc1"),               # pass из Section
            content="ab cd ef",                       # новое: фрагмент Section.content
            location=ChunkLocation(start=0, end=8),   # новое: offset в Section.content
            anchor="#intro",                          # pass из Section
            chunk_index=0,                            # новое: per-source counter
            content_hash=None,                        # новое: ставится дальше pipeline'ом
            metadata=Metadata.empty(),                # pass из Section.metadata
            tags=frozenset(),
        ),
        Chunk(
            chunk_id=ChunkId("536a3432d124:1"),       # тот же digest — anchor тот же
            source_id=SourceId("doc1"),
            content="ef gh",
            location=ChunkLocation(start=6, end=11),  # overlap=2 → пересечение с :0
            anchor="#intro",
            chunk_index=1,
            content_hash=None,
            metadata=Metadata.empty(),
            tags=frozenset(),
        ),
    ]
    ```
    """  # noqa: E501
    return SectionChunker(
        chunker_id=ChunkerId("heading"),
        splitter=OverlapCharSplitter(
            chunk_size=config.chunk_size,
            chunk_overlap=config.chunk_overlap,
        ),
        id_strategy=AnchorBasedChunkId(
            encoder=encoder,
            prefix=prefix,
        ),
    )
