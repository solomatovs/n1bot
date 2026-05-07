"""ConfluenceSearchPipeline: REST /content/search → ConfluenceSearchHit'ы.

Принимает уже готовый `httpx.Client` (с base_url+auth+timeout), не управляет
его lifecycle'ом — caller отвечает за `with`-блок. Это симметрично
PrintPipeline (Store передаётся снаружи).

Все ошибки исполнения оборачиваются в `ConfluenceSearchPipelineError`
(или его подклассы) — caller'ы (tool/CLI) ловят один корневой класс.
"""

from __future__ import annotations

from typing import Any

import httpx

from boba.ext.confluence_tools._search_pipeline.errors import (
    ConfluenceSearchHttpError,
    ConfluenceSearchResponseError,
)
from boba.ext.confluence_tools._search_pipeline.models import ConfluenceSearchHit
from boba.ext.confluence_tools._search_pipeline.stats import ConfluenceSearchStats
from boba.ext.confluence_tools.parse import parse_html, plain_text

__all__ = ["ConfluenceSearchPipeline"]


class ConfluenceSearchPipeline:
    """Pipeline-оркестратор: CQL → list[ConfluenceSearchHit]."""

    _SEARCH_PATH = "/rest/api/content/search"
    _DEFAULT_EXPAND = "body.view,version,space"

    def __init__(
        self,
        *,
        client: httpx.Client,
        base_url: str,
        cql: str,
        limit: int = 10,
        snippet_chars: int = 300,
    ) -> None:
        self._client = client
        self._base_url = base_url.rstrip("/")
        self._cql = cql
        self._limit = limit
        self._snippet_chars = snippet_chars

    def name(self) -> str:
        return f"ConfluenceSearchPipeline(cql={self._cql!r}, limit={self._limit})"

    def reset(self) -> None:
        pass

    def run(self) -> ConfluenceSearchStats:
        params = {
            "cql": self._cql,
            "limit": self._limit,
            "expand": self._DEFAULT_EXPAND,
        }

        try:
            resp = self._client.get(self._SEARCH_PATH, params=params)
            resp.raise_for_status()
        except httpx.HTTPError as e:
            raise ConfluenceSearchHttpError(
                f"Confluence REST {type(e).__name__}: {e}",
            ) from e

        try:
            data = resp.json()
            results = data["results"]
            hits = [self._make_hit(r) for r in results]
        except (ValueError, KeyError, TypeError) as e:
            raise ConfluenceSearchResponseError(
                f"unexpected /content/search response: {type(e).__name__}: {e}",
            ) from e

        return ConfluenceSearchStats(cql=self._cql, hits=hits)

    def _make_hit(self, r: dict[str, Any]) -> ConfluenceSearchHit:
        page_id = r["id"]
        return ConfluenceSearchHit(
            page_id=page_id,
            title=r["title"],
            space_key=r["space"]["key"],
            url=self._viewpage_url(page_id),
            excerpt=self._make_excerpt(r["body"]["view"]["value"]),
            last_modified=r["version"]["when"],
        )

    def _viewpage_url(self, page_id: str) -> str:
        return f"{self._base_url}/pages/viewpage.action?pageId={page_id}"

    def _make_excerpt(self, html: str) -> str:
        if not html:
            return ""

        soup = parse_html(html)

        text = plain_text(soup).strip()
        if len(text) <= self._snippet_chars:
            return text

        return text[: self._snippet_chars - 1].rstrip() + "…"
