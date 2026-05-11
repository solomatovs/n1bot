"""ConfluencePagesRequestSource: явно перечисленные page-id'ы."""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from boba.indexing import PipelineContext, RequestSource
from boba.tool.confluence.pipelines.request_sources._common import (
    extract_host,
    make_page_request,
    viewpage_url,
)
from boba.transport.http import AuthApplier, HttpRequest

__all__ = ["ConfluencePagesRequestSource"]


class ConfluencePagesRequestSource(RequestSource[HttpRequest]):
    """Явный список page-id'ов; без discovery — page_ids фиксированы в ctor'е."""

    def __init__(
        self,
        *,
        base_url: str,
        auth: AuthApplier | None,
        page_ids: Sequence[str],
        body_format: str = "export_view",
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._host = extract_host(base_url)
        self._auth = auth
        self._page_ids = list(page_ids)
        self._body_format = body_format

    def name(self) -> str:
        return f"ConfluencePagesRequestSource({len(self._page_ids)} pages)"

    def stream(self, ctx: PipelineContext) -> Iterable[HttpRequest]:
        del ctx
        for page_id in self._page_ids:
            yield make_page_request(
                base_url=self._base_url,
                host=self._host,
                auth=self._auth,
                page_id=page_id,
                body_format=self._body_format,
            )

    def list_source_ids(self, ctx: PipelineContext) -> Iterable[str]:
        del ctx
        for page_id in self._page_ids:
            yield viewpage_url(self._base_url, page_id)
