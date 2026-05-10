"""markdown_aware_chunker — фабрика SectionChunker[str] с MarkdownAwareSplitter.

Аналог `heading_chunker`, но использует `MarkdownAwareSplitter` —
рекурсивный splitter с markdown-aware separators (horizontal rules →
paragraph break → обычные `\\n` / ` ` / `""`).

Подходит когда `Section.content` содержит сам markdown-разметку (например
после `HtmlMarkdownifyReader` или `MarkdownReader`) — резка происходит по
границам markdown-блоков, а не вслепую по символам, и code-fence остаётся
целым в одной piece.

ChunkId — `AnchorBasedChunkId` (как у `heading_chunker`): chunk_id выводится
из `(source_id, anchor)` и стабилен между ре-индексациями.
"""

from __future__ import annotations

from dataclasses import dataclass

from boba.chunkers.markdown_splitter import MarkdownAwareSplitter
from boba.indexing import (
    AnchorBasedChunkId,
    ChunkerId,
    DigestPrefix,
    KeyEncoder,
    SectionChunker,
)

__all__ = [
    "DEFAULT_CHUNK_OVERLAP",
    "DEFAULT_CHUNK_SIZE",
    "DEFAULT_DIGEST_PREFIX_CHARS",
    "MarkdownAwareChunkerConfig",
    "markdown_aware_chunker",
]


DEFAULT_CHUNK_SIZE = 1500
DEFAULT_CHUNK_OVERLAP = 150
DEFAULT_DIGEST_PREFIX_CHARS = 12


@dataclass(frozen=True)
class MarkdownAwareChunkerConfig:
    """Конфиг markdown_aware_chunker."""

    chunk_size: int = DEFAULT_CHUNK_SIZE
    chunk_overlap: int = DEFAULT_CHUNK_OVERLAP
    digest_prefix_chars: int = DEFAULT_DIGEST_PREFIX_CHARS


def markdown_aware_chunker(
    config: MarkdownAwareChunkerConfig,
    encoder: KeyEncoder[str],
    prefix: DigestPrefix,
) -> SectionChunker[str]:
    """
    Фабрика `SectionChunker[str]` с `MarkdownAwareSplitter` под капотом

    Splitter режет markdown с приоритетом:
        horizontal rules → paragraph break → обычные separators.

    Это даёт более читабельные чанки чем `heading_chunker` (тот режет по символам внутри Section),
    и при этом сохраняет code-fence целым в одной piece
    (paragraph-break не попадает внутрь обычного fence).

    **Когда применять** (vs. `heading_chunker`):
    - Section.content уже содержит markdown-разметку (heading `# `, code-fence ` ``` `, списки `- ...`, таблицы, horizontal rules `---`).
      Типично после `HtmlMarkdownifyReader` или `MarkdownReader` с длинными Section
    - Хочется чтобы code-блоки и таблицы попадали в один чанк целиком.

    **Pipeline-цепочка** (типичный случай):
    ```
    HtmlMarkdownifyReader  →  Section[str] (markdown content)
        ↓
    markdown_aware_chunker →  Chunk[str]   (режет по markdown-границам)
    ```

    **Пример** (одна большая Section с code-fence и horizontal rule → 3 чанка;
    code-fence остаётся целым в одном чанке, маркеры heading сохранены):
    ```python
    chunker = markdown_aware_chunker(
        MarkdownAwareChunkerConfig(chunk_size=70, chunk_overlap=0),
        encoder=Sha256TextEncoder(),
        prefix=FixedDigestPrefix(12),
    )

    md = '''# Intro

    intro paragraph.
    '''

    ```python
    def f():
        return 1
    ```

    middle paragraph.

    ---

    bottom paragraph.'''
    # len(md) == 105

    sections = iter([
        Section(
            source_id=SourceId("doc1"),
            content=md,
            anchor="intro",
            order=0,
        ),
    ])

    # 1 Section → 3 chunks. Splitter режет сначала по `\\n---\\n`,
    # потом по `\\n\\n`. Code-fence и heading-маркер остаются целыми.
    list(chunker.stream(ctx, sections)) == [
        Chunk(
            chunk_id=ChunkId("4b347ccda55a:0"),       # новое: digest(anchor) + ":0"
            source_id=SourceId("doc1"),               # pass из Section
            content=(                                 # новое: фрагмент Section.content
                "# Intro\\n\\n"                        # heading-маркер сохранён
                "intro paragraph.\\n\\n"
                "```python\\ndef f():\\n    return 1\\n```"  # code-fence ЦЕЛЫЙ
            ),
            location=ChunkLocation(start=0, end=62),  # новое: offset в Section.content
            anchor="intro",                           # pass из Section
            chunk_index=0,                            # новое: per-source counter
            content_hash=None,
            metadata=Metadata.empty(),
            tags=frozenset(),
        ),
        Chunk(
            chunk_id=ChunkId("4b347ccda55a:1"),       # тот же digest — anchor тот же
            source_id=SourceId("doc1"),
            content="middle paragraph.\\n",
            location=ChunkLocation(start=64, end=82), # после `\\n---\\n` отрезано
            anchor="intro",
            chunk_index=1,
            content_hash=None,
            metadata=Metadata.empty(),
            tags=frozenset(),
        ),
        Chunk(
            chunk_id=ChunkId("4b347ccda55a:2"),
            source_id=SourceId("doc1"),
            content="\\nbottom paragraph.",
            location=ChunkLocation(start=87, end=105),
            anchor="intro",
            chunk_index=2,
            content_hash=None,
            metadata=Metadata.empty(),
            tags=frozenset(),
        ),
    ]
    ```

    **Что показывает пример** (vs. `heading_chunker` с теми же параметрами):
    - Code-fence ` ```python\\ndef f():\\n    return 1\\n``` ` остался в одной
      piece — `heading_chunker` (символьный) скорее всего разорвал бы его
      посередине, оставив чанк с открытым ` ``` ` без закрытия.
    - Heading-маркер `# Intro` сохранён — paragraph-break вокруг heading'а
      даёт границу резки **снаружи** маркера.
    - Резка по horizontal rule `---` — между логическими разделами документа.
    """  # noqa: E501
    return SectionChunker(
        chunker_id=ChunkerId("markdown_aware"),
        splitter=MarkdownAwareSplitter(
            chunk_size=config.chunk_size,
            chunk_overlap=config.chunk_overlap,
        ),
        id_strategy=AnchorBasedChunkId(
            encoder=encoder,
            prefix=prefix,
        ),
    )
