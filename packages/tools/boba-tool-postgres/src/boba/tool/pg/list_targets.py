"""Tool list_targets: список профилей БД."""

from __future__ import annotations

from typing import Annotated

from boba.tool.pg.executor import SqlExecutorConfig
from boba.tools import FromConfig, tool
from boba.tools.domain import TableResult

__all__ = ["list_targets"]


@tool
def list_targets(
    cfg: Annotated[SqlExecutorConfig, FromConfig()],
) -> TableResult:
    """Список доступных значений параметра target для PG-tool'ов."""
    rows = [{"target": target} for target in cfg.targets()]
    return TableResult(rows=rows)
