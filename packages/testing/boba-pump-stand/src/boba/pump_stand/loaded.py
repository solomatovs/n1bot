"""Таблица-приёмник на стороне postgres стенда: колонки из каталога, список
таблиц схемы, строки и текст значений для проверок приёмника."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from psycopg import sql

from boba.db.postgres import AsyncPostgresPool, PgQuery, PgQueryBuilder
from boba.pump_stand.sides import PostgresSide

__all__ = ["Loaded"]


class Loaded:
    """Таблица schema.table на postgres стенда."""

    def __init__(self, side: PostgresSide, schema: str, table: str) -> None:
        self._side = side
        self._schema = schema
        self._table = table

    async def columns(self) -> list[tuple[str, str, bool]]:
        """(имя, тип как format_type, not null) в порядке каталога."""
        query = (
            PgQueryBuilder()
            .add(
                "select a.attname, format_type(a.atttypid, a.atttypmod), a.attnotnull "
                "from pg_attribute a join pg_class c on c.oid = a.attrelid "
                "join pg_namespace n on n.oid = c.relnamespace "
                "where n.nspname = %(schema)s and c.relname = %(table)s "
                "and a.attnum > 0 and not a.attisdropped order by a.attnum",
                schema=self._schema,
                table=self._table,
            )
            .build()
        )
        rows = await self._rows(query)

        return [(str(r[0]), str(r[1]), bool(r[2])) for r in rows]

    async def tables(self) -> list[str]:
        query = (
            PgQueryBuilder()
            .add(
                "select c.relname from pg_class c join pg_namespace n "
                "on n.oid = c.relnamespace where n.nspname = %(schema)s "
                "and c.relkind = 'r' order by 1",
                schema=self._schema,
            )
            .build()
        )
        rows = await self._rows(query)

        return [str(r[0]) for r in rows]

    async def count(self) -> int:
        query = PgQueryBuilder(table=self._ident()).add("select count(*) from {table}")
        rows = await self._rows(query.build())

        return int(rows[0][0])

    async def texts(self, column: str, order_by: str = "id") -> list[str | None]:
        """Значения колонки текстом (::text) по порядку order_by в сессии COPY:
        так float, money и timestamptz печатаются одинаково у обеих таблиц."""
        query = (
            PgQueryBuilder(
                table=self._ident(),
                column=sql.Identifier(column),
                order_by=sql.Identifier(order_by),
            )
            .add("select {column}::text from {table} order by {order_by}")
            .build()
        )
        values: list[str | None] = []
        for row in await self._rows(query):
            if row[0] is None:
                values.append(None)
                continue

            values.append(str(row[0]))

        return values

    async def aggregate(self, expression: str) -> Any:
        """Одно агрегатное значение по всей таблице, выражение как написано."""
        query = (
            PgQueryBuilder(table=self._ident())
            .add("select")
            .raw_query(expression)
            .add("from {table}")
            .build()
        )
        rows = await self._rows(query)

        return rows[0][0]

    async def scalars(self, expression: str, order_by: str = "id") -> list[Any]:
        """Выражение по строкам таблицы в порядке order_by, как написано."""
        query = (
            PgQueryBuilder(table=self._ident(), order_by=sql.Identifier(order_by))
            .add("select")
            .raw_query(expression)
            .add("from {table} order by {order_by}")
            .build()
        )
        rows = await self._rows(query)

        return [row[0] for row in rows]

    def _ident(self) -> sql.Identifier:
        return sql.Identifier(self._schema, self._table)

    async def _rows(self, query: PgQuery) -> Sequence[Sequence[Any]]:
        profile = self._side.source.postgres.copy_text()
        async with await AsyncPostgresPool.dedicated(profile) as conn:
            cursor = await conn.execute(query.text, query.params)

            return await cursor.fetchall()
