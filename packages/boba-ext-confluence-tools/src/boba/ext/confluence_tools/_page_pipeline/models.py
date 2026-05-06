"""DTO для результата ConfluencePagePipeline.fetch()."""

from __future__ import annotations

from dataclasses import dataclass, field

__all__ = ["ConfluencePageContent", "ConfluencePageHeading"]


@dataclass(frozen=True)
class ConfluencePageHeading:
    """Один заголовок (h1..h6) с anchor'ом для последующего section-fetch'а."""

    level: int
    text: str
    anchor: str


@dataclass(frozen=True)
class ConfluencePageContent:
    """Метаданные + body конкретной страницы Confluence."""

    page_id: str
    title: str
    space_key: str
    url: str
    version: str
    last_modified: str
    body_html: str
    headings: list[ConfluencePageHeading] = field(default_factory=list)
