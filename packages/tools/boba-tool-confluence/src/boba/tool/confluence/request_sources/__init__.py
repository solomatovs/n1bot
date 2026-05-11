"""3 RequestSource'а для Confluence Server REST (CQL / Pages / Space)."""

from __future__ import annotations

from boba.tool.confluence.request_sources.cql import (
    ConfluenceCqlRequestSource,
)
from boba.tool.confluence.request_sources.pages import (
    ConfluencePagesRequestSource,
)
from boba.tool.confluence.request_sources.space import (
    ConfluenceSpaceRequestSource,
)

__all__ = [
    "ConfluenceCqlRequestSource",
    "ConfluencePagesRequestSource",
    "ConfluenceSpaceRequestSource",
]
