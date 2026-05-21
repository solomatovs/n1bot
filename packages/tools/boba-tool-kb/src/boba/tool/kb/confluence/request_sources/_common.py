"""Общие helpers: URL-builder, httpx-клиент для discovery-запросов.

Identity (`source_id`) формируется RequestSource'ом — stable viewpage-URL
страницы (отдельный от REST URL запроса). Если когда-то понадобится
cross-transport дедупликация (одна и та же Confluence-страница, полученная
REST'ом и FS-export'ом, получает один id) — это будет отдельный
canonical-resolver слой, не функция RequestSource'а.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from urllib.parse import urlparse

import httpx

from boba.indexing import Metadata, SourceId
from boba.tool.kb.confluence.keys import ConfluenceKeys
from boba.transport.http import HttpRequest

__all__ = [
    "extract_host",
    "iter_paginated",
    "make_discovery_client",
    "make_page_request",
    "viewpage_url",
]

def extract_host(base_url: str) -> str:
    """
    `https://confl.x.com/wiki/` → `confl.x.com` (только netloc)
    """
    netloc = urlparse(base_url).netloc
    return netloc or base_url.split("://", 1)[-1].split("/", 1)[0]


def viewpage_url(base_url: str, page_id: str) -> str:
    """Stable canonical URL страницы — `…/pages/viewpage.action?pageId={id}`.

    Используется как `Request.source_id` (отдельно от URL REST-запроса).
    Не зависит от `body_format` или других expand-параметров; стабилен при
    изменении REST-эндпоинтов; кликабелен в браузере.
    """
    return f"{base_url.rstrip('/')}/pages/viewpage.action?pageId={page_id}"


def make_page_request(
    *,
    base_url: str,
    host: str,
    auth: httpx.Auth | None,
    page_id: str,
    body_format: str,
) -> HttpRequest:
    """HttpRequest на выгрузку страницы.

    `url`        — REST endpoint с expand-полями (что Transport исполняет).
    `source_id`  — stable viewpage URL (canonical id документа). Отдельный
                   от REST URL, который меняется с эндпоинтами.
    `metadata`   — page_id и host для дальнейших стадий (Decoder/Reader).
    """
    expand = f"body.{body_format},version,ancestors,space,metadata.labels"
    rest_url = (
        f"{base_url.rstrip('/')}/rest/api/content/{page_id}"
        f"?expand={expand}"
    )
    return HttpRequest(
        url=rest_url,
        method="GET",
        auth=auth,
        source_id=SourceId(viewpage_url(base_url, page_id)),
        metadata=(
            Metadata.empty()
            .set(ConfluenceKeys.PAGE_ID, page_id)
            .set(ConfluenceKeys.HOST, host)
        ),
    )


def make_discovery_client(
    base_url: str,
    auth: httpx.Auth | None,
    timeout_sec: float,
) -> httpx.Client:
    """httpx.Client для discovery-запросов (пагинация id'ов).

    Это собственный HTTP внутри RequestSource'а — отдельный от Transport.
    Transport занимается выгрузкой content'а; RequestSource — только
    планированием (какие id существуют). Цикла зависимостей нет.
    """
    return httpx.Client(
        base_url=base_url.rstrip("/"),
        timeout=timeout_sec,
        auth=auth,
    )


def iter_paginated(
    client: httpx.Client,
    initial_path: str,
) -> Iterator[dict[str, Any]]:
    """Cursor-based пагинация Confluence REST: `_links.next` ведёт следующие.

    Возвращает items только из текущей страницы — caller извлекает то что
    ему нужно (page_id или весь объект).
    """
    path: str | None = initial_path
    while path:
        resp = client.get(path)
        resp.raise_for_status()
        data = resp.json()
        results = _extract_results(data)
        yield from results
        path = _next_link(data)


def _extract_results(data: dict[str, Any]) -> list[dict[str, Any]]:
    """top-level 'results' или вложенный 'page.results'."""
    if "results" in data:
        return list(data.get("results") or [])
    return list(data.get("page", {}).get("results") or [])


def _next_link(data: dict[str, Any]) -> str | None:
    next_path = data.get("_links", {}).get("next")
    if not next_path:
        return None
    return str(next_path)
