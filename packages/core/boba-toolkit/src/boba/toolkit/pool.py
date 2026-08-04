"""Pool postgres, чьи соединения прерываются остановкой хода."""

from __future__ import annotations

from collections.abc import AsyncGenerator, Generator
from contextlib import asynccontextmanager, contextmanager
from typing import Any

import psycopg
from psycopg.rows import DictRow

from boba.db.postgres import AsyncPostgresPool
from boba.toolkit.cancellation import current_cancellation

__all__ = ["CancellablePool"]


class CancellablePool:
    """Делегат AsyncPostgresPool: регистрирует conn.cancel как прерыватель.

    cancel() у AsyncConnection синхронный и зовётся из чужого потока — это и нужно
    остановке хода: отмена asyncio сама по себе не прерывает запрос, идущий в базе.
    """

    def __init__(self, inner: AsyncPostgresPool) -> None:
        self._inner = inner

    @asynccontextmanager
    async def connection(self) -> AsyncGenerator[psycopg.AsyncConnection[Any], None]:
        async with self._inner.connection() as conn:
            with self._abort(conn):
                yield conn

    @asynccontextmanager
    async def cursor(self) -> AsyncGenerator[psycopg.AsyncCursor[Any], None]:
        async with self._inner.cursor() as cur:
            with self._abort(cur.connection):
                yield cur

    @asynccontextmanager
    async def dict_cursor(self) -> AsyncGenerator[psycopg.AsyncCursor[DictRow], None]:
        async with self._inner.dict_cursor() as cur:
            with self._abort(cur.connection):
                yield cur

    async def close(self) -> None:
        await self._inner.close()

    @staticmethod
    @contextmanager
    def _abort(conn: psycopg.AsyncConnection[Any]) -> Generator[None]:
        with current_cancellation().abort_with(conn.cancel):
            yield
