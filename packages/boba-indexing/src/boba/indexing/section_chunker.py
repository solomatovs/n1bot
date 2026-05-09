"""
SectionChunker[T] — универсальный Chunker[T] для нарезки Section[T].content на Chunk[T].

Композиция Splitter[T] + ChunkIdStrategy[T] → SectionChunker[T],
который гарантирует корректный трекинг ChunkLocation в исходном Section.content
и стабильные ChunkId для идемпотентной ре-индексации.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Generic, TypeVar

from boba.indexing.chunk_id import ChunkIdStrategy
from boba.indexing.chunker import Chunker, ChunkerId
from boba.indexing.chunks import Chunk
from boba.indexing.context import PipelineContext
from boba.indexing.sections import Section
from boba.indexing.splitter import Splitter

__all__ = ["SectionChunker"]

T = TypeVar("T")


class SectionChunker(Chunker[T], Generic[T]):
    """
    Section[T] → Chunk[T];
    per-source сквозная нумерация chunk_index
    """

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
