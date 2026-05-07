"""Value-objects, которые KB отдаёт tools."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class CollectionInfo:
    """Описание одной коллекции для kb_list_collections."""

    name: str
    description: str


@dataclass(frozen=True)
class SearchHit:
    """Один результат kb_search; distance — chromadb (меньше = ближе)."""

    id: str
    distance: float
    metadata: Mapping[str, str]
    snippet: str
