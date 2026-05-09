"""SectionChunker: универсальный Chunker с DI splitter + ChunkIdStrategy.

Унифицирует Sliding/Heading и любые будущие per-section chunker'ы. Все
различия — через две инжектируемые стратегии:
- splitter: Converter[str, Iterable[str]] — как нарезать text Section'и
- id_strategy: ChunkIdStrategy — как считать стабильный chunk_id

Per-source chunk_index — сквозной по Section'ам одного source_id.
"""

from __future__ import annotations

from collections.abc import Iterable

from boba.indexing.chunk_id import ChunkIdStrategy
from boba.indexing.chunker import Chunker, ChunkerId
from boba.indexing.chunks import Chunk
from boba.patterns import Converter
from boba.processing import IndexingContext, Section

__all__ = ["SectionChunker"]


class SectionChunker(Chunker):
    """Section → Chunk с инжектируемыми splitter и id-стратегией."""

    def __init__(
        self,
        chunker_id: ChunkerId,
        splitter: Converter[str, Iterable[str]],
        id_strategy: ChunkIdStrategy,
    ) -> None:
        self._chunker_id = chunker_id
        self._splitter = splitter
        self._id_strategy = id_strategy

    def name(self) -> str:
        return f"SectionChunker({self._chunker_id.to_wire()})"

    def chunker_id(self) -> ChunkerId:
        return self._chunker_id

    def stream(
        self, ctx: IndexingContext, stream: Iterable[Section],
    ) -> Iterable[Chunk]:
        del ctx
        per_source_index: dict[str, int] = {}
        for section in stream:
            for piece in self._splitter.convert(section.text):
                idx = per_source_index.get(section.source_id, 0)
                per_source_index[section.source_id] = idx + 1
                yield Chunk(
                    chunk_id=self._id_strategy.compute(section, idx),
                    source_id=section.source_id,
                    text=piece,
                    anchor=section.anchor,
                    chunk_index=idx,
                    metadata=dict(section.metadata),
                )
