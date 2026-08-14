"""Каталожные запросы postgres: композиция psycopg, значения — параметрами.

Фильтры приходят значениями и уезжают плейсхолдерами, поэтому склейки текста
с пользовательским вводом здесь нет: меняется только состав условий.
"""

from __future__ import annotations

from typing import Any, ClassVar

from psycopg import sql

from boba.toolkit.sql import CatalogQuery

__all__ = ["PgCatalog"]

PgCatalogQuery = CatalogQuery[tuple[Any, ...]]
"""Каталожный запрос postgres: текст плюс позиционные параметры psycopg."""


class PgCatalog:
    """Сборка запросов к pg_catalog: список отношений и описание колонок."""

    RELKINDS: ClassVar[tuple[str, ...]] = ("r", "p", "v", "m", "f")
    """Таблицы, партиционированные, view, матвью и сторонние — но не индексы."""

    TABLES_SELECT: ClassVar[sql.SQL] = sql.SQL("""
        select
            n.nspname                                     as schema,
            c.relname                                     as table_name,
            c.relkind                                     as kind,
            c.reltuples::bigint                           as approx_rows,
            pg_catalog.pg_get_userbyid(c.relowner)        as owner,
            pg_catalog.pg_total_relation_size(c.oid)      as total_bytes,
            pg_catalog.obj_description(c.oid, 'pg_class') as comment
        from
            pg_catalog.pg_class c
            join pg_catalog.pg_namespace n on n.oid = c.relnamespace
    """)
    TABLES_ORDER: ClassVar[sql.SQL] = sql.SQL("""
        order by
            n.nspname,
            c.relname
    """)

    COLUMNS_SELECT: ClassVar[sql.SQL] = sql.SQL("""
        select
            n.nspname                                        as schema,
            a.attnum                                         as position,
            a.attname                                        as column_name,
            pg_catalog.format_type(a.atttypid, a.atttypmod)  as type,
            not a.attnotnull                                 as nullable,
            pg_catalog.pg_get_expr(d.adbin, d.adrelid)       as default_expression,
            a.attidentity                                    as identity,
            a.attgenerated                                   as generated,
            COALESCE(i.indisprimary, false)                  as primary_key,
            pg_catalog.col_description(a.attrelid, a.attnum) as comment
        from
            pg_catalog.pg_attribute a
            join pg_catalog.pg_class c     on c.oid = a.attrelid
            join pg_catalog.pg_namespace n on n.oid = c.relnamespace
            left join pg_catalog.pg_attrdef d
                on d.adrelid = a.attrelid
                and d.adnum = a.attnum
            left join pg_catalog.pg_index i
                on i.indrelid = a.attrelid
                and i.indisprimary
                and a.attnum = ANY (i.indkey)
    """)
    COLUMNS_ORDER: ClassVar[sql.SQL] = sql.SQL("""
        order by
            n.nspname,
            a.attnum
    """)

    @classmethod
    def tables(cls, pg_schema: str | None, table_pattern: str | None) -> PgCatalogQuery:
        """Отношения подключения; пустые фильтры не сужают выдачу."""
        conditions: list[sql.Composable] = [sql.SQL("c.relkind = ANY (%s)")]
        params: list[Any] = [list(cls.RELKINDS)]

        if pg_schema:
            conditions.append(sql.SQL("n.nspname = %s"))
            params.append(pg_schema)

        if table_pattern:
            conditions.append(sql.SQL("c.relname like %s"))
            params.append(table_pattern)

        statement = cls._assemble(cls.TABLES_SELECT, conditions, cls.TABLES_ORDER)

        return PgCatalogQuery(text=statement.as_string(), params=tuple(params))

    @classmethod
    def columns(cls, table: str, pg_schema: str | None) -> PgCatalogQuery:
        """Колонки таблицы; без схемы ищется одноимённая во всех схемах."""
        conditions: list[sql.Composable] = [
            sql.SQL("c.relname = %s"),
            sql.SQL("a.attnum > 0"),
            sql.SQL("not a.attisdropped"),
        ]
        params: list[Any] = [table]

        if pg_schema:
            conditions.append(sql.SQL("n.nspname = %s"))
            params.append(pg_schema)

        statement = cls._assemble(cls.COLUMNS_SELECT, conditions, cls.COLUMNS_ORDER)

        return PgCatalogQuery(text=statement.as_string(), params=tuple(params))

    @classmethod
    def _assemble(
        cls,
        select: sql.SQL,
        conditions: list[sql.Composable],
        order: sql.SQL,
    ) -> sql.Composed:
        where = sql.SQL("\n            and ").join(conditions)
        return sql.SQL(" ").join([select, sql.SQL("where\n            "), where, order])
