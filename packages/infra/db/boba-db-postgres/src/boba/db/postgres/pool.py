"""PostgresPool: тонкая обёртка над psycopg_pool.ConnectionPool + process-singleton."""

from __future__ import annotations

import json
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
    "Обёртка-singleton над psycopg_pool.ConnectionPool; read-only задаётся опциями DSN"

    _CacheKey = tuple[str, tuple[tuple[str, str], ...]]
    _CACHE: ClassVar[dict[_CacheKey, PostgresPool]] = {}

    def __init__(
        self,
        cfg: PostgresConfig,
        *,
        override_options: dict[str, str] | None = None,
        configure: ConfigureConnection | None = None,
    ) -> None:
        from psycopg_pool import ConnectionPool  # noqa: PLC0415

        self._cfg = cfg
        self._pool = ConnectionPool(
            kwargs=cfg.conn_settings(override_options),
            **cfg.pool_settings(),
            configure=configure,
            open=True,
        )
        self._closed = False
        logger.info(
            "PostgresPool opened db=%s user=%s min_size=%d max_size=%s configure=%s",
            cfg.dbname,
            cfg.user,
            cfg.pool.min_size,
            cfg.pool.max_size,
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
        "Connection + tuple-cursor — для одиночных запросов без row_factory"
        with self._pool.connection() as conn, conn.cursor() as cur:
            yield cur

    @contextmanager
    def client_cursor(self) -> Iterator[psycopg.ClientCursor[Any]]:
        "Connection + ClientCursor: mogrify есть только у ClientCursor, не у серверного"
        with self._pool.connection() as conn, psycopg.ClientCursor(conn) as cur:
            yield cur

    @contextmanager
    def dict_cursor(self) -> Iterator[psycopg.Cursor[DictRow]]:
        "Connection + dict-cursor (row_factory=dict_row)"
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
        override_options: dict[str, str] | None = None,
        configure: ConfigureConnection | None = None,
    ) -> PostgresPool:
        """Process-singleton по cfg + override_options; закрытый pool пересоздаётся.

        configure применяется при первом создании, при повторном get игнорируется.
        """
        key: PostgresPool._CacheKey = (
            json.dumps(
                {**cfg.conn_settings(), **cfg.pool_settings()},
                sort_keys=True,
                default=str,
            ),
            tuple(sorted((override_options or {}).items())),
        )
        pool = cls._CACHE.get(key)

        if pool is None or pool._closed:
            pool = cls(cfg, override_options=override_options, configure=configure)
            cls._CACHE[key] = pool

        return pool
