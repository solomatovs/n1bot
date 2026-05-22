"""`SqlExecutorConfig` + `SqlExecutor` — read-only SQL execute с safety limits.

Tool'ы (`sql_query`, `sql_list_tables`, `sql_describe_table`) принимают
`SqlExecutorConfig` (composite-BaseModel: connection + safety limits) как
nested-поле в своих tool-конфигах, и строят `SqlExecutor` inline через
`cfg.executor.build()`.

Безопасность — только на уровне доступов (DSN-роли) + libpq
session-параметров, зашитых в DSN через `options=`-параметр; никакой
интроспекции SQL'я (keyword-валидатора нет, LIMIT не инжектится — LLM
пишет запрос как считает нужным):

- DSN роль должна быть read-only (`GRANT SELECT ON ...`). Это
  **первичный** guard: PG сам отклонит INSERT/DROP/etc.
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

from pydantic import BaseModel, Field

from boba.db.postgres import PostgresPool
from boba.tool.kb.core.postgres_connection import PostgresConnection

logger = logging.getLogger(__name__)

__all__ = ["SqlExecutor", "SqlExecutorConfig", "SqlQueryError", "SqlResult"]


class SqlExecutorConfig(BaseModel):
    """Composite-конфиг для `SqlExecutor`: connection + safety limits.

    Не settings (нет `config_path`) — встраивается как nested-поле в
    tool-конфиги `SqlQueryConfig` / `SqlListTablesConfig` /
    `SqlDescribeTableConfig`. Благодаря рекурсивному flatten'у все поля
    (host/port/.../max_rows/max_cell_chars/statement_timeout_ms) лежат
    плоско в корневой TOML-секции tool'а.
    """

    connection: PostgresConnection
    max_rows: int = Field(
        default=100,
        ge=1,
        description=(
            "Жёсткий потолок числа строк в результате `sql_query`. LLM "
            "может попросить меньше через `row_limit`, но не больше."
        ),
    )
    max_cell_chars: int = Field(
        default=200,
        ge=1,
        description=(
            "Максимальная длина одного значения cell в markdown-таблице. "
            "Длинные строки обрезаются до `max_cell_chars` с суффиксом '…'."
        ),
    )
    statement_timeout_ms: int = Field(
        default=5000,
        ge=100,
        description=(
            "PG statement_timeout (мс) — зашивается в DSN через libpq "
            "параметр `options='-c statement_timeout=<ms>'`. PG применяет "
            "при коннекте, действует на ВСЕ statement'ы сессии."
        ),
    )

    def session_options(self) -> dict[str, str]:
        """Session-level GUC, зашиваемые в `options=`-параметр DSN."""
        return {
            "default_transaction_read_only": "on",
            "statement_timeout": str(self.statement_timeout_ms),
        }


class SqlQueryError(RuntimeError):
    """
    Ошибка выполнения SQL на стороне psycopg/PostgreSQL
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
    """
    SqlExecutor
    """

    def __init__(
        self,
        *,
        cfg: SqlExecutorConfig,
    ) -> None:
        self._cfg = cfg
        self._pool = PostgresPool.get(
            cfg.connection.to_pool_config(
                session_options=cfg.session_options(),
            ),
        )
        logger.info(
            "SqlExecutor opened: max_rows=%d max_cell_chars=%d",
            cfg.max_rows, cfg.max_cell_chars,
        )

    @property
    def max_cell_chars(self) -> int:
        return self._cfg.max_cell_chars

    @property
    def max_rows_cap(self) -> int:
        """Hard cap из конфига. Introspection-tools (`sql_list_tables`/
        `sql_describe_table`) фетчат до этого значения."""
        return self._cfg.max_rows

    def execute(
        self,
        query: str,
        *,
        row_limit: int,
        params: Sequence[Any] | None = None,
    ) -> SqlResult:
        """
        Execute SQL; возвращает rows + meta
        """
        effective_limit = min(max(row_limit, 1), self._cfg.max_rows)
        # +1 чтобы определить truncated (есть ли ещё строки сверх лимита).
        fetch_limit = effective_limit + 1

        try:
            with self._pool.cursor() as cur:
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
