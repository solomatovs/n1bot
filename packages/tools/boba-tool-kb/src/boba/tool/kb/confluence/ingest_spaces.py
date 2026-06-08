"""Tool confluence_ingest_spaces: индексация всех страниц перечисленных spaces.

Discovery через /rest/api/space/{key}/content. Общий конфиг/pipeline —
ingest_base.py (секция [tool.kb.confluence.ingest]).
"""

from __future__ import annotations

from typing import Annotated

from pydantic import Field

from boba.settings import LLMStringList
from boba.tool.kb.confluence.connection import ConfluenceConnection
from boba.tool.kb.confluence.ingest_base import ConfluenceIngest, ConfluenceIngestConfig
from boba.tool.kb.confluence.request_sources import ConfluenceMultiSpaceRequestSource
from boba.tools import FromConfig, tool
from boba.tools.domain import TableResult

__all__ = ["confluence_ingest_spaces"]


@tool
def confluence_ingest_spaces(
    cfg: Annotated[ConfluenceIngestConfig, FromConfig()],
    space_keys: Annotated[
        LLMStringList,
        Field(
            min_length=1,
            description=(
                "Список space-ключей Confluence для индексации. "
                'Передавай JSON-массив строк: `["DOCS", "ENG"]`. Discovery '
                "через `/rest/api/space/{key}/content` — индексируются все "
                "страницы каждого space. Используй, когда нужен полный "
                "охват space'а."
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
                "Если true, переиндексировать страницы space'ов целиком: "
                "переэмбеддить все чанки (минуя skip-by-hash) и удалить "
                "устаревшие чанки этих страниц, включая снятые с них вложения."
            ),
        ),
    ] = False,
) -> TableResult:
    """Индексирует ВСЕ страницы перечисленных Confluence-spaces в KB.

    Возвращает TableResult с одной строкой-stats: колонки space_keys/
    collection/indexed/skipped_unchanged/pruned/failed.
    """
    conn = ConfluenceConnection(profile=cfg.confluence, body_format=cfg.body_format)
    request_source = ConfluenceMultiSpaceRequestSource(
        conn=conn,
        space_keys=space_keys,
        body_format=conn.body_format,
    )
    result = ConfluenceIngest.ingest(cfg, request_source, prune_missing, force_update)
    return TableResult(rows=[{"space_keys": space_keys, **result}])
