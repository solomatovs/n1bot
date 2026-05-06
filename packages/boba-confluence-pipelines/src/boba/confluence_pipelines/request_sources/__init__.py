"""3 RequestSource'а для Confluence Server REST (CQL / Pages / Space)."""

from __future__ import annotations

from boba.confluence_pipelines.request_sources.cql import (
    ConfluenceCqlRequestSource,
)
from boba.confluence_pipelines.request_sources.pages import (
    ConfluencePagesRequestSource,
)
from boba.confluence_pipelines.request_sources.space import (
    ConfluenceSpaceRequestSource,
)

__all__ = [
    "ConfluenceCqlRequestSource",
    "ConfluencePagesRequestSource",
    "ConfluenceSpaceRequestSource",
]
