"""Тонкий httpx-обёртка над Confluence Server/DC REST.

API:
- pages_in_space(space_key) → Iterator[Page]
- page_by_id(page_id) → Page
- page_ids_in_space(space_key) → Iterator[str]
- pages_by_cql(cql) → Iterator[Page]

Каждый итератор лениво идёт по pagination ('_links.next').
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Any

import httpx

from boba.ext.confluence_source.auth import ConfluenceAuth

__all__ = ["ConfluenceClient", "Page"]


@dataclass(frozen=True)
class Page:
    """Минимальное представление одной Confluence-страницы."""

    page_id: str
    title: str
    space_key: str
    version: int
    body_html: str
    last_modified: str
    ancestors_path: str  # "/Root/Parent/..." titles joined with /


_DEFAULT_LIMIT = 50


def _extract_results(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Достать results-массив: top-level 'results' или вложенный 'page.results'."""
    if "results" in data:
        return list(data.get("results") or [])
    return list(data.get("page", {}).get("results") or [])


class ConfluenceClient:
    """Sync httpx client поверх Confluence Server/DC REST."""

    def __init__(
        self,
        base_url: str,
        auth: ConfluenceAuth,
        body_format: str,
        timeout_sec: float,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._body_format = body_format
        kwargs: dict[str, Any] = {
            "base_url": self._base_url,
            "timeout": timeout_sec,
        }
        auth.apply(kwargs)
        if transport is not None:
            kwargs["transport"] = transport
        self._client = httpx.Client(**kwargs)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> ConfluenceClient:
        return self

    def __exit__(self, *exc: object) -> None:
        del exc
        self.close()

    def page_by_id(self, page_id: str) -> Page:
        """Один документ по id."""
        params = {"expand": self._expand()}
        resp = self._client.get(f"/rest/api/content/{page_id}", params=params)
        resp.raise_for_status()
        return self._page_from_json(resp.json())

    def pages_in_space(self, space_key: str) -> Iterator[Page]:
        """Все страницы (type=page) в space — пагинация по '_links.next'."""
        params: dict[str, Any] = {
            "type": "page",
            "expand": self._expand(),
            "limit": _DEFAULT_LIMIT,
        }
        url = f"/rest/api/space/{space_key}/content"
        yield from self._paginate(url, params)

    def page_ids_in_space(self, space_key: str) -> Iterator[str]:
        """Только id'ы страниц space — для sync без выкачивания тел."""
        params: dict[str, Any] = {
            "type": "page",
            "limit": _DEFAULT_LIMIT,
        }
        url = f"/rest/api/space/{space_key}/content"
        for raw in self._paginate_raw(url, params):
            page_id = raw.get("id")
            if isinstance(page_id, str):
                yield page_id

    def pages_by_cql(self, cql: str) -> Iterator[Page]:
        """Страницы по CQL-запросу."""
        params: dict[str, Any] = {
            "cql": cql,
            "expand": self._expand(),
            "limit": _DEFAULT_LIMIT,
        }
        url = "/rest/api/content/search"
        yield from self._paginate(url, params)

    def _expand(self) -> str:
        return f"body.{self._body_format},version,ancestors,space"

    def _paginate(
        self, url: str, params: Mapping[str, Any]
    ) -> Iterator[Page]:
        for raw in self._paginate_raw(url, params):
            yield self._page_from_json(raw)

    def _paginate_raw(
        self, url: str, params: Mapping[str, Any]
    ) -> Iterator[dict[str, Any]]:
        next_url: str | None = url
        next_params: Mapping[str, Any] | None = params
        while next_url is not None:
            resp = self._client.get(next_url, params=next_params)
            resp.raise_for_status()
            data = resp.json()
            container = _extract_results(data)
            yield from container
            link = data.get("_links", {}).get("next")
            if not link:
                next_url = None
                next_params = None
                continue
            # _links.next бывает относительный — клиент сам резолвит относит. base_url
            next_url = link
            next_params = None  # next содержит query уже

    def _page_from_json(self, raw: dict[str, Any]) -> Page:
        body = raw.get("body", {}).get(self._body_format, {})
        body_html = body.get("value", "") if isinstance(body, dict) else ""
        version = int(raw.get("version", {}).get("number", 0) or 0)
        last_mod = str(raw.get("version", {}).get("when", "") or "")
        space = raw.get("space", {}) or {}
        space_key = str(space.get("key", "") or "")
        ancestors = raw.get("ancestors", []) or []
        ancestors_path = "/".join(
            str(a.get("title", "")) for a in ancestors if a.get("title")
        )
        return Page(
            page_id=str(raw.get("id", "")),
            title=str(raw.get("title", "") or ""),
            space_key=space_key,
            version=version,
            body_html=body_html,
            last_modified=last_mod,
            ancestors_path=ancestors_path,
        )
