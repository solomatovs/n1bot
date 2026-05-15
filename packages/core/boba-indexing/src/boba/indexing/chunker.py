"""
Chunker[T] - интерфейс для нарезки Section[T] на Chunk[T]
"""

from __future__ import annotations

from abc import abstractmethod
from typing import TypeVar

from typing import NewType

from boba.indexing.chunks import Chunk
from boba.indexing.context import PipelineContext
from boba.indexing.sections import Section
from boba.patterns import StreamTransformer

__all__ = ["Chunker", "ChunkerId"]

T = TypeVar("T")


ChunkerId = NewType("ChunkerId", str)
"""Идентификатор Chunker-реализации (например 'sliding', 'heading')."""


class Chunker(
    StreamTransformer[PipelineContext, Section[T], Chunk[T]],
):
    """
    Преобразует поток секций `Section[T]` в поток чанков `Chunk[T]`

    **Схема**:
    ```python
    Section[T]   ──────────────────────────chunker.stream──→  Iterable[Chunk[T]]
        source_id   ──pass──────────────────────────────→     source_id     (тот же — исходный документ)
        anchor      ──pass──────────────────────────────→     anchor        (тот же — якорь внутри документа)
        metadata    ──merge─────────────────────────────→     metadata      (+ может дополняться ChunkerKeys.*)
        tags        ──pass──────────────────────────────→     tags
        content     ──split──→ piece                    →     content       (фрагмент Section.content)
                                                        →     location      (offset в Section.content)
                                                        →     chunk_index   (0..N-1, сквозной по source_id)
                                                        →     chunk_id      (digest:{chunk_index}, stable)
    ```

    **Контракты для реализации**:
    - `chunk_id` не должен меняться для одной и той-же секции Section[T], он должен быть детерменирован что бы можно было выполнить re-index
    - `chunk_index` — позиция чанка внутри его `source_id` (не внутри `Section[T]`), уникален в паре с anchor
    - `location.start`/`end` — offset в `Section.content`, не во всём документе.

    **Пример**:
    ```python
    # две секции одного документа.
    # первую splitter режет на 2 чанка (длина > chunk_size, есть overlap),
    # вторая помещается в один чанк.
    sections = iter([
        Section(
            source_id=SourceId("doc1"),
            content="ab cd ef gh",          # 11 chars  → 2 chunks при chunk_size=8
            anchor="#intro",
            order=0,
        ),
        Section(
            source_id=SourceId("doc1"),
            content="ij kl",                # 5 chars   → 1 chunk
            anchor="#api",
            order=1,
        ),
    ])

    # конкретная реализация Chunker[str] (factory из boba-chunkers)
    chunker: Chunker[str] = markdown_structural_chunker(
        MarkdownStructuralChunkerConfig(chunk_size=8, chunk_overlap=5),
        encoder=Sha256TextEncoder(),
        prefix=FixedDigestPrefix(12),
    )

    # 3 chunk'а: 2 от #intro + 1 от #api; chunk_index сквозной по source_id.
    list(chunker.stream(ctx, sections)) == [
        Chunk(
            chunk_id=ChunkId("a1b2c3d4e5f6:0"),       # новое: digest(anchor) + ":{chunk_index}"
            source_id=SourceId("doc1"),               # pass из Section
            content="ab cd ef",                       # новое: фрагмент Section.content
            location=ChunkLocation(start=0, end=8),   # новое: offset в Section.content
            anchor="#intro",                          # pass из Section
            chunk_index=0,                            # новое: позиция per source_id
            content_hash=None,                        # новое: ставится дальше pipeline'ом
            metadata=Metadata.empty(),                # merge из Section.metadata (тут пусто)
            tags=frozenset(),                         # pass из Section.tags (тут пусто)
        ),
        Chunk(
            chunk_id=ChunkId("a1b2c3d4e5f6:1"),       # тот же digest, что и у :0 — anchor тот же
            source_id=SourceId("doc1"),
            content="cd ef gh",
            location=ChunkLocation(start=3, end=11),  # overlap с предыдущим chunk'ом (chunk_overlap=5)
            anchor="#intro",
            chunk_index=1,
            content_hash=None,
            metadata=Metadata.empty(),
            tags=frozenset(),
        ),
        Chunk(
            chunk_id=ChunkId("f6e5d4c3b2a1:2"),       # другой digest — другой anchor
            source_id=SourceId("doc1"),
            content="ij kl",
            location=ChunkLocation(start=0, end=5),   # offset внутри Section.content — снова с 0
            anchor="#api",
            chunk_index=2,                            # не 0! счётчик сквозной per source_id
            content_hash=None,
            metadata=Metadata.empty(),
            tags=frozenset(),
        ),
    ]
    ```
    """  # noqa: E501

    @abstractmethod
    def chunker_id(self) -> ChunkerId: ...
