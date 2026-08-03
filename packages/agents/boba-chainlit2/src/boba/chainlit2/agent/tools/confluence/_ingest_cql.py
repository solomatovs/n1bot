"""Tool confluence_ingest_cql: индексация страниц, отобранных CQL-запросом.

Общий конфиг/pipeline — ingest_base.py (секция [tool.kb.confluence.ingest]).
"""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import Field

from boba.chainlit2.agent.tools.confluence.ingest_base import (
    ConfluenceIngestConfig,
)
from boba.chainlit2.agent.tools.confluence.ingest_caller import (
    ConfluenceIngestCaller,
)

__all__ = ["confluence_ingest_cql"]


def confluence_ingest_cql(
    cfg: ConfluenceIngestConfig,
    caller: ConfluenceIngestCaller,
    cql: Annotated[
        str,
        Field(
            min_length=1,
            description=(
                "CQL-запрос для discovery страниц. Пример: "
                '`space = DOCS AND lastModified > "2024-01-01"`. '
                "Используй для тонких фильтров (по дате, метке, автору) — "
                "когда не нужен весь space, но и конкретных page_id нет."
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
) -> dict[str, Any]:
    """Индексирует страницы Confluence, отобранные CQL-запросом, в KB.

    Возвращает JSON {collection, indexed, skipped_unchanged, pruned, failed}.
    """
    result = caller.ingest(
        config=cfg.model_dump(mode="json"),
        mode="cql",
        cql=cql,
        prune_missing=prune_missing,
        force_update=False,
    )
    return {"cql": cql, **result}
