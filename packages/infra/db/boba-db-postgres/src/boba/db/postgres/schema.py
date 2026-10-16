"""Схема postgres хранилища и транзакционный advisory-замок: то, что нужно
каждому хранилищу до первого запроса, и применение файлов schema/*.sql пакета.

Ошибки:
PostgresError — схему создать не удалось не по правам, каталога файлов схемы
    нет, он пуст или сервер отклонил файл, соединение или пул отказали.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from importlib.resources.abc import Traversable
from operator import attrgetter
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
    терпимостью к отсутствию прав, замок DDL, применение файлов schema/*.sql
    пакета и сведения о таблицах.

    Создаёт её PostgresTable по имени из конфига, а DDL на старте процесса
    применяют корень приложения, хранилища плагинов, история агента и
    накат ix; без права на create schema её заводит администратор, и отказ
    по правам только пишется в лог.
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

    async def apply_files(
        self,
        conn: psycopg.AsyncConnection[Any],
        schema_dir: Traversable,
        names: Mapping[str, sql.Composable],
    ) -> Sequence[str]:
        """Файлы *.sql каталога schema_dir по порядку имён в этой схеме; итог —
        имена применённых файлов. Схема создаётся, каждый файл идёт своей
        транзакцией под замком DDL схемы: процессы кластера стартуют разом, а
        значение enum из alter type add value можно использовать только в
        следующем файле. {schema} и имена names подставляются стоящими
        именами; файлы идемпотентны, повтор безвреден.

        Ошибки:
        PostgresError — каталога нет, он пуст или сервер отклонил файл.
        """
        files = self._files(schema_dir)

        async with conn.transaction():
            await self.ddl_lock().acquire(conn)
            await self.ensure(conn)

        applied: list[str] = []
        for path in files:
            query = PgQueryBuilder(schema=self.ident, **names).from_file(path).build()
            try:
                async with conn.transaction():
                    await self.ddl_lock().acquire(conn)
                    await conn.execute(query.text, query.params, prepare=False)
            except psycopg.Error as exc:
                msg = (
                    f"applying {path.name} of {schema_dir} in schema {self._name} "
                    f"failed: {exc}"
                )
                raise PostgresError(msg) from exc

            logger.info("schema %s: applied %s", self._name, path.name)
            applied.append(path.name)

        return applied

    def _files(self, schema_dir: Traversable) -> list[Traversable]:
        """Файлы *.sql каталога по порядку имён.

        Ошибки:
        PostgresError — каталога нет или в нём нет *.sql.
        """
        if not schema_dir.is_dir():
            msg = f"schema {self._name}: schema directory {schema_dir} does not exist"
            raise PostgresError(msg)

        files: list[Traversable] = []
        for path in schema_dir.iterdir():
            if path.name.endswith(".sql"):
                files.append(path)

        if not files:
            msg = f"schema {self._name}: no *.sql files under {schema_dir}"
            raise PostgresError(msg)

        files.sort(key=attrgetter("name"))

        return files

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
