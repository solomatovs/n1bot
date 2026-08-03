"""FormatPlan / FormatBlock — DTO-план рендера Section в LLM-формат для format-aware chunker'а."""

from __future__ import annotations

from dataclasses import dataclass

from boba.indexing.location import ChunkLocation

__all__ = ["FormatBlock", "FormatPlan"]


@dataclass(frozen=True)
class FormatBlock:
    """Одна семантическая единица body для chunker'а; is_atomic — «не резать char-split'ом»."""

    format_content: str
    raw_content: str
    location: ChunkLocation
    is_atomic: bool = False


@dataclass(frozen=True)
class FormatPlan:
    """План рендера Section в LLM-формат: blocks + repeat_header/footer + block_glue + breadcrumb-инфо."""

    blocks: tuple[FormatBlock, ...] = ()
    repeat_header: str = ""
    repeat_footer: str = ""
    block_glue: str = "\n\n"
    breadcrumb_level: int | None = None
    breadcrumb_text: str | None = None
