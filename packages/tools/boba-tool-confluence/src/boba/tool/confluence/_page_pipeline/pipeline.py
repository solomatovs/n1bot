"""ConfluencePagePipeline: REST /content/{id} → ConfluencePageContent.

Принимает готовый `httpx.Client` (caller отвечает за `with`-блок) — симметрично
ConfluenceSearchPipeline. Делает один GET-запрос с expand'ом body+version+space,
парсит JSON, прогоняет body-HTML через core.parse_html для извлечения
структуры заголовков. body_format='view' даёт clean HTML без `ac:*`-макросов
(в отличие от 'export_view', который используется индексатором).
"""

from __future__ import annotations

from typing import Any, ClassVar

import httpx

from boba.tool.confluence._page_pipeline.errors import (
    ConfluencePageHttpError,
    ConfluencePageNotFoundError,
    ConfluencePageResponseError,
)
from boba.tool.confluence._page_pipeline.models import (
    ConfluencePageContent,
    ConfluencePageHeading,
)
from boba.tool.confluence.parse import (
    anchor_for,
    collect_headings,
    parse_html,
)

__all__ = ["ConfluencePagePipeline"]


class ConfluencePagePipeline:
    """Pipeline-оркестратор: page_id → ConfluencePageContent."""

    _CONTENT_PATH: ClassVar[str] = "/rest/api/content"
    _DEFAULT_EXPAND_TPL: ClassVar[str] = "body.{fmt},version,space"

    def __init__(
        self,
        client: httpx.Client,
        base_url: str,
        body_format: str,
    ) -> None:
        self._client = client
        self._base_url = base_url.rstrip("/")
        self._body_format = body_format

    def name(self) -> str:
        return f"ConfluencePagePipeline(format={self._body_format!r})"

    def reset(self) -> None:
        pass

    def fetch(self, page_id: str) -> ConfluencePageContent:
        params = {
            "expand": self._DEFAULT_EXPAND_TPL.format(fmt=self._body_format),
        }
        try:
            resp = self._client.get(
                f"{self._CONTENT_PATH}/{page_id}",
                params=params,
            )
        except httpx.HTTPError as e:
            raise ConfluencePageHttpError(
                f"Confluence REST {type(e).__name__}: {e}",
            ) from e
        if resp.status_code == self._NOT_FOUND:
            raise ConfluencePageNotFoundError(
                f"page_id={page_id!r} not found in Confluence",
            )
        try:
            resp.raise_for_status()
        except httpx.HTTPError as e:
            raise ConfluencePageHttpError(
                f"Confluence REST {type(e).__name__}: {e}",
            ) from e
        try:
            data = resp.json()
            return self._build_content(data)
        except (ValueError, KeyError, TypeError) as e:
            raise ConfluencePageResponseError(
                f"unexpected /content/{{id}} response: {type(e).__name__}: {e}",
            ) from e

    _NOT_FOUND: ClassVar[int] = 404

    def _build_content(self, data: dict[str, Any]) -> ConfluencePageContent:
        body_html = data["body"][self._body_format]["value"]
        page_id = data["id"]
        return ConfluencePageContent(
            page_id=page_id,
            title=data["title"],
            space_key=data["space"]["key"],
            url=self._viewpage_url(page_id),
            version=str(data["version"]["number"]),
            last_modified=data["version"]["when"],
            body_html=body_html,
            headings=self._extract_headings(body_html),
        )

    def _viewpage_url(self, page_id: str) -> str:
        return f"{self._base_url}/pages/viewpage.action?pageId={page_id}"

    @staticmethod
    def _extract_headings(html: str) -> list[ConfluencePageHeading]:
        if not html:
            return []
        soup = parse_html(html)
        return [
            ConfluencePageHeading(
                level=h.level,
                text=h.text,
                anchor=anchor_for(h),
            )
            for h in collect_headings(soup)
            if h.text.strip()
        ]
