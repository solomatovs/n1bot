"""Tool confluence_ingest_pages: индексация явного списка страниц по page_id.

Общий конфиг/pipeline — ingest_base.py (секция [tool.kb.confluence.ingest]).
"""

from __future__ import annotations

from typing import Annotated

from pydantic import Field

from boba.settings import LLMStringList
from boba.tool.kb.confluence.connection import ConfluenceConnection
from boba.tool.kb.confluence.ingest_base import ConfluenceIngest, ConfluenceIngestConfig
from boba.tool.kb.confluence.request_sources import ConfluencePagesRequestSource
from boba.tools import FromConfig, tool
from boba.tools.domain import TableResult

__all__ = ["confluence_ingest_pages"]


@tool
def confluence_ingest_pages(
    cfg: Annotated[ConfluenceIngestConfig, FromConfig()],
    page_ids: Annotated[
        LLMStringList,
        Field(
            min_length=1,
            description=(
                "Список page_id страниц Confluence для индексации. "
                'Передавай JSON-массив строк: `["950276", "950278"]`. '
                "Каждый id — строка из URL `viewpage.action?pageId=<id>`. "
                "Используй, когда уже знаешь конкретные страницы (например, "
                "из результатов `confluence_search_cql`)."
            ),
        ),
    ],
    prune_missing: Annotated[
        bool,
        Field(
            description=(
                "Если true, удалить из коллекции чанки, чьих source_id нет "
                "среди страниц, попавших в discovery текущего run'а."
            ),
        ),
    ] = False,
) -> TableResult:
    """Индексирует явный список страниц Confluence по page_id в KB.

    Возвращает TableResult — одну строку-summary с колонками collection/
    indexed/skipped_unchanged/pruned/failed; список page_id — в note.
    """
    conn = ConfluenceConnection(profile=cfg.confluence, body_format=cfg.body_format)
    request_source = ConfluencePagesRequestSource(
        base_url=conn.base_url,
        page_ids=page_ids,
        body_format=conn.body_format,
    )
    result = ConfluenceIngest.ingest(cfg, request_source, prune_missing)
    return TableResult(
        rows=[result],
        note=f"page_ids ({len(page_ids)}): {', '.join(page_ids)}",
    )
