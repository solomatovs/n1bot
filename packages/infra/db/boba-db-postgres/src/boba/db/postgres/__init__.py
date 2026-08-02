"""boba.db.postgres — Postgres-конфиг + sync/async-пулы для tool-пакетов и приложений.

Использование:
    cfg = PostgresConfig(
        host="db.local", user="reader", dbname="app",
        pool={"timeout": 10.0}, options={"statement_timeout": "5s"},
    )
    pool = PostgresPool.get(
        cfg, override_options={"default_transaction_read_only": "on"}
    )
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT 1")
        cur.fetchone()
"""

from __future__ import annotations

from boba.db.postgres.async_pool import AsyncPostgresPool
from boba.db.postgres.config import (
    PostgresConfig,
    PostgresOptionsConfig,
    PostgresPoolConfig,
)
from boba.db.postgres.errors import PostgresError, PostgresPoolClosedError
from boba.db.postgres.pool import PostgresPool

__all__ = [
    "AsyncPostgresPool",
    "PostgresConfig",
    "PostgresError",
    "PostgresOptionsConfig",
    "PostgresPool",
    "PostgresPoolClosedError",
    "PostgresPoolConfig",
]
