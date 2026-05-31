"""`ChunkerParams` — параметры структурного чанкера + factory `StructuralChunker`.

`BaseModel` (не settings), встраивается как nested-поле в ingest-tool-конфиги
(`confluence_ingest_*`) и CLI-конфиги. `ChunkerParams.build_chunker()` собирает
heading-aware `StructuralChunker` с `OverlapCharSplitter` для size-cap;
`key_encoder=Sha256TextEncoder()` хэширует `format_content` каждого чанка в
`content_hash` — по нему `IndexSink.reconcile` решает skip vs upsert.
"""

from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel, Field

from boba.indexing import (
    ChunkerId,
    FixedDigestPrefix,
    Sha256TextEncoder,
    SourceBasedChunkId,
)
from boba.text import OverlapCharSplitter, StructuralChunker
from boba.text.structural_chunker import SplitterFactory

__all__ = ["ChunkerParams"]


class ChunkerParams(BaseModel):
    """Параметры `OverlapCharSplitter` для `StructuralChunker`."""

    _CHUNKER_ID: ClassVar[ChunkerId] = ChunkerId("postgres-kb-structural")
    _CHUNK_ID_PREFIX_LENGTH: ClassVar[int] = 16

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

    def build_chunker(self) -> StructuralChunker:
        """
        Собирает чанкер: режет документ по заголовкам, длинные секции добивает
        разбиением с перекрытием по символам до заданного размера (overlap).

        content_hasher считает SHA-256 по содержимому чанка — хэш позволяет
        IndexSink решить, нужно ли повторно индексировать чанк.
        chunk_id_generator считает SHA-256 по source_id с префиксом —
        chunk_id уникален в рамках всей таблицы kb.
        """
        return StructuralChunker(
            chunker_id=self._CHUNKER_ID,
            splitter_factory=self._make_splitter_factory(
                chunk_size=self.chunk_size,
                chunk_overlap=self.chunk_overlap,
            ),
            chunk_id_generator=SourceBasedChunkId(
                encoder=Sha256TextEncoder(),
                prefix=FixedDigestPrefix(chars=self._CHUNK_ID_PREFIX_LENGTH),
            ),
            content_hasher=Sha256TextEncoder(),
        )

    @staticmethod
    def _make_splitter_factory(
        *,
        chunk_size: int,
        chunk_overlap: int,
    ) -> SplitterFactory:
        """Замыкаем chunk_size/chunk_overlap; StructuralChunker дёргает на каждой
        секции с `extra_overhead = len(prefix + repeat_header + repeat_footer)`,
        чтобы итоговый чанк влез в budget."""

        def factory(extra_overhead: int) -> OverlapCharSplitter:
            return OverlapCharSplitter(
                chunk_size=chunk_size,
                chunk_overlap=chunk_overlap,
                extra_overhead=extra_overhead,
            )

        return factory
