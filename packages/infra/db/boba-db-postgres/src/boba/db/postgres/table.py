"""Общее для таблиц одной схемы postgres: конфиг подключения, ленивый пул, имена
таблиц и колонок из enum'ов, выполнение DDL и запросов.

Ошибки:
PostgresError — пул, соединение или запрос отказали; сервис-владелец таблицы
    переводит её в ошибку своего слоя.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from enum import StrEnum
from typing import Any

import psycopg
from psycopg import sql
from psycopg.rows import tuple_row

from boba.connections.postgres import PostgresConfig
from boba.db.postgres.async_pool import AsyncPostgresPool, PostgresError
from boba.db.postgres.names import PostgresSchema, SqlNames

__all__ = ["PostgresTable"]


class PostgresTable:
    """База таблиц схемы: пул берётся при первом обращении, __init__ не может await.

    Ошибки: PostgresError — ни пула, ни конфига подключения.
    """

    def __init__(
        self,
        postgres: PostgresConfig | None,
        db_schema: str,
        pool: AsyncPostgresPool | None = None,
    ) -> None:
        """Без готового пула нужен конфиг подключения: пул возьмётся по нему."""
        if postgres is None and pool is None:
            msg = f"table in {db_schema}: neither a pool nor a connection config"
            raise PostgresError(msg)

        self._postgres = postgres
        self._schema = db_schema
        self._pool_ref = pool

    @property
    def schema(self) -> str:
        return self._schema

    async def _pool(self) -> AsyncPostgresPool:
        if self._pool_ref is not None:
            return self._pool_ref

        if self._postgres is None:
            msg = f"table in {self._schema}: no connection config for the pool"
            raise PostgresError(msg)

        self._pool_ref = await AsyncPostgresPool.get(self._postgres)

        return self._pool_ref

    def _table(self, name: StrEnum) -> sql.Identifier:
        """schema.table по имени из enum таблиц."""
        return SqlNames.table(self._schema, name)

    @staticmethod
    def _columns(columns: type[StrEnum]) -> dict[str, sql.Composable]:
        """Плейсхолдеры DDL: имя колонки → идентификатор, по enum колонок."""
        names: dict[str, sql.Composable] = {}
        for column in columns:
            names[column.value] = SqlNames.ident(column)

        return names

    async def _apply_ddl(self, statements: Sequence[sql.Composed]) -> None:
        """Схема и DDL одной транзакцией; повтор безвреден."""
        try:
            pool = await self._pool()
            async with pool.connection() as conn:
                await PostgresSchema.ensure(conn, self._schema)
                async with conn.transaction():
                    for statement in statements:
                        await conn.execute(statement, prepare=False)
        except psycopg.Error as exc:
            raise PostgresError(f"ddl failed in {self._schema}: {exc}") from exc

    async def _execute(self, query: sql.Composed, params: Mapping[str, Any]) -> None:
        try:
            pool = await self._pool()
            async with pool.connection() as conn:
                await conn.execute(query, params)
        except psycopg.Error as exc:
            raise PostgresError(f"query failed in {self._schema}: {exc}") from exc

    async def _fetch(
        self, query: sql.Composed, params: Mapping[str, Any]
    ) -> list[tuple[Any, ...]]:
        try:
            pool = await self._pool()
            async with (
                pool.connection() as conn,
                conn.cursor(row_factory=tuple_row) as cur,
            ):
                await cur.execute(query, params)
                return await cur.fetchall()
        except psycopg.Error as exc:
            raise PostgresError(f"query failed in {self._schema}: {exc}") from exc
