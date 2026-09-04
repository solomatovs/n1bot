"""Имена таблиц и колонок в SQL psycopg и схема под них.

Enum'ы имён живут в core рядом с моделями; здесь — их перевод в идентификаторы и
создание схемы с одинаковой реакцией на отсутствие прав.

Ошибки:
PostgresError — схему создать не удалось не по правам: соединение или пул отказали.
"""

from __future__ import annotations

import logging
from enum import StrEnum

from psycopg import AsyncConnection, sql
from psycopg.errors import InsufficientPrivilege

from boba.db.postgres.async_pool import AsyncPostgresPool, PostgresError

__all__ = ["PostgresSchema", "SqlNames"]

logger = logging.getLogger(__name__)


class SqlNames:
    """Идентификаторы SQL из enum'ов имён."""

    @staticmethod
    def ident(name: StrEnum) -> sql.Identifier:
        return sql.Identifier(name.value)

    @staticmethod
    def table(schema: str, name: StrEnum) -> sql.Identifier:
        return sql.Identifier(schema, name.value)


class PostgresSchema:
    """Создание схемы: без прав на CREATE SCHEMA её заводит администратор."""

    @staticmethod
    async def ensure(conn: AsyncConnection, schema: str) -> None:
        try:
            async with conn.transaction():
                await conn.execute(
                    sql.SQL("create schema if not exists {schema}").format(
                        schema=sql.Identifier(schema)
                    ),
                    prepare=False,
                )
        except InsufficientPrivilege:
            logger.info(
                "no permission for create schema %r, assuming an administrator "
                "created it",
                schema,
            )

    @classmethod
    async def ensure_with(cls, pool: AsyncPostgresPool, schema: str) -> None:
        """То же на соединении из пула; отказ пула — PostgresError."""
        try:
            async with pool.connection() as conn:
                await cls.ensure(conn, schema)
        except PostgresError:
            raise
        except Exception as exc:
            msg = f"ensuring schema {schema!r} on a pool connection failed: {exc}"
            raise PostgresError(msg) from exc
