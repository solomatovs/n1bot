"""Общий хелпер: Page → SourceItem + сборка url'ов и client'а."""

from __future__ import annotations

from urllib.parse import urlparse

from boba.ext.confluence_source.client import ConfluenceClient, Page
from boba.ext.confluence_source.config import (
    ConfluenceCommonConfig,
    ConfluenceCommonSection,
    build_auth,
)
from boba.indexing import IndexerExtensionContext, SourceItem

__all__ = [
    "build_client",
    "load_common",
    "page_source_id",
    "page_to_item",
]

CONTENT_HINT = "confluence_html"


def load_common(ctx: IndexerExtensionContext) -> ConfluenceCommonConfig:
    """Достаёт общую секцию [indexer.sources.confluence] из AppConfig."""
    return ctx.config.section(ConfluenceCommonSection)


def build_client(common: ConfluenceCommonConfig) -> ConfluenceClient:
    """ConfluenceCommonConfig → готовый ConfluenceClient."""
    if not common.base_url:
        msg = (
            "base_url пустой. Задай его через env: "
            "BOBA_INDEXER__SOURCES__CONFLUENCE__BASE_URL=..."
        )
        raise ValueError(msg)
    return ConfluenceClient(
        base_url=common.base_url,
        auth=build_auth(common),
        body_format=common.body_format,
        timeout_sec=common.timeout_sec,
    )


def page_source_id(base_url: str, page_id: str) -> str:
    """Каноничный source_id: confluence://{host}/page/{page_id}."""
    host = urlparse(base_url).netloc or base_url
    return f"confluence://{host}/page/{page_id}"


def _page_view_url(base_url: str, page_id: str) -> str:
    return f"{base_url.rstrip('/')}/pages/viewpage.action?pageId={page_id}"


def page_to_item(base_url: str, page: Page) -> SourceItem:
    """Page (REST DTO) → SourceItem для индексирования."""
    metadata = {
        "title": page.title,
        "space_key": page.space_key,
        "page_id": page.page_id,
        "version": str(page.version),
        "last_modified": page.last_modified,
        "ancestors_path": page.ancestors_path,
        "source_url": _page_view_url(base_url, page.page_id),
    }
    return SourceItem(
        source_id=page_source_id(base_url, page.page_id),
        content_hint=CONTENT_HINT,
        payload=page.body_html.encode("utf-8"),
        metadata=metadata,
        content_hash=str(page.version),
    )
