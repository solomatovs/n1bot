"""Tool `confluence_search_cql` + `ConfluenceSearchCqlConfig`: online CQL-search.

Полнотекстовый поиск страниц по реальному Confluence (не по KB). LLM
передаёт строку запроса + список `spaces` для ограничения; connection и
лимиты — из TOML-секции `[tool.kb.confluence.search.cql]`.
"""

from __future__ import annotations

from typing import Annotated, Any, ClassVar

import httpx
from pydantic import Field

from boba.indexing import (
    PipelineContext,
    PipelineId,
    ReaderKeys,
    RuntimePipeline,
    Section,
)
from boba.settings import BobaFlatSettings, BobaSettingsConfigDict, LLMStringList
from boba.tool.kb.confluence.connection import ConfluenceConnection
from boba.tool.kb.confluence.models import ConfluenceKeys
from boba.tool.kb.confluence.reading import ConfluenceSearchHitsReader
from boba.tool.kb.confluence.request_sources import ConfluenceCqlSearchRequestSource
from boba.tools import FromConfig, tool
from boba.transport.http import HttpKeys

__all__ = ["ConfluenceSearchCqlConfig", "confluence_search_cql"]


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


class CqlSearch:
    """Сборка CQL и распаковка search-`Section` в плоский hit-dict."""

    PIPELINE_ID: ClassVar[PipelineId] = PipelineId("confluence.search_cql")

    @staticmethod
    def hit(section: Section[str]) -> dict[str, str]:
        m = section.metadata
        return {
            "page_id": m.get(ConfluenceKeys.PAGE_ID) or "",
            "title": m.get(ReaderKeys.PAGE_TITLE) or "",
            "space_key": m.get(ConfluenceKeys.SPACE_KEY) or "",
            "url": str(section.source_id),
            "snippet": section.content,
            "last_modified": m.get(HttpKeys.LAST_MODIFIED) or "",
        }

    @staticmethod
    def cql_literal(value: str) -> str:
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'

    @staticmethod
    def build_cql(query: str, spaces: list[str] | None) -> str:
        text_block = f"text ~ {CqlSearch.cql_literal(query)}"
        if not spaces:
            return text_block
        if len(spaces) == 1:
            space_block = f"space = {CqlSearch.cql_literal(spaces[0])}"
        else:
            joined = ", ".join(CqlSearch.cql_literal(s) for s in spaces)
            space_block = f"space in ({joined})"
        return f"({text_block}) and ({space_block})"


@tool
def confluence_search_cql(
    cfg: Annotated[ConfluenceSearchCqlConfig, FromConfig()],
    query: Annotated[
        str,
        Field(min_length=1, description="Строка полнотекстового поиска в Confluence."),
    ],
    spaces: Annotated[
        LLMStringList | None,
        Field(
            description=(
                "Ограничение поиска по space-ключам Confluence. "
                "Не передавай (или `null`) — поиск по всем space'ам."
            ),
        ),
    ] = None,
    limit: Annotated[
        int,
        Field(ge=1, description="Максимум hits в ответе."),
    ] = 20,
) -> list[dict[str, Any]]:
    """Полнотекстовый поиск страниц Confluence (online CQL).

    Возвращает плоский список hits: `[{page_id, title, space_key, url,
    snippet, last_modified}, ...]`
    """
    if limit > cfg.max_limit:
        raise RuntimeError(
            f"limit={limit} превышает max_limit={cfg.max_limit}",
        )

    pipeline = RuntimePipeline(
        request_source=ConfluenceCqlSearchRequestSource(
            base_url=cfg.confluence.base_url,
            auth=cfg.confluence.make_auth(),
            cql=CqlSearch.build_cql(query=query, spaces=spaces),
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
            pipeline.stream(PipelineContext(pipeline_id=CqlSearch.PIPELINE_ID)),
        )
    except httpx.HTTPError as e:
        raise RuntimeError(
            f"Confluence search failed: {type(e).__name__}: {e}",
        ) from e

    return [CqlSearch.hit(s) for s in sections]
