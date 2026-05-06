"""ConfluenceSearchHit: один результат CQL-поиска."""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["ConfluenceSearchHit"]


@dataclass(frozen=True)
class ConfluenceSearchHit:
    """Запись поискового результата Confluence."""

    page_id: str
    title: str
    space_key: str
    url: str
    excerpt: str
    last_modified: str
