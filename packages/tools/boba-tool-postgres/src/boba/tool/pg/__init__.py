"""boba-tool-postgres: read-only SQL-tools для LLM."""

from __future__ import annotations

from boba.tool.pg.describe_table import DescribeTableConfig, describe_table
from boba.tool.pg.executor import (
    SqlExecutor,
    SqlExecutorConfig,
    SqlQueryError,
    SqlResult,
)
from boba.tool.pg.list_tables import ListTablesConfig, list_tables
from boba.tool.pg.list_targets import list_targets
from boba.tool.pg.query import QueryConfig, query

__all__ = [
    "DescribeTableConfig",
    "ListTablesConfig",
    "QueryConfig",
    "SqlExecutor",
    "SqlExecutorConfig",
    "SqlQueryError",
    "SqlResult",
    "describe_table",
    "list_tables",
    "list_targets",
    "query",
]
