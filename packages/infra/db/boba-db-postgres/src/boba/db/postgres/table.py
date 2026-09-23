"""Общее для хранилищ одной схемы postgres: ленивый пул, сборщик запросов со
схемой стоящим именем, граница ошибок слоя, транзакция с курсором словарей,
DDL под advisory-замком и проверка раскладки таблиц.

Ошибки:
PostgresError — пул, соединение или запрос отказали, строка не сложилась в
    модель или раскладка таблицы расходится с ожидаемой; наследник переводит
    её в ошибку своего слоя, подменив _failure.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Iterable, Mapping, Sequence
from contextlib import asynccontextmanager
from enum import StrEnum
from typing import Any, ClassVar, TypeVar

import psycopg
from psycopg import sql
from psycopg.rows import DictRow, dict_row
from pydantic import BaseModel, ValidationError

from boba.db.postgres.async_pool import AsyncPostgresPool, PostgresError, PostgresPool
from boba.db.postgres.connection import PostgresConfig
from boba.db.postgres.query import PgQuery, PgQueryBuilder
from boba.db.postgres.schema import AdvisoryLock, PostgresSchema

__all__ = ["Cursor", "ModelT", "PostgresTable"]

Cursor = psycopg.AsyncCursor[DictRow]
"""Курсор словарей, который отдаёт _transaction."""

ModelT = TypeVar("ModelT", bound=BaseModel)


class PostgresTable:
    """Базовый класс хранилищ схемы postgres; его наследуют таблицы чата и
    шины (boba-runtime), workflow, connections, каталог и kb-store.

    Пул берётся при первом обращении, потому что __init__ не может await;
    готовый пул приходит параметром. SQL наследник пишет в _query(): схема
    подставляется именем {schema}, значения уезжают параметрами. Отказ базы
    ловит _guarded и переводит в ошибку слоя тем, что вернёт _failure: здесь
    это PostgresError, наследник подменяет её своей и подписывает LABEL.
    """

    LABEL: ClassVar[str] = "postgres"
    """Подпись слоя в текстах ошибок."""

    def __init__(
        self,
        postgres: PostgresConfig | None,
        db_schema: str,
        pool: PostgresPool | None = None,
    ) -> None:
        """Без готового пула нужен конфиг подключения: пул возьмётся по нему.

        Ошибки:
        PostgresError — ни пула, ни конфига подключения.
        """
        if postgres is None and pool is None:
            msg = (
                f"table in schema {db_schema}: expected a pool or a postgres "
                "connection config, got neither"
            )
            raise PostgresError(msg)

        self._postgres = postgres
        self._schema = PostgresSchema(db_schema)
        self._pool_ref = pool

    @property
    def schema(self) -> str:
        return self._schema.name

    async def _pool(self) -> PostgresPool:
        if self._pool_ref is not None:
            return self._pool_ref

        self._pool_ref = await self._open_pool()

        return self._pool_ref

    async def _open_pool(self) -> PostgresPool:
        """Пул процесса по конфигу подключения; наследник со своим хуком
        соединения (register_vector) подменяет."""
        if self._postgres is None:
            msg = (
                f"table in schema {self._schema.name}: no pool given and no postgres "
                "connection config to build one"
            )
            raise PostgresError(msg)

        return await AsyncPostgresPool.get(self._postgres)

    def _query(self) -> PgQueryBuilder:
        """Сборщик запроса со схемой стоящим именем {schema}."""
        return PgQueryBuilder(schema=self._schema.ident)

    def _column_list(self, columns: Iterable[StrEnum]) -> sql.Composed:
        """Список колонок из enum'а имён — имя-фрагмент для {…} в запросе."""
        idents: list[sql.Composable] = []
        for column in columns:
            idents.append(sql.Identifier(column.value))

        return sql.SQL(", ").join(idents)

    def _detail(self, action: str, exc: Exception) -> str:
        return f"{self.LABEL}: {action} in schema {self._schema.name} failed: {exc}"

    def _failure(self, action: str, exc: Exception) -> Exception:
        """Ошибка слоя по отказу базы; наследник возвращает свой тип."""
        return PostgresError(self._detail(action, exc))

    @asynccontextmanager
    async def _guarded(self, action: str) -> AsyncGenerator[None, None]:
        """Граница слоя: отказ базы или пула уходит наружу ошибкой из _failure."""
        try:
            yield
        except (psycopg.Error, PostgresError) as exc:
            raise self._failure(action, exc) from exc

    @asynccontextmanager
    async def _transaction(self, action: str) -> AsyncGenerator[Cursor, None]:
        """Курсор словарей внутри одной транзакции на соединении из пула."""
        async with self._guarded(action):
            pool = await self._pool()
            async with (
                pool.connection() as conn,
                conn.transaction(),
                conn.cursor(row_factory=dict_row) as cur,
            ):
                yield cur

    async def _execute(self, query: PgQuery, action: str) -> int:
        """Запрос без выборки; сколько строк он затронул."""
        async with self._transaction(action) as cur:
            await cur.execute(query.text, query.params)

            return cur.rowcount

    async def _row(self, query: PgQuery, action: str) -> DictRow | None:
        async with self._transaction(action) as cur:
            await cur.execute(query.text, query.params)

            return await cur.fetchone()

    async def _rows(self, query: PgQuery, action: str) -> list[DictRow]:
        async with self._transaction(action) as cur:
            await cur.execute(query.text, query.params)

            return await cur.fetchall()

    def _returning(self, row: DictRow | None, what: str) -> DictRow:
        """Строка после insert/update … returning; её отсутствие — отказ хранилища."""
        if row is None:
            raise self._failure(what, PostgresError("the statement returned no row"))

        return row

    def _parse(self, model: type[ModelT], row: Mapping[str, Any]) -> ModelT:
        """Ошибки:
        ошибка слоя из _failure — строка не складывается в модель.
        """
        try:
            return model.model_validate(dict(row))
        except ValidationError as exc:
            reason = PostgresError(
                f"row {dict(row)!r} does not form a valid {model.__name__}: {exc}"
            )
            raise self._failure(f"parsing a {model.__name__} row", reason) from exc

    def _parse_all(
        self, model: type[ModelT], rows: Iterable[Mapping[str, Any]]
    ) -> list[ModelT]:
        parsed: list[ModelT] = []
        for row in rows:
            parsed.append(self._parse(model, row))

        return parsed

    async def _advisory_lock(self, cur: Cursor, key: str) -> None:
        """Транзакционный advisory-замок по ключу; снимается с концом транзакции."""
        await AdvisoryLock(key).acquire(cur.connection)

    async def _apply_ddl(self, statements: Sequence[PgQuery], *schemas: str) -> None:
        """Схемы (своя и названные) и DDL одной транзакцией под замком DDL
        своей схемы: процессы кластера стартуют разом, и без замка
        create … if not exists падает на уникальности каталога. Повтор
        безвреден."""
        async with self._guarded(f"applying {len(statements)} ddl statements"):
            pool = await self._pool()
            async with pool.connection() as conn, conn.transaction():
                await self._schema.ddl_lock().acquire(conn)
                await self._schema.ensure(conn)
                for name in schemas:
                    await PostgresSchema(name).ensure(conn)

                for statement in statements:
                    await conn.execute(statement.text, statement.params, prepare=False)

    async def _check_layouts(
        self, layouts: Mapping[str, Iterable[str]], schema: str | None = None
    ) -> None:
        """Колонки таблиц ровно те, что ожидает код: таблица старого выпуска,
        которую `create table if not exists` оставил как есть, — ошибка с
        расхождением и советом снести схему, а не тихая работа до первого
        запроса. schema — чужая схема таблиц; пусто — своя."""
        where = self._schema
        if schema is not None:
            where = PostgresSchema(schema)

        async with self._guarded("checking table layouts"):
            pool = await self._pool()
            async with pool.connection() as conn:
                for table, columns in layouts.items():
                    await self._check_layout(conn, where, table, set(columns))

    async def _check_layout(
        self,
        conn: psycopg.AsyncConnection[Any],
        where: PostgresSchema,
        table: str,
        expected: set[str],
    ) -> None:
        actual = await where.columns_of(conn, table)
        missing = sorted(expected - actual)
        unexpected = sorted(actual - expected)
        if not missing and not unexpected:
            return

        msg = (
            f"table {where.name}.{table} has a layout the code does not "
            f"expect: missing columns {missing}, unexpected columns "
            f"{unexpected}; the table was created by another release and "
            f"is not migrated, drop the schema ({where.name}) and restart"
        )
        raise PostgresError(msg)
