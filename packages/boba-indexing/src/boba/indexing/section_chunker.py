"""SectionChunker[T] — Chunker[T] нарезки Section[T].content на Chunk[T].

Композиция `Splitter[T]` + `ChunkIdStrategy[T]`. Domain-чанкер: режет content,
прокидывает `section.metadata` в `chunk.metadata` как есть и считает chunk_id
через стратегию. Не трогает optional-поля (anchor, location-в-исходнике):
их обработка — задача format-specific чанкера, который умеет правильно
интерпретировать данные конкретного парсера.
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
    """`Chunker[T]` композицией `Splitter[T]` + `ChunkIdStrategy[T]`.

    Поведение:

    - `section.content` режется через `splitter` на `SplitPiece[T]`.
    - Из каждого piece собирается `Chunk[T]` с `format_content == raw_content
      == piece.content` (этот chunker не делает LLM-обогащения).
    - `section.metadata` пробрасывается в `chunk.metadata` без модификации.
    - `chunk_index` — per-source сквозной счётчик; обеспечивает уникальность
      `chunk_id`'ов даже при совпадающих ключах стратегии.
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
                key = section.source_id.to_wire()
                idx = per_source_index.get(key, 0)
                per_source_index[key] = idx + 1

                yield Chunk[T](
                    chunk_id=self._id_strategy.compute(section, idx),
                    source_id=section.source_id,
                    format_content=piece.content,
                    raw_content=piece.content,
                    chunk_index=idx,
                    metadata=section.metadata,
                )
