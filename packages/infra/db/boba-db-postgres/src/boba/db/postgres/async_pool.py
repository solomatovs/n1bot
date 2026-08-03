"""AsyncPostgresPool: async-обёртка над psycopg_pool.AsyncConnectionPool."""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

import psycopg
from psycopg.rows import DictRow, dict_row

from boba.db.postgres.config import PostgresConfig
from boba.db.postgres.errors import PostgresPoolClosedError

__all__ = ["AsyncPostgresPool"]

logger = logging.getLogger(__name__)


class AsyncPostgresPool:
    "async-обёртка над psycopg_pool.AsyncConnectionPool с явным open()/close()"

    def __init__(
        self,
        cfg: PostgresConfig,
        *,
        override_options: dict[str, str] | None = None,
    ) -> None:
        from psycopg_pool import AsyncConnectionPool  # noqa: PLC0415

        self._cfg = cfg
        self._pool = AsyncConnectionPool(
            connection_class=psycopg.AsyncConnection,
            kwargs=cfg.conn_settings(override_options),
            **cfg.pool_settings(),
            open=False,
        )
        self._closed = False
        logger.info(
            "AsyncPostgresPool created db=%s user=%s min_size=%d max_size=%s",
            cfg.dbname,
            cfg.user,
            cfg.pool.min_size,
            cfg.pool.max_size,
        )

    async def open(self) -> None:
        """Открыть пул (установить фоновые соединения)."""
        await self._pool.open()

    @property
    def raw(self) -> Any:
        """Внутренний psycopg_pool.AsyncConnectionPool (для langgraph-саверов)."""
        return self._pool

    @asynccontextmanager
    async def connection(self) -> AsyncGenerator[psycopg.AsyncConnection[Any], None]:
        """Взять AsyncConnection из пула."""
        if self._closed:
            raise PostgresPoolClosedError("PostgresPool is closed")

        async with self._pool.connection() as conn:
            yield conn

    @asynccontextmanager
    async def cursor(self) -> AsyncGenerator[psycopg.AsyncCursor[Any], None]:
        """AsyncConnection + tuple-cursor — одиночные запросы без row_factory."""
        async with self._pool.connection() as conn, conn.cursor() as cur:
            yield cur

    @asynccontextmanager
    async def client_cursor(
        self,
    ) -> AsyncGenerator[psycopg.AsyncClientCursor[Any], None]:
        """AsyncConnection + AsyncClientCursor (client-side parameter binding)."""
        async with (
            self._pool.connection() as conn,
            psycopg.AsyncClientCursor(conn) as cur,
        ):
            yield cur

    @asynccontextmanager
    async def dict_cursor(self) -> AsyncGenerator[psycopg.AsyncCursor[DictRow], None]:
        """AsyncConnection + dict-cursor (row_factory=dict_row)."""
        async with (
            self._pool.connection() as conn,
            conn.cursor(row_factory=dict_row) as cur,
        ):
            yield cur

    async def close(self) -> None:
        """Закрыть пул. Идемпотентно."""
        if self._closed:
            return
        self._closed = True
        await self._pool.close()
        logger.info("AsyncPostgresPool closed")
