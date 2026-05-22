"""`build_chunker` — factory `StructuralChunker` из `ChunkerParams`.

Heading-aware Chunker с `OverlapCharSplitter` для size-cap.
`key_encoder=Sha256TextEncoder()` хэширует `format_content` каждого чанка
в `content_hash` — это то по чему `IndexSink.reconcile` решает skip vs upsert.
"""

from __future__ import annotations

from boba.indexing import (
    ChunkerId,
    FixedDigestPrefix,
    Sha256TextEncoder,
    SourceBasedChunkId,
)
from boba.text import OverlapCharSplitter, StructuralChunker
from boba.text.structural_chunker import SplitterFactory
from boba.tool.kb.core.chunker_params import ChunkerParams

__all__ = ["build_chunker"]

_CHUNKER_ID: ChunkerId = ChunkerId("postgres-kb-structural")
_CHUNK_ID_PREFIX_LENGTH: int = 16


def build_chunker(params: ChunkerParams) -> StructuralChunker:
    """Heading-aware Chunker с `OverlapCharSplitter` для size-cap.

    `key_encoder=Sha256TextEncoder()` хэширует `format_content` каждого
    чанка в `content_hash` — это то по чему `IndexSink.reconcile` решает
    skip vs upsert. Отдельный от `id_strategy.encoder` инстанс (тот хэширует
    `source_id` для `chunk_id`), хотя оба используют SHA-256.
    """
    return StructuralChunker(
        chunker_id=_CHUNKER_ID,
        splitter_factory=_make_splitter_factory(
            chunk_size=params.chunk_size,
            chunk_overlap=params.chunk_overlap,
        ),
        chunk_id_generator=SourceBasedChunkId(
            encoder=Sha256TextEncoder(),
            prefix=FixedDigestPrefix(chars=_CHUNK_ID_PREFIX_LENGTH),
        ),
        content_hasher=Sha256TextEncoder(),
    )


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
