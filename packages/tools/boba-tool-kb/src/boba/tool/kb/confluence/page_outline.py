"""Tool: онлайн-outline страницы Confluence по page_id."""

from __future__ import annotations

from typing import Annotated, Any

import httpx
from pydantic import Field

from boba.html import HtmlKeys
from boba.indexing import (
    PipelineContext,
    PipelineId,
    ReaderKeys,
    RuntimePipeline,
    Section,
    SectionKeys,
)
from boba.tool.kb.confluence.config import ConfluencePluginConfig
from boba.tool.kb.confluence.connection import ConfluenceConnection
from boba.tool.kb.confluence.decoder import ConfluenceJsonDecoder
from boba.tool.kb.confluence.keys import ConfluenceKeys
from boba.tool.kb.confluence.reader import ConfluenceReader
from boba.tool.kb.confluence.request_sources.pages import (
    ConfluencePagesRequestSource,
)
from boba.tools import FromConfig, tool
from boba.transport.http import HttpKeys

__all__ = ["confluence_page_outline"]


_PIPELINE_ID: PipelineId = PipelineId("confluence.page_outline")


@tool
def confluence_page_outline(
    page_id: Annotated[
        str,
        Field(
            min_length=1,
            description=(
                "ID страницы Confluence (число; виден в URL "
                "viewpage.action?pageId=...)."
            ),
        ),
    ],
    max_headings: Annotated[
        int,
        Field(
            ge=1, le=500,
            description="Максимум заголовков в ответе (защита от длинных страниц).",
        ),
    ],
    cfg: Annotated[ConfluencePluginConfig, FromConfig()],
) -> dict[str, Any]:
    """Online-outline страницы Confluence: page_id → структура заголовков h1..h6.

    Возвращает title + метаданные + список секций с anchor'ами; anchor
    нужен для `confluence_page_section`.
    """
    pipeline = RuntimePipeline(
        request_source=ConfluencePagesRequestSource(
            base_url=cfg.base_url,
            auth=ConfluenceConnection.make_auth(cfg),
            page_ids=[page_id],
            body_format=cfg.body_format,
        ),
        transport=ConfluenceConnection.make_transport(cfg),
        decoder=ConfluenceJsonDecoder(body_format=cfg.body_format),
        reader=ConfluenceReader(),
    )

    try:
        sections = list(
            pipeline.stream(PipelineContext(pipeline_id=_PIPELINE_ID)),
        )
    except httpx.HTTPError as e:
        raise RuntimeError(
            f"Confluence page outline failed: {type(e).__name__}: {e}",
        ) from e

    headings = [s for s in sections if s.metadata.has(HtmlKeys.HEADING_LEVEL)]
    total = len(headings)
    truncated = total > max_headings
    meta = _page_meta(sections)

    return {
        "page_id": page_id,
        "title": meta["title"],
        "space_key": meta["space_key"],
        "url": meta["url"],
        "version": meta["version"],
        "last_modified": meta["last_modified"],
        "sections": [
            {
                "level": h.metadata.get(HtmlKeys.HEADING_LEVEL) or 0,
                "text": h.metadata.get(HtmlKeys.HEADING_TEXT) or "",
                "anchor": h.metadata.get(SectionKeys.ANCHOR) or "",
            }
            for h in headings[:max_headings]
        ],
        "truncated": truncated,
        "total_headings": total,
    }


def _page_meta(sections: list[Section[str]]) -> dict[str, Any]:
    """Page-level метаданные. Все секции одной страницы делят metadata."""
    if not sections:
        return {
            "title": "",
            "space_key": "",
            "url": "",
            "version": "",
            "last_modified": "",
        }
    first = sections[0]
    m = first.metadata
    version = m.get(ConfluenceKeys.VERSION)
    return {
        "title": m.get(ReaderKeys.PAGE_TITLE) or "",
        "space_key": m.get(ConfluenceKeys.SPACE_KEY) or "",
        "url": str(first.source_id),
        "version": str(version) if version is not None else "",
        "last_modified": m.get(HttpKeys.LAST_MODIFIED) or "",
    }
