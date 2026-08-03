"""Tool confluence_ingest_pages: индексация явного списка страниц по page_id.

Общий конфиг/pipeline — ingest_base.py (секция [tool.kb.confluence.ingest]).
"""

from __future__ import annotations

from typing import Annotated

from pydantic import Field

from boba.chainlit2.agent.tools.confluence.ingest_base import (
    ConfluenceIngestConfig,
)
from boba.chainlit2.agent.tools.confluence.ingest_caller import (
    ConfluenceIngestCaller,
)
from boba.chainlit2.rendering.tool_result import TableResult
from boba.settings import LLMStringList

__all__ = ["confluence_ingest_pages"]


def confluence_ingest_pages(
    cfg: ConfluenceIngestConfig,
    caller: ConfluenceIngestCaller,
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
    force_update: Annotated[
        bool,
        Field(
            description=(
                "Если true, переиндексировать указанные страницы целиком: "
                "переэмбеддить все чанки (минуя skip-by-hash) и удалить "
                "устаревшие чанки этих страниц, включая снятые с них вложения."
            ),
        ),
    ] = False,
) -> TableResult:
    """Индексирует явный список страниц Confluence по page_id в KB.

    Возвращает TableResult — одну строку-summary с колонками collection/
    indexed/skipped_unchanged/pruned/failed; список page_id — в note.
    """
    result = caller.ingest(
        cfg=cfg,
        mode="pages",
        page_ids=page_ids,
        prune_missing=prune_missing,
        force_update=force_update,
    )
    return TableResult(
        rows=[result],
        note=f"page_ids ({len(page_ids)}): {', '.join(page_ids)}",
    )
