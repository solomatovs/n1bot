"""ConfluenceSearchStats: сводка прогона ConfluenceSearchPipeline."""

from __future__ import annotations

from dataclasses import dataclass, field

from boba.ext.confluence_tools._search_pipeline.models import ConfluenceSearchHit

__all__ = ["ConfluenceSearchStats"]


@dataclass(frozen=True)
class ConfluenceSearchStats:
    """Результат поиска: список hits + использованный CQL."""

    cql: str
    hits: list[ConfluenceSearchHit] = field(default_factory=list)
