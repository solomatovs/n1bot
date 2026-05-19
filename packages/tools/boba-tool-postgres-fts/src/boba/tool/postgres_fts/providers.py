"""DI-провайдеры PostgresPool и PgFtsKnowledgeBase.

`@provides`-функции конструируются framework'ом один раз на жизнь
агента (Scope.APP). `provide_pool` — generator-provider: на teardown
контейнера Dishka вызовет `pool.close()`, освободив все connection'ы.

Включение/отключение плагина целиком — забота framework'а
(`AgentBuilder.discover_plugins` читает `[tool.postgres_fts]` enable).
Если плагин не enabled, его модуль не импортируется → providers не
видны → pool не создаётся.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Annotated

from boba.db.postgres import PostgresConfig, PostgresPool
from boba.tool.postgres_fts.config import PostgresFtsPluginConfig
from boba.tool.postgres_fts.db import PgFtsKnowledgeBase
from boba.tools import FromConfig, FromDI, Scope, provides

__all__ = ["provide_kb", "provide_pool"]


@provides(scope=Scope.APP)
def provide_pool(
    cfg: Annotated[PostgresFtsPluginConfig, FromConfig()],
) -> Iterator[PostgresPool]:
    """PostgresPool с teardown — close() на завершении контейнера."""
    pool = PostgresPool.get(
        PostgresConfig(
            dsn=cfg.dsn,
            min_size=cfg.min_pool_size,
            max_size=cfg.max_pool_size,
            connect_timeout_sec=cfg.connect_timeout_sec,
        ),
    )
    try:
        yield pool
    finally:
        pool.close()


@provides(scope=Scope.APP)
def provide_kb(
    cfg: Annotated[PostgresFtsPluginConfig, FromConfig()],
    pool: Annotated[PostgresPool, FromDI(Scope.APP)],
) -> PgFtsKnowledgeBase:
    """KB поверх pool'а; всё read-only, lifetime = lifetime агента."""
    return PgFtsKnowledgeBase(
        pool=pool,
        indexes=cfg.indexes,
        snippet_options=cfg.snippet_options,
    )
