"""PostgresPool: тонкая обёртка над psycopg_pool.ConnectionPool + process-singleton."""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any, ClassVar

import psycopg
from psycopg.rows import DictRow, dict_row

from boba.db.postgres.config import PostgresConfig
from boba.db.postgres.errors import PostgresPoolClosedError

__all__ = ["PostgresPool"]

logger = logging.getLogger(__name__)


ConfigureConnection = Callable[[psycopg.Connection[Any]], None]
"""Hook на каждое новое соединение pool'а (pgvector/hstore type registration)."""


class PostgresPool:
    """
    обёртка над `psycopg_pool.ConnectionPool` - singleton

    Контракт read-only задаётся параметрами DSN
        `default_transaction_read_only=on`
        `statement_timeout=...`
    """

    _CacheKey = tuple[str, int, int, float]
    _CACHE: ClassVar[dict[_CacheKey, PostgresPool]] = {}

    def __init__(
        self,
        cfg: PostgresConfig,
        *,
        configure: ConfigureConnection | None = None,
    ) -> None:
        from psycopg_pool import ConnectionPool  # noqa: PLC0415

        self._cfg = cfg
        self._pool = ConnectionPool(
            conninfo=cfg.dsn,
            min_size=cfg.min_size,
            max_size=cfg.max_size,
            timeout=cfg.connect_timeout_sec,
            configure=configure,
            open=True,
        )
        self._closed = False
        logger.info(
            "PostgresPool opened min_size=%d max_size=%d configure=%s",
            cfg.min_size,
            cfg.max_size,
            "yes" if configure is not None else "no",
        )

    @contextmanager
    def connection(self) -> Iterator[psycopg.Connection[Any]]:
        """Взять connection из pool'а"""
        if self._closed:
            raise PostgresPoolClosedError("PostgresPool is closed")

        with self._pool.connection() as conn:
            yield conn

    @contextmanager
    def cursor(self) -> Iterator[psycopg.Cursor[Any]]:
        """Connection + tuple-cursor — для одиночных запросов без row_factory.

        Короткая запись частого паттерна:

            with pool.connection() as conn, conn.cursor() as cur:
                cur.execute(...)

        Если нужна транзакция, явно `with pool.connection() as conn:
        conn.transaction(); ...` — там pool.cursor() не подходит.
        """
        with self._pool.connection() as conn, conn.cursor() as cur:
            yield cur

    @contextmanager
    def client_cursor(self) -> Iterator[psycopg.ClientCursor[Any]]:
        """Connection + ClientCursor — psycopg3 client-side parameter binding.

        Используется там, где нужен `cur.mogrify(query, params) → str` без
        реального исполнения (рендер шаблона в литерально-параметризованный
        SQL для копи-пейста / pgbench / EXPLAIN-сессии). Серверный
        `psycopg.Cursor` не имеет `mogrify`-а — только `ClientCursor`.

        Короткая запись частого паттерна:

            with pool.client_cursor() as cur:
                rendered = cur.mogrify(query, params)
        """
        with self._pool.connection() as conn, psycopg.ClientCursor(conn) as cur:
            yield cur

    @contextmanager
    def dict_cursor(self) -> Iterator[psycopg.Cursor[DictRow]]:
        """Connection + dict-cursor (`row_factory=dict_row`) — `cur` рядного словаря.

        Короткая запись частого паттерна:

            with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                cur.execute(...)
        """
        with (
            self._pool.connection() as conn,
            conn.cursor(row_factory=dict_row) as cur,
        ):
            yield cur

    def close(self) -> None:
        """Закрыть pool. Идемпотентно."""
        if self._closed:
            return
        self._closed = True
        self._pool.close()
        logger.info("PostgresPool closed")

    @classmethod
    def get(
        cls,
        cfg: PostgresConfig,
        *,
        configure: ConfigureConnection | None = None,
    ) -> PostgresPool:
        """
        Process-singleton по полному состоянию `cfg`
        пересоздаёт закрытый pool

        configure применяется при первом создании pool'а
        для данного cfg при повторном с тем же DSN значение игнорируется
        """
        key: PostgresPool._CacheKey = (
            cfg.dsn,
            cfg.min_size,
            cfg.max_size,
            cfg.connect_timeout_sec,
        )
        pool = cls._CACHE.get(key)

        if pool is None or pool._closed:
            pool = cls(cfg, configure=configure)
            cls._CACHE[key] = pool

        return pool
