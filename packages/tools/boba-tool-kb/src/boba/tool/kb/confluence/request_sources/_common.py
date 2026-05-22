"""
Общие helpers: URL-builder, httpx-клиент для запросов
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from typing import Any, TypeVar
from urllib.parse import quote, urlparse

import httpx
from pydantic import BaseModel

from boba.indexing import Metadata, SourceId
from boba.tool.kb.confluence.api_models import ConfluencePageItem, ConfluenceSpaceItem
from boba.tool.kb.confluence.config import ConfluenceConnectionConfig
from boba.tool.kb.confluence.connection import ConfluenceConnection
from boba.tool.kb.confluence.keys import ConfluenceKeys
from boba.transport.http import HttpRequest

T = TypeVar("T", bound=BaseModel)

__all__ = [
    "ConfluencePaginator",
    "confluence_discover_pages_by_cql",
    "confluence_discover_space_pages",
    "confluence_discover_spaces",
    "cql_search_path",
    "extract_host",
    "make_page_request",
    "page_fetch_path",
    "space_list_path",
    "space_pages_path",
    "viewpage_url",
]


# --------------------------------------------------------------------------- #
# Pure URL/path builders — все запросы к Confluence REST API строятся здесь.
# Path-функции возвращают relative path для ConfluencePaginator (httpx.Client
# с настроенным base_url). Absolute URL получается через
# `f"{base_url.rstrip('/')}{path}"` или через viewpage_url для source_id.
# --------------------------------------------------------------------------- #

_DEFAULT_PAGE_LIMIT = 50


def viewpage_url(base_url: str, page_id: str) -> str:
    """Stable canonical URL страницы — `{base}/pages/viewpage.action?pageId={id}`.

    Используется как `Request.source_id` (отдельно от URL REST-запроса).
    Не зависит от `body_format` или других expand-параметров; стабилен при
    изменении REST-эндпоинтов; кликабелен в браузере.
    """
    return f"{base_url.rstrip('/')}/pages/viewpage.action?pageId={page_id}"


def page_fetch_path(page_id: str, *, body_format: str) -> str:
    """`/rest/api/content/{id}?expand=…` — выгрузка одной страницы с expand-полями."""
    expand = f"body.{body_format},version,ancestors,space,metadata.labels"
    return f"/rest/api/content/{page_id}?expand={expand}"


def space_list_path(
    space_type: str,
    *,
    expand: str | None = None,
    limit: int = _DEFAULT_PAGE_LIMIT,
) -> str:
    """`/rest/api/space?…` — список space'ов с опциональным фильтром по типу.

    `space_type` ∈ {`global`, `personal`, `any`}: `any` снимает фильтр.
    `expand` — необязательное (например, `description.plain`).
    """
    type_filter = "" if space_type == "any" else f"&type={space_type}"
    expand_q = f"&expand={expand}" if expand else ""
    return f"/rest/api/space?limit={limit}&start=0{type_filter}{expand_q}"


def space_pages_path(
    space_key: str,
    *,
    limit: int = _DEFAULT_PAGE_LIMIT,
) -> str:
    """`/rest/api/space/{key}/content?type=page&…` — все страницы space'а."""
    return f"/rest/api/space/{space_key}/content?type=page&limit={limit}&start=0"


def cql_search_path(
    cql: str,
    *,
    limit: int = _DEFAULT_PAGE_LIMIT,
    expand: str | None = None,
) -> str:
    """`/rest/api/content/search?cql=…&…` — CQL-поиск страниц.

    `cql` URL-encode'ится здесь; не нужно quote'ить заранее.
    `expand` — необязательное (например, `body.view,version,space`).
    """
    expand_q = f"&expand={expand}" if expand else ""
    return f"/rest/api/content/search?cql={quote(cql, safe='')}&limit={limit}{expand_q}"


# --------------------------------------------------------------------------- #
# Helpers / HttpRequest builders.
# --------------------------------------------------------------------------- #


def extract_host(base_url: str) -> str:
    """
    выбирает только netloc из переданного url
    `https://confl.x.com/wiki/` → `confl.x.com`
    """
    netloc = urlparse(base_url).netloc
    return netloc or base_url.split("://", 1)[-1].split("/", 1)[0]


def make_page_request(
    *,
    base_url: str,
    host: str,
    auth: httpx.Auth | None,
    page_id: str,
    body_format: str,
) -> HttpRequest:
    """HttpRequest на выгрузку страницы.

    `url`        — absolute REST endpoint (что Transport исполняет).
    `source_id`  — stable viewpage URL (canonical id документа). Отдельный
                   от REST URL, который меняется с эндпоинтами.
    `metadata`   — page_id и host для дальнейших стадий (Decoder/Reader).
    """
    path = page_fetch_path(page_id, body_format=body_format)
    return HttpRequest(
        url=f"{base_url.rstrip('/')}{path}",
        method="GET",
        auth=auth,
        source_id=SourceId(viewpage_url(base_url, page_id)),
        metadata=(
            Metadata.empty()
            .set(ConfluenceKeys.PAGE_ID, page_id)
            .set(ConfluenceKeys.HOST, host)
        ),
    )


class ConfluencePaginator:
    def __init__(
        self,
        base_url: str,
        auth: httpx.Auth | None,
        timeout_sec: float,
    ):
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            timeout=timeout_sec,
            auth=auth,
        )

    def __call__(self, path: str, item: type[T]) -> Iterator[T]:
        """
        Выполняет запрос и возвращает итератор по результату
        """
        next_path: str | None = path
        while next_path:
            resp = self._client.get(next_path)
            resp.raise_for_status()
            data = resp.json()
            for raw in self._extract_results(data):
                yield item.model_validate(raw)

            next_path = self._next_link(data)

    @staticmethod
    def _extract_results(data: dict[str, Any]) -> list[dict[str, Any]]:
        """top-level 'results' или вложенный 'page.results'."""
        res = data.get("results")
        if isinstance(res, list):
            return res

        res = data.get("page", {}).get("results")
        if isinstance(res, list):
            return res

        return []

    @staticmethod
    def _next_link(data: dict[str, Any]) -> str | None:
        next_path = data.get("_links", {}).get("next")
        if not next_path:
            return None

        return str(next_path)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self._client.close()


def confluence_discover_spaces(
    conn_cfg: ConfluenceConnectionConfig, space_type: str
) -> Iterable[str]:
    """Yield space-keys через `/rest/api/space`."""
    path = space_list_path(space_type)

    with ConfluencePaginator(
        conn_cfg.base_url,
        ConfluenceConnection.make_auth(conn_cfg),
        conn_cfg.timeout_sec,
    ) as x:
        for item in x(path, ConfluenceSpaceItem):
            if item.key:
                yield item.key


def confluence_discover_space_pages(
    conn_cfg: ConfluenceConnectionConfig, space_key: str
) -> Iterable[str]:
    """Yield page-id всех страниц в space через `/rest/api/space/{key}/content`."""
    path = space_pages_path(space_key)

    with ConfluencePaginator(
        conn_cfg.base_url,
        ConfluenceConnection.make_auth(conn_cfg),
        conn_cfg.timeout_sec,
    ) as x:
        for item in x(path, ConfluencePageItem):
            page_id = item.id.strip()
            if page_id:
                yield page_id


def confluence_discover_pages_by_cql(
    conn_cfg: ConfluenceConnectionConfig, cql: str
) -> Iterable[str]:
    """Yield page-id страниц по CQL-запросу через `/rest/api/content/search`."""
    path = cql_search_path(cql)

    with ConfluencePaginator(
        conn_cfg.base_url,
        ConfluenceConnection.make_auth(conn_cfg),
        conn_cfg.timeout_sec,
    ) as x:
        for item in x(path, ConfluencePageItem):
            page_id = item.id.strip()
            if page_id:
                yield page_id
