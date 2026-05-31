"""Локальные DTO KB-плагина (response-форматы для tools)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

__all__ = ["SearchHit"]


@dataclass(frozen=True)
class SearchHit:
    """Один результат kb_search_* (vector/fts).

    `distance` — cosine-distance (`vector_search`) либо отрицательный
    `ts_rank_cd`-скор (`fts_search`); в обоих случаях семантика
    «меньше = ближе/релевантнее».
    """

    id: str
    distance: float
    metadata: Mapping[str, str]
    snippet: str
