"""ChunkerParams — DTO параметров чанкера; сборку делает StructuralChunkerFactory."""

from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel, Field

from boba.confluence.models import TableShape
from boba.indexing import (
    ChunkerId,
    FixedDigestPrefix,
    Sha256TextEncoder,
    SourceBasedChunkId,
)
from boba.text import OverlapCharSplitter, StructuralChunker
from boba.text.structural_chunker import SplitterFactory

__all__ = ["ChunkerParams", "StructuralChunkerFactory"]


class ChunkerParams(BaseModel):
    """Параметры OverlapCharSplitter для StructuralChunker."""

    chunk_size: int = Field(
        default=4000,
        ge=1,
        description=(
            "Целевой размер `format_content` чанка в символах (передаётся "
            "в `OverlapCharSplitter.chunk_size`). `StructuralChunker` "
            "уменьшает effective-budget на длину `prefix + repeat_header + "
            "repeat_footer`, чтобы итоговый чанк влез в лимит."
        ),
    )
    chunk_overlap: int = Field(
        default=0,
        ge=0,
        description=(
            "Перекрытие между соседними чанками в символах (передаётся в "
            "`OverlapCharSplitter.chunk_overlap`). 0 = без перекрытия."
        ),
    )
    table_shape: TableShape = Field(
        description=(
            "Пороги раскладки таблиц: узкая и длинная режется построчно "
            "записями «колонка: значение», остальные — markdown-сеткой с "
            "шапкой, повторённой в каждом куске."
        ),
    )


class StructuralChunkerFactory:
    """Собирает StructuralChunker из параметров нарезки: режет по заголовкам,
    длинные секции добивает overlap-сплиттером."""

    _CHUNKER_ID: ClassVar[ChunkerId] = ChunkerId("postgres-kb-structural")
    _CHUNK_ID_PREFIX_LENGTH: ClassVar[int] = 16

    def __init__(self, params: ChunkerParams) -> None:
        self._params = params

    def build(self) -> StructuralChunker:
        return StructuralChunker(
            chunker_id=self._CHUNKER_ID,
            splitter_factory=self._splitter_factory(),
            chunk_id_generator=SourceBasedChunkId(
                encoder=Sha256TextEncoder(),
                prefix=FixedDigestPrefix(chars=self._CHUNK_ID_PREFIX_LENGTH),
            ),
            content_hasher=Sha256TextEncoder(),
        )

    def _splitter_factory(self) -> SplitterFactory:
        """Замыкает chunk_size/chunk_overlap; extra_overhead приходит от чанкера."""
        params = self._params

        def factory(extra_overhead: int) -> OverlapCharSplitter:
            return OverlapCharSplitter(
                chunk_size=params.chunk_size,
                chunk_overlap=params.chunk_overlap,
                extra_overhead=extra_overhead,
            )

        return factory
