"""Chunk: что Chunker выдаёт в Store."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

__all__ = ["Chunk", "ChunkSummary"]


@dataclass(frozen=True)
class Chunk:
    """Один кусок текста для индексирования в vector store.

    `chunk_id` — стабильный составной id, обычно `f"{source_id}#{anchor}:{idx}"`,
    чтобы re-index был идемпотентен (тот же текст → тот же id).

    `content_hash` — версия источника (например `version.number` страницы).
    Store пишет это в metadata, Pipeline сравнивает на следующем прогоне для
    skip-if-unchanged.
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
    """Read-only сводка чанка для оператора (action=show).

    `snippet` — обрезанный preview текста (без полного содержимого), чтобы
    cli-вывод был компактным.
    """

    chunk_id: str
    source_id: str
    anchor: str | None
    chunk_index: int
    snippet: str
    metadata: Mapping[str, str] = field(default_factory=dict)
