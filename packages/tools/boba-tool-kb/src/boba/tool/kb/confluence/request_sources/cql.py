"""ConfluenceCqlRequestSource: CQL-запрос → пагинированные результаты."""

from __future__ import annotations

from collections.abc import Iterable

from boba.indexing import PipelineContext, RequestSource
from boba.tool.kb.confluence.connection import ConfluenceConnection
from boba.tool.kb.confluence.request_sources._common import (
    confluence_discover_pages_by_cql,
    extract_host,
    make_page_request,
)
from boba.transport.http import HttpRequest

__all__ = ["ConfluenceCqlRequestSource"]


class ConfluenceCqlRequestSource(RequestSource[HttpRequest]):
    """CQL-запрос: `space = DOCS AND lastModified > '2024-01-01'` и т.п.

    Discovery — через `/rest/api/content/search?cql=...` с пагинацией.
    """

    def __init__(
        self,
        *,
        conn: ConfluenceConnection,
        cql: str,
        body_format: str = "export_view",
    ) -> None:
        self._conn = conn
        self._cql = cql
        self._body_format = body_format
        self._host = extract_host(conn.base_url)

    def name(self) -> str:
        return f"ConfluenceCqlRequestSource({self._cql!r})"

    def stream(self, ctx: PipelineContext) -> Iterable[HttpRequest]:
        del ctx
        auth = self._conn.make_auth()
        for page_id in self._iter_page_ids():
            yield make_page_request(
                base_url=self._conn.base_url,
                host=self._host,
                auth=auth,
                page_id=page_id,
                body_format=self._body_format,
            )

    def _iter_page_ids(self) -> Iterable[str]:
        return confluence_discover_pages_by_cql(self._conn, self._cql)
