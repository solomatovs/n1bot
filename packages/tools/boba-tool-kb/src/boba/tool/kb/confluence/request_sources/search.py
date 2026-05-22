"""ConfluenceCqlSearchRequestSource: один CQL-search-запрос (для tool'ов).

В отличии от `ConfluenceCqlRequestSource`, который использует CQL как
discovery-фазу для индексатора (выдаёт `HttpRequest` на каждую найденную
страницу), этот источник эмитит **один** `HttpRequest` на сам search-endpoint
и оставляет JSON-ответ нетронутым — его разбирает `ConfluenceSearchHitsReader`.
"""

from __future__ import annotations

from collections.abc import Iterable

import httpx

from boba.indexing import Metadata, PipelineContext, RequestSource, SourceId
from boba.tool.kb.confluence.keys import ConfluenceKeys
from boba.tool.kb.confluence.request_sources._common import (
    cql_search_path,
    extract_host,
)
from boba.transport.http import HttpRequest

__all__ = ["ConfluenceCqlSearchRequestSource"]


class ConfluenceCqlSearchRequestSource(RequestSource[HttpRequest]):
    """CQL-запрос → один `HttpRequest` на `/content/search`."""

    def __init__(
        self,
        *,
        base_url: str,
        auth: httpx.Auth | None,
        cql: str,
        limit: int,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._host = extract_host(base_url)
        self._auth = auth
        self._cql = cql
        self._limit = limit

    def name(self) -> str:
        return (
            f"ConfluenceCqlSearchRequestSource(cql={self._cql!r}, limit={self._limit})"
        )

    def stream(self, ctx: PipelineContext) -> Iterable[HttpRequest]:
        del ctx
        path = cql_search_path(
            self._cql,
            limit=self._limit,
            expand="body.view,version,space",
        )

        yield HttpRequest(
            url=f"{self._base_url}{path}",
            method="GET",
            auth=self._auth,
            source_id=SourceId(f"confluence:cql:{self._cql}"),
            metadata=Metadata.empty().set(ConfluenceKeys.HOST, self._host),
        )

