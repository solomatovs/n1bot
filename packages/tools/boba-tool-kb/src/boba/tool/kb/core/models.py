"""Локальные DTO KB-плагина (response-форматы для tools)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

__all__ = ["SearchHit"]


@dataclass(frozen=True)
class SearchHit:
    """Один результат kb_search_hybrid.

    `distance` в гибридной выдаче не имеет «единого» физического смысла:
    это **отрицательный RRF-скор** (`-rrf`), семантика «меньше = ближе/
    релевантнее». Чистый cosine-distance — у `PostgresKnowledgeBase.vector_search`.
    """

    id: str
    distance: float
    metadata: Mapping[str, str]
    snippet: str
