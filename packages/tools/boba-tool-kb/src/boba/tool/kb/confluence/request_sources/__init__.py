"""3 RequestSource'а для Confluence Server REST (CQL / Pages / Space)."""

from __future__ import annotations

from boba.tool.kb.confluence.request_sources.cql import (
    ConfluenceCqlRequestSource,
)
from boba.tool.kb.confluence.request_sources.pages import (
    ConfluencePagesRequestSource,
)
from boba.tool.kb.confluence.request_sources.space import (
    ConfluenceMultiSpaceRequestSource,
    ConfluenceSpaceRequestSource,
)

__all__ = [
    "ConfluenceCqlRequestSource",
    "ConfluenceMultiSpaceRequestSource",
    "ConfluencePagesRequestSource",
    "ConfluenceSpaceRequestSource",
]
