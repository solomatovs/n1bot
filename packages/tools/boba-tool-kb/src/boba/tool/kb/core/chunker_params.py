"""`ChunkerParams` — переиспользуемые параметры структурного чанкера.

`BaseModel` (не settings), встраивается как nested-поле в ingest-tool-конфиги
(`files_ingest`, `confluence_*_ingest`). Передаётся в `StructuralChunker`
через `OverlapCharSplitter(chunk_size, chunk_overlap)` factory.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

__all__ = ["ChunkerParams"]


class ChunkerParams(BaseModel):
    """Параметры `OverlapCharSplitter` для `StructuralChunker`."""

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
