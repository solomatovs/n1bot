"""DI-провайдер `SqlExecutor`.

Свой `PostgresPool` (отдельный DSN от `[tool.kb.postgres]`) — read-only
SELECT-only роль. Pool НЕ shares с kb_pool — операторская граница
безопасности проходит здесь.

Read-only режим и statement_timeout зашиты прямо в DSN через libpq
`options=`-параметр (см. `SqlConfig.session_options` / `to_dsn`). PG
применяет их при коннекте — никакого `SET`-statement или
`configure`-callback не нужно.

Type-clash с `provide_postgres_pool` (kb-pool) обойдён тем, что наружу
из провайдера экспонируется не `PostgresPool` (тип-overlap), а
`SqlExecutor` — кастомный класс. Pool создаётся inside и не виден DI.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Annotated

from boba.db.postgres import PostgresPool
from boba.tool.kb.sql.config import SqlConfig
from boba.tool.kb.sql.executor import SqlExecutor
from boba.tools import FromConfig, Scope, provides

__all__ = ["provide_sql_executor"]


@provides(scope=Scope.APP)
def provide_sql_executor(
    cfg: Annotated[SqlConfig, FromConfig()],
) -> Iterator[SqlExecutor]:
    """SqlExecutor + свой PostgresPool. Generator-provider закрывает pool."""
    pool = PostgresPool.get(cfg.to_pool_config())
    try:
        yield SqlExecutor(
            pool=pool,
            max_rows=cfg.max_rows,
            max_cell_chars=cfg.max_cell_chars,
        )
    finally:
        pool.close()
