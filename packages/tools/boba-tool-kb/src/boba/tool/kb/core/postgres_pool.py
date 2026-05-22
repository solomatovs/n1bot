"""`open_kb_pool` — factory PostgresPool для KB-store (с `register_vector`).

`PostgresPool.get(...)` — singleton по DSN, поэтому повторные вызовы с
тем же `PostgresConnection` возвращают тот же объект pool'а. Configure-
hook `register_vector` регистрирует pgvector-типы на каждом fresh-коннекте
(без этого `embedding`-колонка приходит как plain str и `INSERT vector`
падает на cast).

Используется в каждом tool, которому нужен PG-доступ для kb-таблиц:
ingest-tools (write), search-tools (read), bootstrap-CLI (DDL).
"""

from __future__ import annotations

from pgvector.psycopg import register_vector

from boba.db.postgres import PostgresConnection, PostgresPool

__all__ = ["open_kb_pool"]


def open_kb_pool(connection: PostgresConnection) -> PostgresPool:
    """Открыть (или взять из кэша) `PostgresPool` под `connection`."""
    return PostgresPool.get(
        connection.to_pool_config(),
        configure=register_vector,
    )
