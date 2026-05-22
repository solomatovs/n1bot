"""Subpackage `sql`: ad-hoc SQL-tools для read-only exploratory-аналитики.

Tools:
- `sql_list_tables(schema=None)`        — список таблиц/view (с фильтром по schema).
- `sql_describe_table(table, schema)`   — колонки/типы конкретной таблицы.
- `sql_query(query, row_limit)`         — произвольный SQL, результат — markdown.

Безопасность — только access-control + libpq session-параметры в DSN
(нет SQL-side keyword-валидатора, защита делегирована Postgres):

- Отдельная секция `[tool.kb.sql]` с собственным DSN — оператор даёт LLM
  ограниченную read-only роль (`GRANT SELECT ON ...`). PG отклоняет любые
  DDL/DML с `permission denied`.
- `default_transaction_read_only=on` зашит в DSN через libpq
  `options='-c ...'` (см. `SqlConfig.session_options`) — двойная защита
  поверх прав роли. PG применяет при коннекте, никакой `SET`-statement
  или `configure`-callback не нужен.
- `statement_timeout=<ms>` (тоже через `options=`) — runtime-cap.
- Row cap: `cur.fetchmany(max_rows + 1)` — psycopg-side cap, `LIMIT` в
  SQL не инжектится (LLM сам решает).
- Cell-truncation: длинные значения → `value… (max_cell_chars)`.
- Бинарные значения → `<bytes:N>` placeholder.
"""

from __future__ import annotations

from boba.tool.kb.sql.config import SqlConfig
from boba.tool.kb.sql.executor import SqlExecutor, SqlQueryError, SqlResult
from boba.tool.kb.sql.providers import provide_sql_executor
from boba.tool.kb.sql.tools.describe_table import sql_describe_table
from boba.tool.kb.sql.tools.list_tables import sql_list_tables
from boba.tool.kb.sql.tools.query import sql_query

__all__ = [
    "SqlConfig",
    "SqlExecutor",
    "SqlQueryError",
    "SqlResult",
    "provide_sql_executor",
    "sql_describe_table",
    "sql_list_tables",
    "sql_query",
]
