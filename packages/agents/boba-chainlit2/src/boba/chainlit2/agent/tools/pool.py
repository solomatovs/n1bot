"""Pool postgres, чьи соединения прерываются остановкой хода.

Долгий SQL держит рабочий поток внутри libpq, где проверить флаг отмены
нечем. Единственный внешний способ оборвать запрос — conn.cancel из другого
потока, поэтому каждое выданное соединение регистрирует его как прерыватель
на время работы.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

from boba.chainlit2.agent.cancellation import current_cancellation

if TYPE_CHECKING:
    import psycopg
    from psycopg.rows import DictRow

    from boba.db.postgres import PostgresPool

__all__ = ["CancellablePool"]


class CancellablePool:
    """Делегат PostgresPool, добавляющий отмену на каждое соединение.

    Наследование не подходит: PostgresPool раздаётся фабрикой-синглтоном
    PostgresPool.get, поэтому обёртка делегирует, а не подменяет тип.
    """

    def __init__(self, inner: PostgresPool) -> None:
        self._inner = inner

    @contextmanager
    def connection(self) -> Iterator[psycopg.Connection[Any]]:
        with self._inner.connection() as conn, self._abort(conn):
            yield conn

    @contextmanager
    def cursor(self) -> Iterator[psycopg.Cursor[Any]]:
        with self._inner.cursor() as cur, self._abort(cur.connection):
            yield cur

    @contextmanager
    def dict_cursor(self) -> Iterator[psycopg.Cursor[DictRow]]:
        with self._inner.dict_cursor() as cur, self._abort(cur.connection):
            yield cur

    def close(self) -> None:
        self._inner.close()

    @staticmethod
    @contextmanager
    def _abort(conn: psycopg.Connection[Any]) -> Iterator[None]:
        with current_cancellation().abort_with(conn.cancel):
            yield
