"""SearchStats: что вернул прогон SearchPipeline."""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["SearchStats"]


@dataclass(frozen=True)
class SearchStats:
    """Сводка прогона: число hits + размерность embedding'а коллекции."""

    hits_returned: int
    embedding_dim: int = 0
