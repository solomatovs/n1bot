"""ConfluenceCqlSearchRequestSource: один CQL-search-запрос (для tool'ов).

В отличии от `ConfluenceCqlRequestSource`, который использует CQL как
discovery-фазу для индексатора (выдаёт `HttpRequest` на каждую найденную
страницу), этот источник эмитит **один** `HttpRequest` на сам search-endpoint
и оставляет JSON-ответ нетронутым — его разбирает `ConfluenceSearchHitsReader`.
"""

from __future__ import annotations

from collections.abc import Iterable
from urllib.parse import quote

import httpx

from boba.indexing import Metadata, PipelineContext, RequestSource, SourceId
from boba.tool.confluence.keys import ConfluenceKeys
from boba.tool.confluence.request_sources._common import extract_host
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
            f"ConfluenceCqlSearchRequestSource"
            f"(cql={self._cql!r}, limit={self._limit})"
        )

    def _seatch_path(self, cql: str, limit: int) -> str:
        return (
            "/rest/api/content/search?cql={cql}&limit={limit}"
            "&expand=body.view,version,space"
        )

    def stream(self, ctx: PipelineContext) -> Iterable[HttpRequest]:
        del ctx
        path = self._seatch_path(quote(self._cql, safe=""), self._limit)

        yield HttpRequest(
            url=f"{self._base_url}{path}",
            method="GET",
            auth=self._auth,
            source_id=SourceId(f"confluence:cql:{self._cql}"),
            metadata=Metadata.empty().set(ConfluenceKeys.HOST, self._host),
        )

    def list_source_ids(self, ctx: PipelineContext) -> Iterable[str]:
        del ctx
        yield f"confluence:cql:{self._cql}"
