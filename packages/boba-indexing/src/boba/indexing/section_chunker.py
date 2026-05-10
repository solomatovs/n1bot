"""
SectionChunker[T] — универсальный Chunker[T] для нарезки Section[T].content на Chunk[T].

Композиция Splitter[T] + ChunkIdStrategy[T] → SectionChunker[T],
который гарантирует корректный трекинг ChunkLocation в исходном Section.content
и стабильные ChunkId для идемпотентной ре-индексации.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import TypeVar

from boba.indexing.chunk_id import ChunkIdStrategy
from boba.indexing.chunker import Chunker, ChunkerId
from boba.indexing.chunks import Chunk
from boba.indexing.context import PipelineContext
from boba.indexing.sections import Section
from boba.indexing.splitter import Splitter

__all__ = ["SectionChunker"]

T = TypeVar("T")


class SectionChunker(Chunker[T]):
    """
    Универсальная реализация `Chunker[T]`: композиция `Splitter[T]` + `ChunkIdStrategy[T]`.

    **Схема**:
    ```python
    Section[T]   ──────────────────────────chunker.stream──→  Iterable[Chunk[T]]
        source_id   ──pass──────────────────────────────→     source_id
        anchor      ──pass──────────────────────────────→     anchor
        metadata    ──pass──────────────────────────────→     metadata
        content     ──splitter.split──→ SplitPiece[T]   →     content       (← piece.content)
                                                        →     location      (← piece.location)
                                                        →     chunk_index   (per-source counter)
                                   id_strategy.compute(section, idx)
                                                        →     chunk_id      (stable ChunkId)
    ```

    `chunk_index` — per-source сквозной счётчик: разные секции одного source_id
    получат разные chunk_index (`0..N-1`), даже если резка дала одинаковый контент.

    **Пример**:
    ```python
    chunker = SectionChunker(
        chunker_id=ChunkerId("heading"),
        splitter=OverlapCharSplitter(chunk_size=8, chunk_overlap=2),
        id_strategy=AnchorBasedChunkId(
            encoder=Sha256TextEncoder(),
            prefix=FixedDigestPrefix(12),
        ),
    )

    sections = iter([
        Section(
            source_id=SourceId("doc1"),
            content="ab cd ef gh ij",     # 14 chars → 2 chunks при chunk_size=8, overlap=2
            anchor="#s1",
            order=0,
        ),
    ])

    # Section → 2 chunks; chunk_id'ы делят digest, chunk_index растёт.
    list(chunker.stream(ctx, sections)) == [
        Chunk(
            chunk_id=ChunkId("d37c2a97056f:0"),       # новое: digest(anchor) + ":{chunk_index}"
            source_id=SourceId("doc1"),               # pass из Section
            content="ab cd ef",                       # новое: ← piece.content
            location=ChunkLocation(start=0, end=8),   # новое: ← piece.location
            anchor="#s1",                             # pass из Section
            chunk_index=0,                            # новое: per-source counter
            content_hash=None,                        # новое: ставится дальше pipeline'ом
            metadata=Metadata.empty(),                # pass из Section.metadata (тут пусто)
            tags=frozenset(),                         # default (Section.tags не пробрасывается этой реализацией)
        ),
        Chunk(
            chunk_id=ChunkId("d37c2a97056f:1"),       # тот же digest — anchor тот же
            source_id=SourceId("doc1"),
            content="ef gh ij",
            location=ChunkLocation(start=6, end=14),  # overlap=2 → пересечение с :0
            anchor="#s1",
            chunk_index=1,
            content_hash=None,
            metadata=Metadata.empty(),
            tags=frozenset(),
        ),
    ]
    ```
    """  # noqa: E501

    def __init__(
        self,
        chunker_id: ChunkerId,
        splitter: Splitter[T],
        id_strategy: ChunkIdStrategy[T],
    ) -> None:
        self._chunker_id = chunker_id
        self._splitter = splitter
        self._id_strategy = id_strategy

    def name(self) -> str:
        return f"SectionChunker({self._chunker_id.to_wire()})"

    def chunker_id(self) -> ChunkerId:
        return self._chunker_id

    def reset(self) -> None:
        pass

    def stream(
        self,
        ctx: PipelineContext,
        stream: Iterable[Section[T]],
    ) -> Iterable[Chunk[T]]:
        del ctx
        per_source_index: dict[str, int] = {}
        for section in stream:
            for piece in self._splitter.split(section.content):
                # id секции + порядковый индекс чанка внутри секции
                key = section.source_id.to_wire()
                idx = per_source_index.get(key, 0)
                per_source_index[key] = idx + 1

                yield Chunk[T](
                    chunk_id=self._id_strategy.compute(section, idx),
                    source_id=section.source_id,
                    content=piece.content,
                    location=piece.location,
                    anchor=section.anchor,
                    chunk_index=idx,
                    metadata=section.metadata,
                )
