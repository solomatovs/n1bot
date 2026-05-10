"""ConfluenceCqlRequestSource: CQL-запрос → пагинированные результаты."""

from __future__ import annotations

from collections.abc import Iterable
from urllib.parse import quote

from boba.ext.confluence_tools.pipelines.request_sources._common import (
    extract_host,
    iter_paginated,
    make_discovery_client,
    make_page_request,
    viewpage_url,
)
from boba.indexing import PipelineContext, RequestSource
from boba.transport.http import AuthApplier, HttpRequest

__all__ = ["ConfluenceCqlRequestSource"]

_LIST_LIMIT = 50
_LIST_PATH = "/rest/api/content/search?cql={cql}&limit={limit}"


class ConfluenceCqlRequestSource(RequestSource[HttpRequest]):
    """CQL-запрос: `space = DOCS AND lastModified > '2024-01-01'` и т.п.

    Discovery — через `/rest/api/content/search?cql=...` с пагинацией.
    """

    def __init__(
        self,
        *,
        base_url: str,
        auth: AuthApplier | None,
        cql: str,
        body_format: str = "export_view",
        timeout_sec: float = 30.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._host = extract_host(base_url)
        self._auth = auth
        self._cql = cql
        self._body_format = body_format
        self._timeout = timeout_sec

    def name(self) -> str:
        return f"ConfluenceCqlRequestSource({self._cql!r})"

    def stream(self, ctx: PipelineContext) -> Iterable[HttpRequest]:
        del ctx
        for page_id in self._iter_page_ids():
            yield make_page_request(
                base_url=self._base_url,
                host=self._host,
                auth=self._auth,
                page_id=page_id,
                body_format=self._body_format,
            )

    def list_source_ids(self, ctx: PipelineContext) -> Iterable[str]:
        del ctx
        for page_id in self._iter_page_ids():
            yield viewpage_url(self._base_url, page_id)

    def _iter_page_ids(self) -> Iterable[str]:
        path = _LIST_PATH.format(cql=quote(self._cql, safe=""), limit=_LIST_LIMIT)
        with make_discovery_client(self._base_url, self._auth, self._timeout) as client:
            for raw in iter_paginated(client, path):
                page_id = str(raw.get("id") or "").strip()
                if page_id:
                    yield page_id
