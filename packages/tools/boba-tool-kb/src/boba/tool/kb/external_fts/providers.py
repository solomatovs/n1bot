"""DI-провайдер `PgFtsKnowledgeBase` (read-only FTS по whitelist-индексам).

Только один provider — `PgFtsKnowledgeBase`. Pool НЕ объявляется как
отдельный `@provides` (чтобы не было type-clash с `provide_postgres_pool`
из `boba.tool.kb.providers`), а резолвится внутри:

- если `external_fts.dsn` пуст ИЛИ совпадает с `kb.dsn` → используется
  `PostgresPool` из DI (тот же singleton, что и у kb_search) —
  гарантированно один Pool с register_vector configure-hook'ом
  (для FTS-запросов это безвредно).
- иначе → создаём свой Pool через `PostgresPool.get(...)` с pool-sizes
  из `KbPluginConfig` (чтобы process-singleton cache в `PostgresPool`
  мог переиспользовать, если DSN тоже совпадёт случайно).

`PostgresPool.get(...)` сам кэширует по (dsn, min_size, max_size,
connect_timeout) — даже на одном DSN при разных sizes получим разные
pools, поэтому шаринг гарантируем явной инжекцией kb-pool'а из DI.
"""

from __future__ import annotations

from typing import Annotated

from boba.db.postgres import PostgresConfig, PostgresPool
from boba.tool.kb.config import KbPluginConfig
from boba.tool.kb.external_fts.config import ExternalFtsConfig
from boba.tool.kb.external_fts.db import PgFtsKnowledgeBase
from boba.tools import FromConfig, FromDI, Scope, provides

__all__ = ["provide_external_fts_kb"]


@provides(scope=Scope.APP)
def provide_external_fts_kb(
    fts_cfg: Annotated[ExternalFtsConfig, FromConfig()],
    kb_cfg: Annotated[KbPluginConfig, FromConfig()],
    kb_pool: Annotated[PostgresPool, FromDI(Scope.APP)],
) -> PgFtsKnowledgeBase:
    """KB поверх PostgresPool; whitelist из `ExternalFtsConfig.indexes`.

    DSN-fallback: пустой `external_fts.dsn` или совпадающий с
    `[tool.kb].dsn` → переиспользуем kb-pool. Иначе — отдельный Pool
    с pool-sizes от `KbPluginConfig` (через `PostgresPool.get`
    process-singleton). Lifetime — APP; для отдельного pool'а close
    НЕ зовём (общий `PostgresPool._CACHE` удалится на process exit).
    """
    if not fts_cfg.dsn or fts_cfg.dsn == kb_cfg.dsn:
        pool = kb_pool
    else:
        pool = PostgresPool.get(
            PostgresConfig(
                dsn=fts_cfg.dsn,
                min_size=kb_cfg.pool_min_size,
                max_size=kb_cfg.pool_max_size,
            ),
        )
    return PgFtsKnowledgeBase(
        pool=pool,
        indexes=fts_cfg.indexes,
        snippet_options=fts_cfg.snippet_options,
    )
