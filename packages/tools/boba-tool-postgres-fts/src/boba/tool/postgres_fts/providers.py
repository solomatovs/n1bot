"""DI-провайдеры PostgresPool и PgFtsKnowledgeBase для v2.

`@provides`-функции конструируются framework'ом один раз на жизнь
агента (Scope.APP). `provide_pool` — generator-provider: на teardown
контейнера Dishka вызовет `pool.close()`, освободив все connection'ы.

Оба провайдера навешены на `enable_if=_pg_fts_active` — если плагин
выключен, ни pool, ни KB не создаются (Postgres-соединение не открывается).
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Annotated

from boba.db.postgres import PostgresConfig, PostgresPool
from boba.tool.postgres_fts.config import PostgresFtsPluginConfig
from boba.tool.postgres_fts.db import PgFtsKnowledgeBase
from boba.tools import FromConfig, FromDI, Scope, provides

__all__ = ["provide_kb", "provide_pool"]


def _pg_fts_active(
    cfg: Annotated[PostgresFtsPluginConfig, FromConfig()],
) -> bool:
    """Provider включён при `enable=True`. dsn/indexes валидируются конфигом."""
    return cfg.enable


@provides(scope=Scope.APP, enable_if=_pg_fts_active)
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


@provides(scope=Scope.APP, enable_if=_pg_fts_active)
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
