"""Tool list_targets: список профилей БД."""

from __future__ import annotations

from typing import Annotated

from boba.markdown import format_markdown_table
from boba.tool.pg.query import QueryConfig
from boba.tools import FromConfig, tool

__all__ = ["list_targets"]


@tool
def list_targets(
    query_cfg: Annotated[QueryConfig, FromConfig()],
) -> str:
    """Список доступных значений параметра target для PG-tool'ов."""
    databases = query_cfg.executor.databases
    rows = [
        (target, conn.host, conn.database)
        for target, conn in sorted(databases.items())
    ]
    return format_markdown_table(
        columns=["target", "host", "database"],
        rows=rows,  # type: ignore[arg-type]
        max_cell_chars=None,
    )
