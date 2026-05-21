"""`SqlExecutor` — тонкий wrapper над PostgresPool с safety limits.

Tool'ы (`sql_query`, `sql_list_tables`, `sql_describe_table`) получают
`SqlExecutor` через FromDI и используют единый execute-path.

Безопасность — только на уровне доступов (DSN-роли) + libpq
session-параметров, зашитых в DSN через `options=`-параметр (см.
`SqlConfig.session_options` / `PostgresConnectionConfig.to_dsn`); никакой
интроспекции SQL'я (keyword-валидатора нет, LIMIT не инжектится — LLM
пишет запрос как считает нужным):

- DSN роль из `[tool.kb.sql]` должна быть read-only (`GRANT SELECT ON ...`).
  Это **первичный** guard: PG сам отклонит INSERT/DROP/etc.
- `default_transaction_read_only=on` (libpq `options=-c ...`) — двойная
  защита поверх прав роли. PG применяет при коннекте, действует на ВСЕ
  транзакции сессии. Блокирует INSERT/UPDATE/DELETE/COPY (DDL типа
  DROP/CREATE — нет, для них только права роли).
- `statement_timeout=<ms>` (тоже через libpq `options=-c ...`) —
  runtime-cap на длительность каждого statement.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from boba.db.postgres import PostgresPool

logger = logging.getLogger(__name__)

__all__ = ["SqlExecutor", "SqlQueryError", "SqlResult"]


class SqlQueryError(RuntimeError):
    """Ошибка выполнения SQL на стороне psycopg/PostgreSQL.

    Включает PG-side отказы read-only роли (`permission denied`,
    `cannot execute ... in a read-only transaction`) — это
    единственный механизм отклонения DDL/DML (без LLM-side валидатора).
    """


@dataclass(frozen=True)
class SqlResult:
    """Один результат `SqlExecutor.execute`."""

    columns: list[str]
    rows: list[tuple[Any, ...]]
    row_count: int          # фактический len(rows) — может быть < запрошенного cap'а
    limit_applied: int      # эффективный cap (fetchmany'ем до столько строк)
    truncated: bool         # row_count == limit_applied → есть ещё


class SqlExecutor:
    """Execute с safety-нагрузкой (row-cap; read-only/statement_timeout —
    в DSN через libpq `options=`, не в этом классе).

    Один execute-path, используется всеми sql_*-tool'ами. Возвращает
    `SqlResult` с column names + rows; форматирование в markdown — забота
    tool'ов (`format_markdown_table`).
    """

    def __init__(
        self,
        pool: PostgresPool,
        *,
        max_rows: int,
        max_cell_chars: int,
    ) -> None:
        self._pool = pool
        self._max_rows = max_rows
        self._max_cell_chars = max_cell_chars
        logger.info(
            "SqlExecutor opened: max_rows=%d max_cell_chars=%d",
            max_rows, max_cell_chars,
        )

    @property
    def max_cell_chars(self) -> int:
        return self._max_cell_chars

    @property
    def max_rows_cap(self) -> int:
        """Hard cap из конфига. Introspection-tools (`sql_list_tables`/
        `sql_describe_table`) фетчат до этого значения."""
        return self._max_rows

    def execute(
        self,
        query: str,
        *,
        row_limit: int,
        params: Sequence[Any] | None = None,
    ) -> SqlResult:
        """Execute SQL; возвращает rows + meta.

        `row_limit` — soft cap (обрезается до `max_rows`). Cap применяется
        через `cur.fetchmany(effective_limit + 1)` — `LIMIT` в самом
        SQL'е НЕ инжектится (LLM сам пишет LIMIT, если нужно).

        `params` — для параметризованных запросов tool'ами (например,
        sql_describe_table подставляет имя таблицы через placeholder).

        DDL/DML отклоняются PG-side: либо `permission denied` от
        read-only роли, либо `cannot execute X in a read-only transaction`
        от session-default `default_transaction_read_only=on` (зашит в
        DSN через libpq `options=`-параметр, см. `SqlConfig.session_options`).
        Ловим как `SqlQueryError`.
        """
        effective_limit = min(max(row_limit, 1), self._max_rows)
        # +1 чтобы определить truncated (есть ли ещё строки сверх лимита).
        fetch_limit = effective_limit + 1

        try:
            with self._pool.connection() as conn, conn.cursor() as cur:
                # psycopg ожидает LiteralString для query; здесь runtime-string
                # от LLM/internal tool'ов — runtime psycopg принимает str без
                # проблем, тип-check выключаем.
                cur.execute(query, params or ())  # type: ignore[arg-type]
                fetched = cur.fetchmany(fetch_limit)
                columns = [d.name for d in (cur.description or [])]
        except Exception as e:
            raise SqlQueryError(
                f"SQL execute failed: {type(e).__name__}: {e}",
            ) from e

        truncated = len(fetched) > effective_limit
        rows = [tuple(row) for row in fetched[:effective_limit]]
        return SqlResult(
            columns=columns,
            rows=rows,
            row_count=len(rows),
            limit_applied=effective_limit,
            truncated=truncated,
        )
