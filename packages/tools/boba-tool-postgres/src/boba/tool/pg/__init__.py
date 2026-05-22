"""boba-tool-postgres — плагин ad-hoc read-only SQL/FTS-tools для LLM.

Архитектура **tool-centric**: каждый tool имеет свой self-contained
конфиг (`BobaFlatSettings` с собственной `config_path`). Сервисы
(`SqlExecutor`, `PgFtsExecutor`) строятся inline из nested
`SqlExecutorConfig` / `FtsExecutorConfig`.

Безопасность — только access-control + libpq session-параметры в DSN
(нет SQL-side keyword-валидатора, защита делегирована Postgres):

- Connection задаётся в каждой секции независимо — оператор может дать
  LLM ограниченную read-only роль (`GRANT SELECT ON ...`). PG отклоняет
  любые DDL/DML с `permission denied`.
- `default_transaction_read_only=on` зашит в DSN через libpq
  `options='-c ...'` — двойная защита поверх прав роли. PG применяет
  при коннекте.
- `statement_timeout=<ms>` (тоже через `options=`) — runtime-cap.
- Row cap: `cur.fetchmany(max_rows + 1)` — psycopg-side cap.
- Cell-truncation: длинные значения → `value… (max_cell_chars)`.
- FTS identifier'ы (schema/table/columns) приходят из конфига, не от
  LLM, и подставляются через `psycopg.sql.Identifier`.

Tools (что LLM реально вызывает):
- `query`           → `[tool.pg.query]`
- `list_tables`     → `[tool.pg.list_tables]`
- `describe_table`  → `[tool.pg.describe_table]`
- `fts_search`      → `[tool.pg.fts_search]`
"""

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
    "query",
]
