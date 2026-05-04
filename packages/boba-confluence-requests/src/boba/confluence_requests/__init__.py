"""boba-ext-confluence-requests: 3 RequestSource'а для Confluence Server REST."""

from __future__ import annotations

from boba.confluence_requests.cql_request_source import (
    ConfluenceCqlRequestSource,
)
from boba.confluence_requests.pages_request_source import (
    ConfluencePagesRequestSource,
)
from boba.confluence_requests.space_request_source import (
    ConfluenceSpaceRequestSource,
)

__all__ = [
    "ConfluenceCqlRequestSource",
    "ConfluencePagesRequestSource",
    "ConfluenceSpaceRequestSource",
]
