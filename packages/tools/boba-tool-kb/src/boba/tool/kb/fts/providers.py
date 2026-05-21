"""DI-провайдер `PgFtsKnowledgeBase` (read-only FTS по whitelist-таблице).

Pool НЕ объявляется как отдельный `@provides` (чтобы не было type-clash
с `provide_postgres_pool` из `boba.tool.kb.providers`), а резолвится
внутри:

- если `FtsConfig.dsn` пуст → используется `PostgresPool` из DI
  (тот же singleton, что и у kb_search) — гарантированно один Pool с
  register_vector configure-hook'ом (для FTS-запросов безвреден).
- иначе → создаём свой Pool через `PostgresPool.get(...)` с теми же
  pool-sizes/connect_timeout из `[tool.kb.postgres]`.
"""

from __future__ import annotations

from typing import Annotated

from boba.db.postgres import PostgresConfig, PostgresPool
from boba.tool.kb.fts.config import FtsConfig
from boba.tool.kb.fts.db import PgFtsKnowledgeBase
from boba.tool.kb.postgres_config import PostgresConnectionConfig
from boba.tools import FromConfig, FromDI, Scope, provides

__all__ = ["provide_fts_kb"]


@provides(scope=Scope.APP)
def provide_fts_kb(
    fts_cfg: Annotated[FtsConfig, FromConfig()],
    pg_cfg: Annotated[PostgresConnectionConfig, FromConfig()],
    kb_pool: Annotated[PostgresPool, FromDI(Scope.APP)],
) -> PgFtsKnowledgeBase:
    """`PgFtsKnowledgeBase` поверх PostgresPool; индекс из `FtsConfig.index`.

    DSN-fallback: `fts.dsn=""` → kb-pool из DI (тот же singleton).
    Иначе — отдельный Pool через `PostgresPool.get(...)` с pool-sizes
    из `[tool.kb.postgres]`. Lifetime — APP; для отдельного pool'а close
    НЕ зовём (общий `PostgresPool._CACHE` удалится на process exit).
    """
    if not fts_cfg.dsn:
        pool = kb_pool
    else:
        pool = PostgresPool.get(
            PostgresConfig(
                dsn=fts_cfg.dsn,
                min_size=pg_cfg.pool_min_size,
                max_size=pg_cfg.pool_max_size,
                connect_timeout_sec=pg_cfg.connect_timeout_sec,
            ),
        )
    return PgFtsKnowledgeBase(
        pool=pool,
        index=fts_cfg.index,
        snippet_options=fts_cfg.snippet_options,
    )
