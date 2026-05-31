"""Tool list_targets: список профилей БД."""

from __future__ import annotations

from typing import Annotated

from boba.tool.pg.query import QueryConfig
from boba.tools import FromConfig, tool
from boba.tools.domain import TableResult

__all__ = ["list_targets"]


@tool
def list_targets(
    query_cfg: Annotated[QueryConfig, FromConfig()],
) -> TableResult:
    """Список доступных значений параметра target для PG-tool'ов."""
    databases = query_cfg.executor.databases
    rows = [{"target": target} for target in databases]
    return TableResult(rows=rows)
