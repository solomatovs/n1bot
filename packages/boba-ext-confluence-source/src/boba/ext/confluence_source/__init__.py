"""Boba indexing extension: Confluence Server/DC source (3 modes).

Регистрирует через entry-point `boba.indexing.sources` три SourceFactory:
- ext.confluence_space — все страницы space'а.
- ext.confluence_pages — явный список page-id'ов.
- ext.confluence_cql   — страницы по CQL-запросу.

Общий backend (base_url + auth + body_format) — секция
`[indexer.sources.confluence]` (ConfluenceCommonSection). Per-mode
параметры — `[indexer.sources.confluence.{space,pages,cql}]`.
"""

from __future__ import annotations

from collections.abc import Iterable

from boba.ext.confluence_source.cql_source import (
    ConfluenceCqlSource,
    ConfluenceCqlSourceFactory,
    ConfluenceCqlSourceSection,
)
from boba.ext.confluence_source.pages_source import (
    ConfluencePagesSource,
    ConfluencePagesSourceFactory,
    ConfluencePagesSourceSection,
)
from boba.ext.confluence_source.space_source import (
    ConfluenceSpaceSource,
    ConfluenceSpaceSourceFactory,
    ConfluenceSpaceSourceSection,
)
from boba.indexing import IndexerExtensionContext, SourceFactory

__all__ = [
    "ConfluenceCqlSource",
    "ConfluenceCqlSourceFactory",
    "ConfluenceCqlSourceSection",
    "ConfluencePagesSource",
    "ConfluencePagesSourceFactory",
    "ConfluencePagesSourceSection",
    "ConfluenceSpaceSource",
    "ConfluenceSpaceSourceFactory",
    "ConfluenceSpaceSourceSection",
    "register_sources",
]


def register_sources(
    ctx: IndexerExtensionContext,
) -> Iterable[SourceFactory]:
    """Entry-point boba.indexing.sources."""
    del ctx
    yield ConfluenceSpaceSourceFactory()
    yield ConfluencePagesSourceFactory()
    yield ConfluenceCqlSourceFactory()
