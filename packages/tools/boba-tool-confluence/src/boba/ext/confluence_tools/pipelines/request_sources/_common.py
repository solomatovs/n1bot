"""Общие helpers: URL-builder, httpx-клиент для discovery-запросов.

Identity (source_id) НЕ формируется RequestSource'ом — это URL транспорта.
HttpTransport подставит `RawDocument.source_id = HttpRequest.url` при пустом
поле. Если когда-то понадобится cross-transport дедупликация (одна и та же
Confluence-страница, полученная REST'ом и FS-export'ом, получает один id) —
это будет отдельный canonical-resolver слой, не функция RequestSource'а.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from urllib.parse import urlparse

import httpx

from boba.http_transport import HttpRequest
from boba.processing import AuthApplier

__all__ = [
    "extract_host",
    "iter_paginated",
    "make_discovery_client",
    "make_page_request",
    "viewpage_url",
]

_DEFAULT_EXPAND = "body.{body_format},version,ancestors,space,metadata.labels"


def extract_host(base_url: str) -> str:
    """`https://confl.x.com/wiki/` → `confl.x.com` (только netloc)."""
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
    auth: AuthApplier | None,
    page_id: str,
    body_format: str,
) -> HttpRequest:
    """HttpRequest на выгрузку страницы.

    `url` — REST endpoint с expand-полями (что Transport исполняет).
    `source_id` — stable viewpage URL (canonical id документа).
    Эти поля разные: REST URL может меняться, viewpage URL — нет.
    """
    expand = _DEFAULT_EXPAND.format(body_format=body_format)
    rest_url = (
        f"{base_url.rstrip('/')}/rest/api/content/{page_id}"
        f"?expand={expand}"
    )
    return HttpRequest(
        url=rest_url,
        method="GET",
        auth=auth,
        source_id=viewpage_url(base_url, page_id),
        metadata={
            "confluence_page_id": page_id,
            "confluence_host": host,
        },
    )


def make_discovery_client(
    base_url: str,
    auth: AuthApplier | None,
    timeout_sec: float,
) -> httpx.Client:
    """httpx.Client для discovery-запросов (пагинация id'ов).

    Это собственный HTTP внутри RequestSource'а — отдельный от Transport.
    Transport занимается выгрузкой content'а; RequestSource — только
    планированием (какие id существуют). Цикла зависимостей нет.
    """
    kwargs: dict[str, Any] = {
        "base_url": base_url.rstrip("/"),
        "timeout": timeout_sec,
    }
    if auth is not None:
        auth(kwargs)
    return httpx.Client(**kwargs)


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
