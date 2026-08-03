"""Pool postgres, чьи соединения прерываются остановкой хода."""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from typing import Any

import psycopg
from psycopg.rows import DictRow

from boba.db.postgres import PostgresPool
from boba.toolkit.cancellation import current_cancellation

__all__ = ["CancellablePool"]


class CancellablePool:
    """Делегат PostgresPool: регистрирует conn.cancel как прерыватель."""

    def __init__(self, inner: PostgresPool) -> None:
        self._inner = inner

    @contextmanager
    def connection(self) -> Generator[psycopg.Connection[Any]]:
        with self._inner.connection() as conn, self._abort(conn):
            yield conn

    @contextmanager
    def cursor(self) -> Generator[psycopg.Cursor[Any]]:
        with self._inner.cursor() as cur, self._abort(cur.connection):
            yield cur

    @contextmanager
    def dict_cursor(self) -> Generator[psycopg.Cursor[DictRow]]:
        with self._inner.dict_cursor() as cur, self._abort(cur.connection):
            yield cur

    def close(self) -> None:
        self._inner.close()

    @staticmethod
    @contextmanager
    def _abort(conn: psycopg.Connection[Any]) -> Generator[None]:
        with current_cancellation().abort_with(conn.cancel):
            yield
