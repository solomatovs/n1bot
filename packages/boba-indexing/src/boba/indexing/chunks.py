"""Chunk: что Chunker выдаёт в Store."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

__all__ = ["Chunk", "ChunkSummary"]


@dataclass(frozen=True)
class Chunk:
    """
    Один кусок текста для индексирования в vector store
    """

    chunk_id: str
    source_id: str
    text: str
    anchor: str | None = None
    chunk_index: int = 0
    content_hash: str = ""
    metadata: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ChunkSummary:
    """
    Read-only сводка чанка для оператора
    """

    chunk_id: str
    source_id: str
    anchor: str | None
    chunk_index: int
    snippet: str
    metadata: Mapping[str, str] = field(default_factory=dict)
