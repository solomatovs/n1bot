"""Tool `confluence_search_cql` + `ConfluenceSearchCqlConfig`: online CQL-search.

Полнотекстовый поиск страниц по реальному Confluence (не по KB). LLM
передаёт строку запроса + опц. `space` ограничение; connection и
лимиты — из TOML-секции `[tool.kb.confluence.search.cql]`.
"""

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
from boba.settings import BobaFlatSettings, BobaSettingsConfigDict
from boba.tool.kb.confluence.connection import ConfluenceConnection
from boba.tool.kb.confluence.keys import ConfluenceKeys
from boba.tool.kb.confluence.request_sources.search import (
    ConfluenceCqlSearchRequestSource,
)
from boba.tool.kb.confluence.search_reader import ConfluenceSearchHitsReader
from boba.tools import FromConfig, tool
from boba.transport.http import HttpKeys

__all__ = ["ConfluenceSearchCqlConfig", "confluence_search_cql"]


_PIPELINE_ID: PipelineId = PipelineId("confluence.search_cql")


class ConfluenceSearchCqlConfig(BobaFlatSettings):
    """Self-contained конфиг tool'а `confluence_search_cql`.

    Config-секция: `[tool.kb.confluence.search.cql]`.
    """

    model_config = BobaSettingsConfigDict(
        case_sensitive=False,
        extra="ignore",
        config_path="tool.kb.confluence.search.cql",
        defaults_from=("confluence",),
    )

    confluence: ConfluenceConnection
    snippet_chars: int = Field(
        default=300,
        ge=1,
        description="Максимальная длина сниппета на каждый hit.",
    )
    max_limit: int = Field(
        default=50,
        ge=1,
        le=200,
        description="Жёсткий потолок параметра `limit` от LLM.",
    )


@tool
def confluence_search_cql(
    cfg: Annotated[ConfluenceSearchCqlConfig, FromConfig()],
    query: Annotated[
        str,
        Field(min_length=1, description="Строка полнотекстового поиска в Confluence."),
    ],
    limit: Annotated[
        int,
        Field(ge=1, description="Максимум hits в ответе."),
    ] = 20,
    space: Annotated[
        str | None,
        Field(description="Ограничение поиска по space."),
    ] = None,
) -> list[dict[str, Any]]:
    """Полнотекстовый поиск страниц Confluence (online CQL).

    Возвращает плоский список hits: `[{page_id, title, space_key, url,
    snippet, last_modified}, ...]`. Совместимо по shape с `kb_search_hybrid`
    и `fts_search` (тоже `list[dict]`). Для последующей работы:
    `confluence_download(page_ids=[...])` (HTML/Markdown → workspace) или
    `confluence_ingest(page_ids=[...])` (страницы → KB-коллекцию для kb_search_*).
    """
    if limit > cfg.max_limit:
        raise RuntimeError(
            f"limit={limit} превышает max_limit={cfg.max_limit}",
        )

    pipeline = RuntimePipeline(
        request_source=ConfluenceCqlSearchRequestSource(
            base_url=cfg.confluence.base_url,
            auth=cfg.confluence.make_auth(),
            cql=_build_cql(query=query, space=space),
            limit=limit,
        ),
        transport=cfg.confluence.make_transport(),
        reader=ConfluenceSearchHitsReader(
            base_url=cfg.confluence.base_url,
            snippet_chars=cfg.snippet_chars,
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
