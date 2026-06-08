"""Tool confluence_search_cql + ConfluenceSearchCqlConfig: online CQL-search.

Полнотекстовый поиск страниц по реальному Confluence (не по KB). LLM
передаёт строку запроса + список spaces + limit/snippet_chars;
connection (confluence) — из секции [tool.kb].
"""

from __future__ import annotations

from typing import Annotated, ClassVar

import httpx
from pydantic import BaseModel, ConfigDict, Field

from boba.indexing import (
    Pipeline,
    ReaderKeys,
    Section,
)
from boba.settings import LLMStringList
from boba.tool.kb.confluence.connection import ConfluenceConnection
from boba.tool.kb.confluence.models import ConfluenceKeys, HttpKeys
from boba.tool.kb.confluence.pipeline import ConfluenceHttpTransport
from boba.tool.kb.confluence.reading import ConfluenceSearchHitsReader
from boba.tool.kb.confluence.request_sources import ConfluenceCqlSearchRequestSource
from boba.tools import FromConfig, tool
from boba.tools.domain import TableResult
from boba.transport.http import HttpProfile, HttpTransport

__all__ = ["ConfluenceSearchCqlConfig", "confluence_search_cql"]


class ConfluenceSearchCqlConfig(BaseModel):
    """Self-contained конфиг tool'а confluence_search_cql.

    Config-секция: [tool.kb] (читает только confluence). limit/
    snippet_chars — tool-аргументы LLM.
    """

    model_config = ConfigDict(extra="ignore")

    confluence: HttpProfile


class CqlSearch:
    """Сборка CQL и распаковка search-Section в плоский hit-dict."""

    SNIPPET_DEFAULT: ClassVar[int] = 300
    SNIPPET_DESC: ClassVar[str] = (
        "Максимальная длина сниппета на каждый hit (символов). По умолчанию 300."
    )

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
    snippet_chars: Annotated[
        int,
        Field(ge=1, description=CqlSearch.SNIPPET_DESC),
    ] = CqlSearch.SNIPPET_DEFAULT,
) -> TableResult:
    """Полнотекстовый поиск страниц Confluence (online CQL).

    Возвращает TableResult — таблицу hits с колонками page_id/title/
    space_key/url/snippet/last_modified.
    """
    conn = ConfluenceConnection(profile=cfg.confluence)
    pipeline = Pipeline(
        source=ConfluenceCqlSearchRequestSource(
            base_url=conn.base_url,
            cql=CqlSearch.build_cql(query=query, spaces=spaces),
            limit=limit,
        ),
        transport=ConfluenceHttpTransport(HttpTransport(conn.profile)),
        reader=ConfluenceSearchHitsReader(
            base_url=conn.base_url,
            snippet_chars=snippet_chars,
        ),
    )

    try:
        sections = list(pipeline.sections())
    except httpx.HTTPError as e:
        raise RuntimeError(
            f"Confluence search failed: {type(e).__name__}: {e}",
        ) from e

    rows = [CqlSearch.hit(s) for s in sections]
    return TableResult(rows=rows, note=None if rows else "ничего не найдено")
