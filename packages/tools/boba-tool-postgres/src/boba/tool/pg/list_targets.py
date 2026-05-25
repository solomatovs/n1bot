"""Tool list_targets: список профилей БД."""

from __future__ import annotations

from typing import Annotated

from boba.markdown import format_markdown_table
from boba.tool.pg.fts_search import FtsSearchConfig
from boba.tool.pg.query import QueryConfig
from boba.tools import FromConfig, tool

__all__ = ["list_targets"]


@tool
def list_targets(
    query_cfg: Annotated[QueryConfig, FromConfig()],
    fts_search_cfg: Annotated[FtsSearchConfig, FromConfig()],
) -> str:
    """Список доступных значений параметра target для PG-tool'ов.

    Колонка fts_searchable = yes, если у target есть IndexSpec и fts_search
    будет работать.
    """
    databases = query_cfg.executor.databases
    fts_targets = set(fts_search_cfg.executor.whitelists)

    rows = [
        (
            target,
            conn.host,
            conn.database,
            "yes" if target in fts_targets else "-",
        )
        for target, conn in sorted(databases.items())
    ]
    return format_markdown_table(
        columns=["target", "host", "database", "fts_searchable"],
        rows=rows,  # type: ignore[arg-type]
        max_cell_chars=None,
    )
