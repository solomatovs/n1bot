"""Chunk: что Chunker выдаёт в Store."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

__all__ = ["Chunk"]


@dataclass(frozen=True)
class Chunk:
    """Один кусок текста для индексирования в vector store.

    `chunk_id` — стабильный составной id, обычно `f"{source_id}#{anchor}:{idx}"`,
    чтобы re-index был идемпотентен (тот же текст → тот же id).
    """

    chunk_id: str
    source_id: str
    text: str
    anchor: str | None = None
    chunk_index: int = 0
    metadata: Mapping[str, str] = field(default_factory=dict)
