"""ConfluenceSpaceRequestSource: все страницы одного space."""

from __future__ import annotations

from collections.abc import Iterable

from boba.ext.confluence_tools.pipelines.request_sources._common import (
    extract_host,
    iter_paginated,
    make_discovery_client,
    make_page_request,
    viewpage_url,
)
from boba.http_transport import HttpRequest
from boba.processing import AuthApplier, PipelineContext, RequestSource

__all__ = ["ConfluenceSpaceRequestSource"]

_LIST_LIMIT = 50
_LIST_PATH = "/rest/api/space/{key}/content?type=page&limit={limit}&start=0"


class ConfluenceSpaceRequestSource(RequestSource[HttpRequest]):
    """Все страницы space через `/rest/api/space/{key}/content`.

    Discovery запросы (для `list_source_ids`) делает через свой httpx.Client'.
    Stream запросов на fetch — генерит HttpRequest'ы, выполнять их будет
    HttpTransport ниже по pipeline'у.
    """

    def __init__(
        self,
        *,
        base_url: str,
        auth: AuthApplier | None,
        space_key: str,
        body_format: str = "export_view",
        timeout_sec: float = 30.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._host = extract_host(base_url)
        self._auth = auth
        self._space_key = space_key
        self._body_format = body_format
        self._timeout = timeout_sec

    def name(self) -> str:
        return f"ConfluenceSpaceRequestSource({self._host}/{self._space_key})"

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
        """Перечисление source_id'ов = viewpage URL'ы (stable canonical)."""
        del ctx
        for page_id in self._iter_page_ids():
            yield viewpage_url(self._base_url, page_id)

    def _iter_page_ids(self) -> Iterable[str]:
        """Discovery: пагинирует /space/{key}/content и возвращает только page-id'ы."""
        path = _LIST_PATH.format(key=self._space_key, limit=_LIST_LIMIT)
        with make_discovery_client(self._base_url, self._auth, self._timeout) as client:
            for raw in iter_paginated(client, path):
                page_id = str(raw.get("id") or "").strip()
                if page_id:
                    yield page_id
