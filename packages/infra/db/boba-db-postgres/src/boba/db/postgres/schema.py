"""Схема postgres хранилища и транзакционный advisory-замок: то, что нужно
каждому хранилищу до первого запроса.

Ошибки:
PostgresError — схему создать не удалось не по правам либо таблица есть в
    обеих схемах при переносе; соединение или пул отказали.
"""

from __future__ import annotations

import logging
from typing import Any, ClassVar

import psycopg
from psycopg import sql
from psycopg.errors import InsufficientPrivilege

from boba.db.postgres.async_pool import AsyncPostgresPool
from boba.db.postgres.errors import PostgresError
from boba.db.postgres.query import PgQueryBuilder

__all__ = ["AdvisoryLock", "PostgresSchema"]

logger = logging.getLogger(__name__)


class AdvisoryLock:
    """Транзакционный advisory-замок Postgres по текстовому ключу.

    Ключ хэшируется на сервере, замок снимается с концом транзакции. Им
    сериализуют DDL при одновременном старте процессов кластера и запись по
    области: держатель — тот, кто первым выполнил acquire в своей транзакции.
    """

    def __init__(self, key: str) -> None:
        self._key = key

    @property
    def key(self) -> str:
        return self._key

    async def acquire(self, conn: psycopg.AsyncConnection[Any]) -> None:
        query = (
            PgQueryBuilder()
            .add(
                "select pg_advisory_xact_lock(hashtextextended(%(key)s, 0))",
                key=self._key,
            )
            .build()
        )
        await conn.execute(query.text, query.params)


class PostgresSchema:
    """Схема postgres хранилища: идентификатор для запросов, создание с
    терпимостью к отсутствию прав, замок DDL и сведения о таблицах.

    Создаёт её PostgresTable по имени из конфига; без права на create schema
    её заводит администратор, и отказ по правам только пишется в лог.
    """

    DDL_LOCK_PREFIX: ClassVar[str] = "boba.ddl."
    """Ключ замка DDL складывается из префикса и имени схемы."""

    def __init__(self, name: str) -> None:
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    @property
    def ident(self) -> sql.Identifier:
        return sql.Identifier(self._name)

    def ddl_lock(self) -> AdvisoryLock:
        """Замок, под которым процессы по очереди создают таблицы схемы."""
        return AdvisoryLock(self.DDL_LOCK_PREFIX + self._name)

    async def ensure(self, conn: psycopg.AsyncConnection[Any]) -> None:
        """Схема есть; повтор безвреден, отказ по правам — запись в лог."""
        query = (
            PgQueryBuilder(schema=self.ident)
            .add("create schema if not exists {schema}")
            .build()
        )

        try:
            async with conn.transaction():
                await conn.execute(query.text, query.params, prepare=False)
        except InsufficientPrivilege:
            logger.info(
                "no permission for create schema %r, assuming an administrator "
                "created it",
                self._name,
            )

    async def ensure_with(self, pool: AsyncPostgresPool) -> None:
        """То же на соединении из пула; отказ пула — PostgresError."""
        try:
            async with pool.connection() as conn:
                await self.ensure(conn)
        except PostgresError:
            raise
        except Exception as exc:
            msg = f"ensuring schema {self._name!r} on a pool connection failed: {exc}"
            raise PostgresError(msg) from exc

    async def has_table(self, conn: psycopg.AsyncConnection[Any], table: str) -> bool:
        query = (
            PgQueryBuilder()
            .add(
                """
                select 1
                from information_schema.tables
                where table_schema = %(schema)s and table_name = %(table)s
                """,
                schema=self._name,
                table=table,
            )
            .build()
        )
        cur = await conn.execute(query.text, query.params)

        return await cur.fetchone() is not None

    async def columns_of(
        self, conn: psycopg.AsyncConnection[Any], table: str
    ) -> set[str]:
        """Имена колонок таблицы по information_schema; пусто — таблицы нет."""
        query = (
            PgQueryBuilder()
            .add(
                """
                select column_name
                from information_schema.columns
                where table_schema = %(schema)s and table_name = %(table)s
                """,
                schema=self._name,
                table=table,
            )
            .build()
        )
        cur = await conn.execute(query.text, query.params)
        rows = await cur.fetchall()

        names: set[str] = set()
        for row in rows:
            names.add(str(row[0]))

        return names

    async def move_table(
        self, conn: psycopg.AsyncConnection[Any], table: str, target: PostgresSchema
    ) -> None:
        """Таблица переезжает из этой схемы в target (перевод выпусков на
        месте); пустой дубль здесь рядом с уже переехавшей — сносится.

        Ошибки:
        PostgresError — таблица есть в обеих схемах и здесь в ней есть строки.
        """
        if not await self.has_table(conn, table):
            return

        source = sql.Identifier(self._name, table)

        if await target.has_table(conn, table):
            probe = (
                PgQueryBuilder(source=source)
                .add("select 1 from {source} limit 1")
                .build()
            )
            cur = await conn.execute(probe.text, probe.params)
            if await cur.fetchone() is not None:
                msg = (
                    f"table {table} exists both in {self._name} and {target.name} "
                    f"with rows in {self._name}; merge them by hand and restart"
                )
                raise PostgresError(msg)

            drop = (
                PgQueryBuilder(source=source).add("drop table {source} cascade").build()
            )
            await conn.execute(drop.text, drop.params)
            return

        move = (
            PgQueryBuilder(source=source, target=target.ident)
            .add("alter table {source} set schema {target}")
            .build()
        )
        await conn.execute(move.text, move.params)
