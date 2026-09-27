# ruff: noqa: S608 — стейтменты стенда собираются из имён его схем и таблиц
"""Таблица-приёмник на стороне postgres стенда: колонки из каталога, список
таблиц схемы, строки и текст значений для проверок приёмника."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from psycopg import sql

from boba.db.clickhouse.payload import PayloadClickHouse
from boba.db.oracle.payload import PayloadOracle
from boba.db.postgres import AsyncPostgresPool, PgQuery, PgQueryBuilder
from boba.pump_stand.oracle import PumpUser
from boba.pump_stand.sides import ClickHouseSide, OracleSide, PostgresSide

__all__ = ["ChLoaded", "Loaded", "OraLoaded"]


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


class ChLoaded:
    """Таблица database.table на ClickHouse стенда: типы колонок из каталога,
    движок и ключ сортировки, число строк, значения выражений."""

    def __init__(self, side: ClickHouseSide, table: str) -> None:
        self._side = side
        self._table = table

    async def types(self) -> list[tuple[str, str]]:
        rows = await self._rows(
            "select name, type from system.columns "
            f"where database = '{self._side.database}' and table = '{self._table}' "
            "order by position"
        )

        return [(str(row[0]), str(row[1])) for row in rows]

    async def sorting_key(self) -> str:
        rows = await self._rows(
            "select sorting_key from system.tables "
            f"where database = '{self._side.database}' and name = '{self._table}'"
        )

        return str(rows[0][0])

    async def engine(self) -> str:
        rows = await self._rows(
            "select engine from system.tables "
            f"where database = '{self._side.database}' and name = '{self._table}'"
        )

        return str(rows[0][0])

    async def count(self) -> int:
        return int(await self.scalar("count()"))

    async def scalar(self, expression: str) -> Any:
        rows = await self._rows(
            f"select {expression} from {self._side.database}.{self._table}"
        )

        return rows[0][0]

    async def column(self, expression: str, order_by: str = "id") -> list[Any]:
        rows = await self._rows(
            f"select {expression} from {self._side.database}.{self._table} "
            f"order by {order_by}"
        )

        return [row[0] for row in rows]

    async def clusters(self) -> list[str]:
        rows = await self._rows("select distinct cluster from system.clusters")

        return [str(row[0]) for row in rows]

    async def tables(self) -> list[str]:
        rows = await self._rows(
            f"select name from system.tables where database = '{self._side.database}' "
            "order by name"
        )

        return [str(row[0]) for row in rows]

    async def _rows(self, text: str) -> list[Sequence[Any]]:
        async with PayloadClickHouse.opened_config(self._side.profile) as client:
            result = await client.query(text)

        return list(result.result_rows)


class OraLoaded:
    """Таблица схемы PUMP_STAND на Oracle стенда: колонки из all_tab_columns
    текстом типа как в DDL, число строк, значения выражений по порядку."""

    def __init__(self, side: OracleSide, table: str) -> None:
        self._side = side
        self._table = table

    async def columns(self) -> list[tuple[str, str, bool]]:
        """(имя строчными, текст типа, not null) по column_id."""
        rows = await self._rows(
            "select column_name, data_type, data_length, char_length, char_used, "
            "data_precision, data_scale, nullable "
            "from all_tab_columns where owner = :owner and table_name = :name "
            "order by column_id",
            {"owner": PumpUser.NAME.value, "name": self._table.upper()},
        )
        columns: list[tuple[str, str, bool]] = []
        for row in rows:
            columns.append((str(row[0]).lower(), self._type_text(row), row[7] == "N"))

        return columns

    async def tables(self) -> list[str]:
        rows = await self._rows(
            "select table_name from all_tables where owner = :owner "
            "order by table_name",
            {"owner": PumpUser.NAME.value},
        )

        return [str(row[0]).lower() for row in rows]

    async def count(self) -> int:
        rows = await self._rows(
            f"select count(*) from {PumpUser.NAME}.{self._table}", {}
        )

        return int(rows[0][0])

    async def scalar(self, expression: str) -> Any:
        rows = await self._rows(
            f"select {expression} from {PumpUser.NAME}.{self._table}", {}
        )

        return rows[0][0]

    async def column(self, expression: str, order_by: str = "id") -> list[Any]:
        rows = await self._rows(
            f"select {expression} from {PumpUser.NAME}.{self._table} "
            f"order by {order_by}",
            {},
        )

        return [row[0] for row in rows]

    @staticmethod
    def _type_text(row: Sequence[Any]) -> str:
        """NUMBER(10,2), VARCHAR2(20 CHAR), TIMESTAMP(6), CLOB — как в DDL."""
        data_type = str(row[1])
        if data_type == "NUMBER":
            if row[5] is None:
                return data_type

            return f"NUMBER({int(row[5])},{int(row[6] or 0)})"

        if data_type == "FLOAT":
            return f"FLOAT({int(row[5])})"

        if data_type in ("VARCHAR2", "CHAR", "NVARCHAR2", "NCHAR"):
            semantic = ""
            if row[4] == "C" and data_type in ("VARCHAR2", "CHAR"):
                semantic = " CHAR"

            return f"{data_type}({int(row[3])}{semantic})"

        if data_type == "RAW":
            return f"RAW({int(row[2])})"

        return data_type

    async def _rows(
        self, text: str, parameters: Mapping[str, object]
    ) -> list[Sequence[Any]]:
        payload = PayloadOracle(self._side.stand.owner)
        async with (
            payload.opened() as conn,
            payload.rows(conn, text, parameters) as stream,
        ):
            return [row async for row in stream.blocks]
