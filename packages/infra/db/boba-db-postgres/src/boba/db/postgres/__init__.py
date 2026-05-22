"""boba.db.postgres — read-only PG-пул для tool-пакетов.

Использование:
    cfg = PostgresConfig(
        dsn="postgresql://reader:***@db.example/app"
            "?default_transaction_read_only=on&statement_timeout=5000",
    )
    pool = PostgresPool.get(cfg)
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT 1")
        cur.fetchone()
"""

from __future__ import annotations

from boba.db.postgres.config import PostgresConfig
from boba.db.postgres.connection import PostgresConnection
from boba.db.postgres.errors import PostgresError, PostgresPoolClosedError
from boba.db.postgres.pool import PostgresPool

__all__ = [
    "PostgresConfig",
    "PostgresConnection",
    "PostgresError",
    "PostgresPool",
    "PostgresPoolClosedError",
]
