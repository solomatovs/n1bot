"""boba-tool-postgres: read-only SQL/FTS-tools для LLM."""

from __future__ import annotations

from boba.tool.pg.describe_table import DescribeTableConfig, describe_table
from boba.tool.pg.executor import (
    SqlExecutor,
    SqlExecutorConfig,
    SqlQueryError,
    SqlResult,
)
from boba.tool.pg.fts_executor import (
    FtsExecutorConfig,
    FtsHit,
    FtsQueryError,
    IndexSpec,
    PgFtsExecutor,
)
from boba.tool.pg.fts_search import FtsSearchConfig, fts_search
from boba.tool.pg.list_tables import ListTablesConfig, list_tables
from boba.tool.pg.list_targets import list_targets
from boba.tool.pg.query import QueryConfig, query

__all__ = [
    "DescribeTableConfig",
    "FtsExecutorConfig",
    "FtsHit",
    "FtsQueryError",
    "FtsSearchConfig",
    "IndexSpec",
    "ListTablesConfig",
    "PgFtsExecutor",
    "QueryConfig",
    "SqlExecutor",
    "SqlExecutorConfig",
    "SqlQueryError",
    "SqlResult",
    "describe_table",
    "fts_search",
    "list_tables",
    "list_targets",
    "query",
]
