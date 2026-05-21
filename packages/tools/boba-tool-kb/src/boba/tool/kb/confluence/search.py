"""Tool: поиск страниц Confluence через REST CQL-search."""

from __future__ import annotations

from typing import Annotated, Any
from urllib.parse import quote

import httpx
from pydantic import Field

from boba.indexing import (
    PipelineContext,
    PipelineId,
    ReaderKeys,
    RuntimePipeline,
    Section,
)
from boba.tool.kb.confluence.config import ConfluenceConnectionConfig
from boba.tool.kb.confluence.connection import ConfluenceConnection
from boba.tool.kb.confluence.keys import ConfluenceKeys
from boba.tool.kb.confluence.request_sources.search import (
    ConfluenceCqlSearchRequestSource,
)
from boba.tool.kb.confluence.search_reader import ConfluenceSearchHitsReader
from boba.tools import FromConfig, tool
from boba.transport.http import HttpKeys

__all__ = ["confluence_search"]


_PIPELINE_ID: PipelineId = PipelineId("confluence.search")
_SNIPPET_CHARS: int = 300


@tool
def confluence_search(
    query: Annotated[
        str,
        Field(min_length=1, description="Строка полнотекстового поиска в Confluence."),
    ],
    limit: Annotated[
        int, Field(ge=1, le=50, description="Максимум hits в ответе."),
    ],
    cfg: Annotated[ConfluenceConnectionConfig, FromConfig()],
    space: Annotated[
        str | None,
        Field(description="Ограничение поиска по space."),
    ] = None,
) -> list[dict[str, Any]]:
    """Полнотекстовый поиск страниц Confluence (CQL).

    Возвращает плоский список hits: `[{page_id, title, space_key, url,
    snippet, last_modified}, ...]`. Совместимо по shape с `kb_search` и
    `fts_search` (тоже `list[dict]`). Для последующей работы:
    `confluence_page_download` (HTML/Markdown → workspace) или
    `confluence_page_ingest` (страницы → KB-коллекцию для kb_search).
    """
    pipeline = RuntimePipeline(
        request_source=ConfluenceCqlSearchRequestSource(
            base_url=cfg.base_url,
            auth=ConfluenceConnection.make_auth(cfg),
            cql=_build_cql(query=query, space=space),
            limit=limit,
        ),
        transport=ConfluenceConnection.make_transport(cfg),
        reader=ConfluenceSearchHitsReader(
            base_url=cfg.base_url,
            snippet_chars=_SNIPPET_CHARS,
        ),
    )

    try:
        sections = list(
            pipeline.stream(PipelineContext(pipeline_id=_PIPELINE_ID)),
        )
    except httpx.HTTPError as e:
        raise RuntimeError(
            f"Confluence search failed: {type(e).__name__}: {e}",
        ) from e

    return [_hit(s) for s in sections]


def _hit(section: Section[str]) -> dict[str, str]:
    m = section.metadata
    return {
        "page_id": m.get(ConfluenceKeys.PAGE_ID) or "",
        "title": m.get(ReaderKeys.PAGE_TITLE) or "",
        "space_key": m.get(ConfluenceKeys.SPACE_KEY) or "",
        "url": str(section.source_id),
        "snippet": section.content,
        "last_modified": m.get(HttpKeys.LAST_MODIFIED) or "",
    }


def _build_cql(query: str, space: str | None) -> str:
    text_search_block = f'text ~ "{quote(query, safe="")}"'
    if space:
        return f"({text_search_block}) and (space = {space})"
    return text_search_block
