"""Каталожные запросы postgres: композиция psycopg, значения — параметрами.

Фильтры приходят значениями и уезжают плейсхолдерами, поэтому склейки текста
с пользовательским вводом здесь нет: меняется только состав условий.
"""

from __future__ import annotations

from typing import Any, ClassVar

from psycopg import sql

__all__ = ["PgCatalog", "PgCatalogQuery"]


class PgCatalogQuery:
    """Готовый каталожный запрос: текст для payload'а и его параметры."""

    def __init__(self, statement: sql.Composed, params: tuple[Any, ...]) -> None:
        self._statement = statement
        self._params = params

    @property
    def text(self) -> str:
        return self._statement.as_string()

    @property
    def params(self) -> tuple[Any, ...]:
        return self._params


class PgCatalog:
    """Сборка запросов к pg_catalog: список отношений и описание колонок."""

    RELKINDS: ClassVar[tuple[str, ...]] = ("r", "p", "v", "m", "f")
    """Таблицы, партиционированные, view, матвью и сторонние — но не индексы."""

    TABLES_SELECT: ClassVar[sql.SQL] = sql.SQL("""
        SELECT n.nspname                                     AS schema,
               c.relname                                     AS table_name,
               c.relkind                                     AS kind,
               c.reltuples::bigint                           AS approx_rows,
               pg_catalog.pg_get_userbyid(c.relowner)        AS owner,
               pg_catalog.pg_total_relation_size(c.oid)      AS total_bytes,
               pg_catalog.obj_description(c.oid, 'pg_class') AS comment
        FROM pg_catalog.pg_class c
        JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
    """)
    TABLES_ORDER: ClassVar[sql.SQL] = sql.SQL("ORDER BY n.nspname, c.relname")

    COLUMNS_SELECT: ClassVar[sql.SQL] = sql.SQL("""
        SELECT n.nspname                                        AS schema,
               a.attnum                                         AS position,
               a.attname                                        AS column_name,
               pg_catalog.format_type(a.atttypid, a.atttypmod)  AS type,
               NOT a.attnotnull                                 AS nullable,
               pg_catalog.pg_get_expr(d.adbin, d.adrelid)       AS default_expression,
               a.attidentity                                    AS identity,
               a.attgenerated                                   AS generated,
               COALESCE(i.indisprimary, false)                  AS primary_key,
               pg_catalog.col_description(a.attrelid, a.attnum) AS comment
        FROM pg_catalog.pg_attribute a
        JOIN pg_catalog.pg_class c     ON c.oid = a.attrelid
        JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
        LEFT JOIN pg_catalog.pg_attrdef d
               ON d.adrelid = a.attrelid AND d.adnum = a.attnum
        LEFT JOIN pg_catalog.pg_index i
               ON i.indrelid = a.attrelid AND i.indisprimary
              AND a.attnum = ANY (i.indkey)
    """)
    COLUMNS_ORDER: ClassVar[sql.SQL] = sql.SQL("ORDER BY n.nspname, a.attnum")

    @classmethod
    def tables(cls, pg_schema: str | None, table_pattern: str | None) -> PgCatalogQuery:
        """Отношения подключения; пустые фильтры не сужают выдачу."""
        conditions: list[sql.Composable] = [sql.SQL("c.relkind = ANY (%s)")]
        params: list[Any] = [list(cls.RELKINDS)]

        if pg_schema:
            conditions.append(sql.SQL("n.nspname = %s"))
            params.append(pg_schema)

        if table_pattern:
            conditions.append(sql.SQL("c.relname LIKE %s"))
            params.append(table_pattern)

        statement = cls._assemble(cls.TABLES_SELECT, conditions, cls.TABLES_ORDER)
        return PgCatalogQuery(statement, tuple(params))

    @classmethod
    def columns(cls, table: str, pg_schema: str | None) -> PgCatalogQuery:
        """Колонки таблицы; без схемы ищется одноимённая во всех схемах."""
        conditions: list[sql.Composable] = [
            sql.SQL("c.relname = %s"),
            sql.SQL("a.attnum > 0"),
            sql.SQL("NOT a.attisdropped"),
        ]
        params: list[Any] = [table]

        if pg_schema:
            conditions.append(sql.SQL("n.nspname = %s"))
            params.append(pg_schema)

        statement = cls._assemble(cls.COLUMNS_SELECT, conditions, cls.COLUMNS_ORDER)
        return PgCatalogQuery(statement, tuple(params))

    @classmethod
    def _assemble(
        cls,
        select: sql.SQL,
        conditions: list[sql.Composable],
        order: sql.SQL,
    ) -> sql.Composed:
        where = sql.SQL(" AND ").join(conditions)
        return sql.SQL(" ").join([select, sql.SQL("WHERE"), where, order])
