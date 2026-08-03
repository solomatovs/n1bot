"""Tool confluence_ingest_spaces: индексация всех страниц перечисленных spaces."""

from __future__ import annotations

from typing import Annotated

from pydantic import Field

from boba.settings import LLMStringList
from boba.tool.kb.confluence.ingest_base import (
    ConfluenceIngestConfig,
)
from boba.tool.kb.confluence.ingest_caller import (
    ConfluenceIngestCaller,
)
from boba.toolkit.result import TableResult

__all__ = ["confluence_ingest_spaces"]


def confluence_ingest_spaces(
    cfg: ConfluenceIngestConfig,
    caller: ConfluenceIngestCaller,
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
    """Индексирует ВСЕ страницы перечисленных Confluence-spaces в KB."""
    result = caller.ingest(
        cfg=cfg,
        mode="spaces",
        space_keys=space_keys,
        prune_missing=prune_missing,
        force_update=force_update,
    )
    return TableResult(rows=[{"space_keys": space_keys, **result}])
