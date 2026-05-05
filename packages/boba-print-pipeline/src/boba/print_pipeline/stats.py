"""PrintStats: что напечатал PrintPipeline за один прогон."""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["PrintStats"]


@dataclass(frozen=True)
class PrintStats:
    """Сводка прогона PrintPipeline: счётчики + размерность embedding'а коллекции."""

    chunks_printed: int
    sources_seen: int
    embedding_dim: int = 0
