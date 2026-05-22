"""Subpackage `sql`: ad-hoc SQL-tools для read-only exploratory-аналитики.

Tools (каждый — self-contained tool-конфиг):
- `sql_query(query, row_limit)`        — произвольный SQL → markdown.
                                          Секция `[tool.kb.sql_query]`.
- `sql_list_tables(schema=None)`       — список таблиц/view.
                                          Секция `[tool.kb.sql_list_tables]`.
- `sql_describe_table(table, schema)`  — колонки/типы конкретной таблицы.
                                          Секция `[tool.kb.sql_describe_table]`.

Безопасность — только access-control + libpq session-параметры в DSN
(нет SQL-side keyword-валидатора, защита делегирована Postgres):

- Connection задаётся в каждой секции независимо — оператор может дать
  LLM ограниченную read-only роль (`GRANT SELECT ON ...`). PG отклоняет
  любые DDL/DML с `permission denied`.
- `default_transaction_read_only=on` зашит в DSN через libpq
  `options='-c ...'` (см. `SqlExecutorConfig.session_options`) — двойная
  защита поверх прав роли. PG применяет при коннекте.
- `statement_timeout=<ms>` (тоже через `options=`) — runtime-cap.
- Row cap: `cur.fetchmany(max_rows + 1)` — psycopg-side cap.
- Cell-truncation: длинные значения → `value… (max_cell_chars)`.
"""

from __future__ import annotations

from boba.tool.kb.sql.executor import (
    SqlExecutor,
    SqlExecutorConfig,
    SqlQueryError,
    SqlResult,
)
from boba.tool.kb.sql.tools.describe_table import (
    SqlDescribeTableConfig,
    sql_describe_table,
)
from boba.tool.kb.sql.tools.list_tables import SqlListTablesConfig, sql_list_tables
from boba.tool.kb.sql.tools.query import SqlQueryConfig, sql_query

__all__ = [
    "SqlDescribeTableConfig",
    "SqlExecutor",
    "SqlExecutorConfig",
    "SqlListTablesConfig",
    "SqlQueryConfig",
    "SqlQueryError",
    "SqlResult",
    "sql_describe_table",
    "sql_list_tables",
    "sql_query",
]
