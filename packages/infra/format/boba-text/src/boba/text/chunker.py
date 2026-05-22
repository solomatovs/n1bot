"""SectionChunker[T] — `Chunker[T]` нарезки `Section[T].content` на `Chunk[T]`.

Композиция `Splitter[T]` + `ChunkIdStrategy[T]`. Domain-нейтральный по API
(generic в `T`), но pipeline-модель «split content на куски с char-offset'ами»
естественна для текста — потому живёт в text-package, а не в `boba-indexing`.

Поведение:

- `section.content` режется через `splitter` на `SplitPiece[T]`.
- Из каждого piece собирается `Chunk[T]` с `format_content == raw_content
  == piece.content` (этот chunker не делает LLM-обогащения).
- `section.metadata` пробрасывается в `chunk.metadata` без модификации —
  обработку optional-полей (anchor, source-offset'ы) делает format-specific
  чанкер, который умеет правильно интерпретировать данные конкретного парсера.
- `chunk_index` — per-source сквозной счётчик; обеспечивает уникальность
  `chunk_id`'ов даже при совпадающих ключах стратегии.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import TypeVar

from boba.indexing import (
    Chunk,
    Chunker,
    ChunkerId,
    ChunkIdGenerator,
    KeyEncoder,
    PipelineContext,
    Section,
    Splitter,
)

__all__ = ["SectionChunker"]

T = TypeVar("T")


class SectionChunker(Chunker[T]):
    """`Chunker[T]` композицией `Splitter[T]` + `ChunkIdStrategy[T]`."""

    def __init__(
        self,
        chunker_id: ChunkerId,
        splitter: Splitter[T],
        id_strategy: ChunkIdGenerator[T],
        content_hasher: KeyEncoder[T],
    ) -> None:
        self._chunker_id = chunker_id
        self._splitter = splitter
        self._id_strategy = id_strategy
        self._content_hasher = content_hasher

    def name(self) -> str:
        return f"SectionChunker({self._chunker_id})"

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
            chunk_metadata = section.metadata.merge(section.to_chunk_metadata())
            for piece in self._splitter.split(section.content):
                key = section.source_id
                idx = per_source_index.get(key, 0)
                per_source_index[key] = idx + 1

                yield Chunk[T](
                    chunk_id=self._id_strategy.compute(section, idx),
                    source_id=section.source_id,
                    format_content=piece.content,
                    raw_content=piece.content,
                    chunk_index=idx,
                    content_hash=self._content_hasher.encode(piece.content),
                    metadata=chunk_metadata,
                )
