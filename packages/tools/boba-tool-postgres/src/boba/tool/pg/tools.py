"""Postgres-инструменты: функции уровня модуля, модуль — обычная программа.

Запуск: `python -m boba.tool.pg.tools <имя> --флаги` — та же команда у
launcher'а приложения и у человека в терминале.

Ошибки:
PostgresError — до базы не достучаться (сеть, отказ libpq, kerberos).
UnknownConnectionError — имя подключения вне whitelist'а конфига.
psycopg.Error — сервер отклонил запрос (синтаксис, права).
ResultTooLargeError — дамп COPY превысил max_bytes конфига.
QueryBuildError — сборщик получил один параметр с двумя разными значениями.
"""

from __future__ import annotations

import codecs
import sys
from collections.abc import Mapping
from enum import StrEnum
from typing import Annotated, ClassVar, Final

import psycopg
from psycopg.rows import dict_row
from pydantic import Field

from boba.db.postgres import PayloadPostgres, PostgresError
from boba.db.postgres.address import PgAddresses
from boba.db.postgres.connection import PostgresConfig
from boba.db.postgres.query import PgQuery, PgQueryBuilder
from boba.toolkit.entry import ToolMain
from boba.toolkit.facade import Injected, UserConnection, tool
from boba.toolkit.ports import RawInbound, RawOutbound
from boba.toolkit.result import (
    MarkdownResult,
    ResultTooLargeError,
    SqlResult,
    SqlStatement,
    TableResult,
)
from boba.toolkit.sql import (
    QueryBuildError,
    SqlErrorKind,
    SqlLimits,
)
from boba.toolkit.types import SecretRevealing
from boba.toolkit.window import RowLimit, RowOffset, RowPage, RowWindow

PgConnection = Annotated[PostgresConfig, UserConnection]

SchemaFilter = Annotated[
    str,
    Field(min_length=1, description="Имя схемы. `*` — все схемы."),
]
"""LLM-аргумент schema_name: точное имя или `*`."""

PageOffset = Annotated[
    RowOffset, Field(description="Смещение страницы выдачи (0 — с начала).")
]
PageLimit = Annotated[RowLimit, Field(description="Потолок строк на страницу.")]


class AddressColumn(StrEnum):
    """Колонки выдачи pg_address."""

    CONNECTION = "connection"
    URL = "url"


class CopyDump:
    """Показ выгрузки COPY: чем разделены поля, знает только автор запроса.

    Дамп не разбирается — постгрес отдаёт его в формате, заданном самим
    стейтментом. Блок помечается csv: описание инструмента просит этот
    формат, а шапка блока — единственное, на что метка влияет.
    """

    LANG: ClassVar[str] = "csv"


class PgToolConfig(SecretRevealing, SqlLimits):
    """Лимиты выдачи pg-инструментов; [tool.pg]."""

    SECTION: ClassVar[str] = "tool.pg"
    ENGINE: ClassVar[str] = "postgres"
    """Подпись движка в SqlResult."""


async def run_and_collect(
    connection: PostgresConfig,
    query: PgQuery,
    window: RowWindow,
) -> SqlResult:
    """Запрос страницей окна: границы выдачи назначает вызов.

    Порядок строк задан самим запросом, поэтому окно повторяемо: тот же
    offset вернёт тот же кусок, пока каталог не изменился.
    """
    page = RowPage(window, skipped=0)

    conn = await PayloadPostgres.connect_config(connection)
    async with conn, conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(query.text, query.params)

        async for row in cur:
            if not page.add(row):
                break

    statement = SqlStatement(rows=page.rows, note=page.note())

    return SqlResult(engine=PgToolConfig.ENGINE, statements=[statement])


async def run_script(
    connection: PostgresConfig,
    script: str,
    window: RowWindow,
) -> SqlResult:
    """Произвольный текст пользователя: итог каждой его команды по порядку.

    Команд может быть несколько (`select ...; update ...;`): без параметров
    psycopg шлёт их простым протоколом, и postgres выполняет набор одной
    неявной транзакцией — падение любой команды откатывает всё. Выборка
    каждой команды режется тем же окном; команда без выборки отдаёт счётчик
    затронутых строк, где rowcount -1 у psycopg значит «счётчика нет».
    """
    statements: list[SqlStatement] = []

    conn = await PayloadPostgres.connect_config(connection)
    async with conn, conn.cursor(row_factory=dict_row) as cur:
        # bytes: тип Query psycopg требует LiteralString, а текст пишет LLM;
        # кодировка — client_encoding подключения, а не обязательно utf-8
        await cur.execute(script.encode(conn.info.encoding))

        while True:
            status = cur.statusmessage
            if status is None:
                status = ""

            if cur.description is None:
                rowcount: int | None = cur.rowcount
                if cur.rowcount < 0:
                    rowcount = None

                statements.append(SqlStatement(affected_rows=rowcount, status=status))
            else:
                page = RowPage(window, skipped=0)
                async for row in cur:
                    if not page.add(row):
                        break

                statements.append(
                    SqlStatement(rows=page.rows, note=page.note(), status=status)
                )

            if not cur.nextset():
                break

    return SqlResult(engine=PgToolConfig.ENGINE, statements=statements)


@tool
async def pg_list_tables(
    connection: PgConnection,
    pg_schema: Annotated[
        str | None,
        Field(
            description=(
                "Схема; пусто — все схемы, включая системные "
                "(pg_catalog, information_schema). Их много, и выдача "
                "упрётся в limit — сузьте фильтр."
            ),
        ),
    ] = None,
    table_pattern: Annotated[
        str | None,
        Field(
            description=(
                "Шаблон имени в синтаксисе LIKE: `kb_%`, `%log%`. "
                "Пусто — без фильтра по имени."
            ),
        ),
    ] = None,
    *,
    offset: RowOffset,
    limit: RowLimit,
) -> SqlResult:
    """Таблицы и view подключения из pg_catalog.

    Колонки: schema, table_name, kind, approx_rows, owner, total_bytes,
    comment. kind: r таблица, p партиционированная, v view,
    m материализованное view, f сторонняя таблица. Выдача постраничная:
    сколько показано и как листать дальше, сказано в note. Сложные условия
    по каталогу пишутся запросом к pg_catalog через pg_query.
    """
    builder = (
        PgQueryBuilder()
        .add(
            """
            select
                n.nspname                                     as schema,
                c.relname                                     as table_name,
                c.relkind                                     as kind,
                c.reltuples::bigint                           as approx_rows,
                pg_catalog.pg_get_userbyid(c.relowner)        as owner,
                pg_catalog.pg_total_relation_size(c.oid)      as total_bytes,
                pg_catalog.obj_description(c.oid, 'pg_class') as comment
            from pg_catalog.pg_class c
                join pg_catalog.pg_namespace n on n.oid = c.relnamespace
            where c.relkind = any(%(relkinds)s)
            """,
            relkinds=["r", "p", "v", "m", "f"],
        )
        .when(bool(pg_schema), "and n.nspname = %(pg_schema)s", pg_schema=pg_schema)
        .when(
            bool(table_pattern),
            "and c.relname like %(table_pattern)s",
            table_pattern=table_pattern,
        )
        .add("order by n.nspname, c.relname")
    )

    return await run_and_collect(
        connection, builder.build(), RowWindow(offset=offset, limit=limit)
    )


@tool
async def pg_describe_table(
    connection: PgConnection,
    table: Annotated[
        str,
        Field(min_length=1, description="Имя таблицы (без схемы)"),
    ],
    pg_schema: Annotated[
        str | None,
        Field(
            description=(
                "Схема таблицы; пусто — искать во всех схемах, "
                "схема каждой найденной видна колонкой schema."
            ),
        ),
    ] = None,
    *,
    offset: RowOffset,
    limit: RowLimit,
) -> SqlResult:
    """Схема таблицы из pg_catalog: колонки, нативные типы, ключи.

    Колонки: schema, position, column_name, type, nullable,
    default_expression, identity, generated, primary_key, comment. Широкая
    таблица приходит частями: как листать, сказано в note.
    """
    builder = (
        PgQueryBuilder()
        .add(
            """
            select
                n.nspname                                        as schema,
                a.attnum                                         as position,
                a.attname                                        as column_name,
                pg_catalog.format_type(a.atttypid, a.atttypmod)  as type,
                not a.attnotnull                                 as nullable,
                pg_catalog.pg_get_expr(d.adbin, d.adrelid)       as default_expression,
                a.attidentity                                    as identity,
                a.attgenerated                                   as generated,
                coalesce(i.indisprimary, false)                  as primary_key,
                pg_catalog.col_description(a.attrelid, a.attnum) as comment
            from pg_catalog.pg_attribute a
                join pg_catalog.pg_class c     on c.oid = a.attrelid
                join pg_catalog.pg_namespace n on n.oid = c.relnamespace
                left join pg_catalog.pg_attrdef d
                    on d.adrelid = a.attrelid and d.adnum = a.attnum
                left join pg_catalog.pg_index i
                    on i.indrelid = a.attrelid and i.indisprimary
                    and a.attnum = any(i.indkey)
            where c.relname = %(table)s
              and a.attnum > 0
              and not a.attisdropped
            """,
            table=table,
        )
        .when(bool(pg_schema), "and n.nspname = %(pg_schema)s", pg_schema=pg_schema)
        .add("order by n.nspname, a.attnum")
    )

    return await run_and_collect(
        connection, builder.build(), RowWindow(offset=offset, limit=limit)
    )


@tool
async def pg_query(
    connection: PgConnection,
    sql: Annotated[
        str,
        Field(
            min_length=1,
            description=(
                "Произвольный SQL. Запрос с выборкой возвращает строки окном "
                "offset/limit; INSERT/UPDATE/DELETE/DDL возвращают число "
                "затронутых строк и статус сервера. Команд может быть несколько "
                "через `;` — они идут одной транзакцией, и в ответ придёт итог "
                "каждой по порядку; падение любой откатывает весь набор."
            ),
        ),
        MarkdownResult(language="sql"),
    ],
    *,
    offset: RowOffset,
    limit: RowLimit,
) -> SqlResult:
    """Выполнить SQL на подключении: строки либо счётчик затронутых."""

    return await run_script(connection, sql, RowWindow(offset=offset, limit=limit))


@tool
async def pg_copy(
    connection: PgConnection,
    sql: Annotated[
        str,
        Field(
            min_length=1,
            description=(
                "Стейтмент COPY ... TO STDOUT целиком, например: "
                "COPY (select ...) TO STDOUT WITH (FORMAT CSV, HEADER). "
                "Выгружай форматом CSV — в таком виде вывод и показывается. "
                "Ответ возвращается текстом как есть. Если строк больше "
                "лимита — добавьте LIMIT в сам запрос."
            ),
        ),
        MarkdownResult(language="sql"),
    ],
    cfg: Annotated[PgToolConfig, Injected],
) -> MarkdownResult:
    """Выгрузить данные стейтментом COPY ... TO STDOUT как есть."""

    parts: list[str] = []
    size = 0

    conn = await PayloadPostgres.connect_config(connection)

    # bytes: тип Query psycopg требует LiteralString, а запрос пишет LLM;
    # кодировка — client_encoding подключения, а не обязательно utf-8
    statement = sql.encode(conn.info.encoding)

    # блоки COPY режут символ в произвольном месте — декодер инкрементальный
    decoder = codecs.getincrementaldecoder(conn.info.encoding)(errors="replace")

    async with conn, conn.cursor() as cur, cur.copy(statement) as copy_out:
        async for block in copy_out:
            data = bytes(block)

            size += len(data)
            if size > cfg.max_bytes:
                raise ResultTooLargeError.bytes_limit(cfg.max_bytes)

            text = decoder.decode(data)
            if text:
                parts.append(text)

    tail = decoder.decode(b"", True)
    if tail:
        parts.append(tail)

    return MarkdownResult(text="".join(parts), language=CopyDump.LANG)


@tool
async def pg_copy_out(
    connection: PgConnection,
    sql: Annotated[
        str,
        Field(
            min_length=1,
            description=(
                "Стейтмент COPY ... TO STDOUT целиком, например: "
                "COPY my_table TO STDOUT или "
                "COPY (select ...) TO STDOUT. Без WITH-опций постгрес "
                "отдаёт стандартный text-формат COPY — он же дефолт у "
                "принимающего COPY ... FROM STDIN, для перекачки pg->pg "
                "этого достаточно. Форматы обоих концов цепочки должны "
                "совпадать."
            ),
        ),
        MarkdownResult(language="sql"),
    ],
    out: Annotated[RawOutbound, Injected],
) -> MarkdownResult:
    """Насос выгрузки: COPY ... TO STDOUT сырым потоком в выходной порт.

    Узел графа workflow: данные идут следующему узлу, а не в чат.
    В ответ возвращается только счётчик перекачанных байтов.
    """
    total = 0

    conn = await PayloadPostgres.connect_config(connection)

    # bytes: тип Query psycopg требует LiteralString, а запрос пишет LLM
    statement = sql.encode(conn.info.encoding)

    async with conn, conn.cursor() as cur, cur.copy(statement) as copy_out:
        async for block in copy_out:
            data = bytes(block)
            total += len(data)
            await out.write(data)

    return MarkdownResult(text=f"copied out {total} bytes")


@tool
async def pg_copy_in(
    connection: PgConnection,
    sql: Annotated[
        str,
        Field(
            min_length=1,
            description=(
                "Стейтмент COPY ... FROM STDIN целиком, например: "
                "COPY my_table FROM STDIN. Без WITH-опций постгрес ждёт "
                "стандартный text-формат COPY. Формат обязан совпадать с "
                "тем, что отдаёт источник цепочки."
            ),
        ),
        MarkdownResult(language="sql"),
    ],
    feed: Annotated[RawInbound, Injected],
) -> MarkdownResult:
    """Насос загрузки: сырой поток входного порта в COPY ... FROM STDIN.

    Узел графа workflow: данные приходят от предыдущего узла.
    В ответ возвращается счётчик байтов и статус сервера (COPY N).
    """
    total = 0

    conn = await PayloadPostgres.connect_config(connection)
    statement = sql.encode(conn.info.encoding)

    async with conn, conn.cursor() as cur:
        async with cur.copy(statement) as copy_in:
            for chunk in feed:
                total += len(chunk)
                await copy_in.write(chunk)

        status = cur.statusmessage

    return MarkdownResult(text=f"copied in {total} bytes; server: {status}")


@tool
async def pg_database_describe(
    connection: PgConnection,
    db_name: Annotated[
        str,
        Field(
            min_length=1,
            description=(
                "Имя базы из pg_database. `*` — все базы кластера, доступные "
                "текущей роли. Конкретное имя — одна строка."
            ),
        ),
    ],
    *,
    offset: PageOffset,
    limit: PageLimit,
) -> SqlResult:
    """Описание баз данных кластера из pg_catalog.pg_database.

    Колонки: address (db), name, owner, encoding, collate, comment.
    Одна строка на базу. Комментарий виден только у своей базы, остальные
    возвращаются без comment. Выдача постраничная — как листать, сказано
    в note.
    """
    builder = (
        PgQueryBuilder()
        .add(
            """
            select
                d.datname                                          as address,
                d.datname                                          as name,
                pg_catalog.pg_get_userbyid(d.datdba)               as owner,
                pg_catalog.pg_encoding_to_char(d.encoding)         as encoding,
                d.datcollate                                       as collate,
                pg_catalog.shobj_description(d.oid, 'pg_database') as comment
            from pg_catalog.pg_database d
            where not d.datistemplate
            """
        )
        .when(db_name != "*", "and d.datname = %(db_name)s", db_name=db_name)
        .add("order by d.datname")
    )

    return await run_and_collect(
        connection, builder.build(), RowWindow(offset=offset, limit=limit)
    )


@tool
async def pg_schema_describe(
    connection: PgConnection,
    schema_name: Annotated[
        str,
        Field(
            min_length=1,
            description=(
                "Имя схемы из pg_namespace. `*` — все схемы, включая системные "
                "(pg_catalog, information_schema, pg_toast). Их много — сузьте "
                "фильтр или поднимите limit."
            ),
        ),
    ],
    *,
    offset: PageOffset,
    limit: PageLimit,
) -> SqlResult:
    """Описание схем текущей базы из pg_catalog.pg_namespace.

    Колонки: address (db.schema), database, name, owner, comment.
    Одна строка на схему. Порядок строк задан каталогом; окно повторяемо,
    пока каталог не изменился.
    """
    builder = (
        PgQueryBuilder()
        .add(
            """
            select
                concat_ws('.', current_database(), n.nspname)      as address,
                current_database()                                 as database,
                n.nspname                                          as name,
                pg_catalog.pg_get_userbyid(n.nspowner)             as owner,
                pg_catalog.obj_description(n.oid, 'pg_namespace')  as comment
            from pg_catalog.pg_namespace n
            where true
            """
        )
        .when(
            schema_name != "*",
            "and n.nspname = %(schema_name)s",
            schema_name=schema_name,
        )
        .add("order by n.nspname")
    )

    return await run_and_collect(
        connection, builder.build(), RowWindow(offset=offset, limit=limit)
    )


@tool
async def pg_table_describe(
    connection: PgConnection,
    schema_name: SchemaFilter,
    table_name: Annotated[
        str,
        Field(
            min_length=1,
            description=(
                "Имя отношения (таблица, view, матвью, партиция, сторонняя "
                "таблица). `*` — все отношения схемы."
            ),
        ),
    ],
    *,
    offset: PageOffset,
    limit: PageLimit,
) -> SqlResult:
    """Описание отношений из pg_class: таблицы, view, матвью, партиции,
    сторонние таблицы.

    Колонки: address, database, schema, name, kind (table/partitioned/
    partition/view/materialized/foreign), owner, comment, tablespace,
    persistence, row_estimate, total_bytes, partition_key, partition_of,
    partition_bound, definition, check_option, populated, foreign_server,
    options. Широкий результат — листайте окном, как сказано в note.
    """
    builder = (
        PgQueryBuilder()
        .add(
            """
            select
                concat_ws('.', current_database(), n.nspname, c.relname) as address,
                current_database()                                 as database,
                n.nspname                                          as schema,
                c.relname                                          as name,
                case c.relkind
                    when 'r' then
                        case when c.relispartition then 'partition' else 'table' end
                    when 'p' then 'partitioned'
                    when 'v' then 'view'
                    when 'm' then 'materialized'
                    when 'f' then 'foreign'
                end                                                as kind,
                pg_catalog.pg_get_userbyid(c.relowner)             as owner,
                pg_catalog.obj_description(c.oid, 'pg_class')      as comment,
                ts.spcname                                         as tablespace,
                case c.relpersistence
                    when 'p' then 'permanent'
                    when 'u' then 'unlogged'
                    when 't' then 'temporary'
                end                                                as persistence,
                greatest(c.reltuples, 0)::bigint                   as row_estimate,
                pg_catalog.pg_total_relation_size(c.oid)           as total_bytes,
                case when c.relkind = 'p'
                     then pg_catalog.pg_get_partkeydef(c.oid) end  as partition_key,
                case when parent.oid is not null
                     then pn.nspname || '.' || parent.relname end  as partition_of,
                pg_catalog.pg_get_expr(c.relpartbound, c.oid)      as partition_bound,
                case when c.relkind in ('v', 'm')
                     then pg_catalog.pg_get_viewdef(c.oid, true) end as definition,
                opts.options ->> 'check_option'                    as check_option,
                case when c.relkind = 'm' then c.relispopulated end as populated,
                fs.srvname                                         as foreign_server,
                opts.options                                       as options
            from pg_catalog.pg_class c
                join pg_catalog.pg_namespace n on n.oid = c.relnamespace
                left join pg_catalog.pg_tablespace ts on ts.oid = c.reltablespace
                left join pg_catalog.pg_inherits inh on inh.inhrelid = c.oid
                left join pg_catalog.pg_class parent
                    on parent.oid = inh.inhparent and c.relispartition
                left join pg_catalog.pg_namespace pn on pn.oid = parent.relnamespace
                left join pg_catalog.pg_foreign_table ft on ft.ftrelid = c.oid
                left join pg_catalog.pg_foreign_server fs on fs.oid = ft.ftserver
                cross join lateral (
                    select coalesce(
                        (select jsonb_object_agg(
                            split_part(o, '=', 1), substr(o, strpos(o, '=') + 1))
                         from unnest(c.reloptions) as o),
                        '{{}}'::jsonb) as options
                ) opts
            where c.relkind in ('r', 'p', 'v', 'm', 'f')
            """
        )
        .when(
            schema_name != "*",
            "and n.nspname = %(schema_name)s",
            schema_name=schema_name,
        )
        .when(
            table_name != "*",
            "and c.relname = %(table_name)s",
            table_name=table_name,
        )
        .add("order by n.nspname, c.relname")
    )

    return await run_and_collect(
        connection, builder.build(), RowWindow(offset=offset, limit=limit)
    )


@tool
async def pg_column_describe(
    connection: PgConnection,
    schema_name: SchemaFilter,
    table_name: Annotated[
        str,
        Field(
            min_length=1,
            description=(
                "Имя отношения (таблица/view/матвью/партиция). "
                "`*` — все отношения схемы (широкая выдача, "
                "сузьте через limit или возьмите конкретное имя)."
            ),
        ),
    ],
    *,
    offset: PageOffset,
    limit: PageLimit,
) -> SqlResult:
    """Колонки отношения из pg_attribute.

    Колонки: address, database, schema, relation, name, ordinal, type,
    nullable, default, identity (always/by default), generated (stored),
    collation, comment. Дропнутые атрибуты пропускаются. Для широкой
    таблицы выдача приходит частями — как листать, сказано в note.
    """
    builder = (
        PgQueryBuilder()
        .add(
            """
            select
                concat_ws('.', current_database(), n.nspname, c.relname,
                          a.attname)                               as address,
                current_database()                                 as database,
                n.nspname                                          as schema,
                c.relname                                          as relation,
                a.attname                                          as name,
                a.attnum                                           as ordinal,
                pg_catalog.format_type(a.atttypid, a.atttypmod)    as type,
                not a.attnotnull                                   as nullable,
                pg_catalog.pg_get_expr(d.adbin, d.adrelid)         as "default",
                case a.attidentity
                    when 'a' then 'always'
                    when 'd' then 'by default'
                end                                                as identity,
                case a.attgenerated when 's' then 'stored' end     as generated,
                case when a.attcollation <> t.typcollation
                    then co.collname end                           as collation,
                pg_catalog.col_description(a.attrelid, a.attnum)   as comment
            from pg_catalog.pg_attribute a
                join pg_catalog.pg_class c on c.oid = a.attrelid
                join pg_catalog.pg_namespace n on n.oid = c.relnamespace
                join pg_catalog.pg_type t on t.oid = a.atttypid
                left join pg_catalog.pg_attrdef d
                    on d.adrelid = a.attrelid and d.adnum = a.attnum
                left join pg_catalog.pg_collation co on co.oid = a.attcollation
            where c.relkind in ('r', 'p', 'v', 'm', 'f')
              and a.attnum > 0
              and not a.attisdropped
            """
        )
        .when(
            schema_name != "*",
            "and n.nspname = %(schema_name)s",
            schema_name=schema_name,
        )
        .when(
            table_name != "*",
            "and c.relname = %(table_name)s",
            table_name=table_name,
        )
        .add("order by n.nspname, c.relname, a.attnum")
    )

    return await run_and_collect(
        connection, builder.build(), RowWindow(offset=offset, limit=limit)
    )


@tool
async def pg_constraints_describe(
    connection: PgConnection,
    schema_name: SchemaFilter,
    table_name: Annotated[
        str,
        Field(min_length=1, description="Имя отношения. `*` — все отношения схемы."),
    ],
    *,
    offset: PageOffset,
    limit: PageLimit,
) -> SqlResult:
    """Ограничения отношений из pg_constraint: primary, unique, foreign,
    check, exclusion.

    Колонки: address, database, schema, relation, name, kind, columns,
    ref_schema, ref_relation, ref_columns, on_update, on_delete, deferrable,
    initially_deferred, definition, comment. Для FK ref_* заполнены, для
    остальных null. definition — канонический текст ограничения от сервера.
    """
    builder = (
        PgQueryBuilder()
        .add(
            """
            select
                concat_ws('.', current_database(), n.nspname, c.relname,
                          con.conname)                             as address,
                current_database()                                 as database,
                n.nspname                                          as schema,
                c.relname                                          as relation,
                con.conname                                        as name,
                case con.contype
                    when 'p' then 'primary'
                    when 'u' then 'unique'
                    when 'f' then 'foreign'
                    when 'c' then 'check'
                    when 'x' then 'exclusion'
                end                                                as kind,
                array(
                    select a.attname from pg_catalog.pg_attribute a
                    where a.attrelid = con.conrelid and a.attnum = any(con.conkey)
                    order by array_position(con.conkey, a.attnum)
                )                                                  as columns,
                rn.nspname                                         as ref_schema,
                rc.relname                                         as ref_relation,
                case when con.confrelid <> 0 then array(
                    select a.attname from pg_catalog.pg_attribute a
                    where a.attrelid = con.confrelid and a.attnum = any(con.confkey)
                    order by array_position(con.confkey, a.attnum)
                ) end                                              as ref_columns,
                case when con.contype = 'f' then
                    case con.confupdtype
                        when 'a' then 'no action'
                        when 'r' then 'restrict'
                        when 'c' then 'cascade'
                        when 'n' then 'set null'
                        when 'd' then 'set default'
                    end
                end                                                as on_update,
                case when con.contype = 'f' then
                    case con.confdeltype
                        when 'a' then 'no action'
                        when 'r' then 'restrict'
                        when 'c' then 'cascade'
                        when 'n' then 'set null'
                        when 'd' then 'set default'
                    end
                end                                                as on_delete,
                con.condeferrable                                  as deferrable,
                con.condeferred                                as initially_deferred,
                pg_catalog.pg_get_constraintdef(con.oid, true)     as definition,
                pg_catalog.obj_description(con.oid, 'pg_constraint') as comment
            from pg_catalog.pg_constraint con
                join pg_catalog.pg_class c on c.oid = con.conrelid
                join pg_catalog.pg_namespace n on n.oid = c.relnamespace
                left join pg_catalog.pg_class rc on rc.oid = con.confrelid
                left join pg_catalog.pg_namespace rn on rn.oid = rc.relnamespace
            where c.relkind in ('r', 'p', 'v', 'm', 'f')
              and con.contype in ('p', 'u', 'f', 'c', 'x')
            """
        )
        .when(
            schema_name != "*",
            "and n.nspname = %(schema_name)s",
            schema_name=schema_name,
        )
        .when(
            table_name != "*",
            "and c.relname = %(table_name)s",
            table_name=table_name,
        )
        .add("order by n.nspname, c.relname, con.conname")
    )

    return await run_and_collect(
        connection, builder.build(), RowWindow(offset=offset, limit=limit)
    )


@tool
async def pg_indexes_describe(
    connection: PgConnection,
    schema_name: SchemaFilter,
    table_name: Annotated[
        str,
        Field(min_length=1, description="Имя отношения. `*` — все отношения схемы."),
    ],
    *,
    offset: PageOffset,
    limit: PageLimit,
) -> SqlResult:
    """Индексы отношений из pg_index.

    Колонки: address, database, schema, relation, name, method (btree/hash/
    gin/gist/…), unique, primary, columns (выражения/колонки индекса),
    predicate (частичный индекс), definition (полный CREATE INDEX),
    total_bytes, comment. Уникальные и первичные ключи помечены флагами;
    детали ограничения — в pg_constraints_describe.
    """
    builder = (
        PgQueryBuilder()
        .add(
            """
            select
                concat_ws('.', current_database(), n.nspname, c.relname,
                          ic.relname)                              as address,
                current_database()                                 as database,
                n.nspname                                          as schema,
                c.relname                                          as relation,
                ic.relname                                         as name,
                am.amname                                          as method,
                i.indisunique                                      as unique,
                i.indisprimary                                     as primary,
                array(
                    select pg_catalog.pg_get_indexdef(i.indexrelid, k.n, true)
                    from generate_series(1, i.indnkeyatts) as k(n)
                )                                                  as columns,
                pg_catalog.pg_get_expr(i.indpred, i.indrelid, true) as predicate,
                pg_catalog.pg_get_indexdef(i.indexrelid)           as definition,
                pg_catalog.pg_relation_size(i.indexrelid)          as total_bytes,
                pg_catalog.obj_description(i.indexrelid, 'pg_class') as comment
            from pg_catalog.pg_index i
                join pg_catalog.pg_class c on c.oid = i.indrelid
                join pg_catalog.pg_namespace n on n.oid = c.relnamespace
                join pg_catalog.pg_class ic on ic.oid = i.indexrelid
                join pg_catalog.pg_am am on am.oid = ic.relam
            where c.relkind in ('r', 'p', 'v', 'm', 'f')
            """
        )
        .when(
            schema_name != "*",
            "and n.nspname = %(schema_name)s",
            schema_name=schema_name,
        )
        .when(
            table_name != "*",
            "and c.relname = %(table_name)s",
            table_name=table_name,
        )
        .add("order by n.nspname, c.relname, ic.relname")
    )

    return await run_and_collect(
        connection, builder.build(), RowWindow(offset=offset, limit=limit)
    )


@tool
async def pg_routines_describe(
    connection: PgConnection,
    schema_name: Annotated[
        str,
        Field(
            min_length=1,
            description="Имя схемы. `*` — все схемы, включая системные функции.",
        ),
    ],
    routine_name: Annotated[
        str,
        Field(min_length=1, description="Имя рутины. `*` — все рутины схемы."),
    ],
    *,
    offset: PageOffset,
    limit: PageLimit,
) -> SqlResult:
    """Рутины из pg_proc: функции, процедуры, агрегаты, оконные функции.

    Колонки: address, database, schema, name, signature, kind (function/
    procedure/aggregate/window), owner, language, arguments, returns,
    returns_set, volatility, strict, security_definer, parallel, cost, rows,
    body, definition (полный CREATE OR REPLACE, у агрегатов пусто), comment.
    Одна строка на перегрузку; различайте по signature.
    """
    builder = (
        PgQueryBuilder()
        .add(
            """
            select
                concat_ws('.', current_database(), n.nspname, p.proname) as address,
                current_database()                                 as database,
                n.nspname                                          as schema,
                p.proname                                          as name,
                pg_catalog.oidvectortypes(p.proargtypes)           as signature,
                case p.prokind
                    when 'f' then 'function'
                    when 'p' then 'procedure'
                    when 'a' then 'aggregate'
                    when 'w' then 'window'
                end                                                as kind,
                pg_catalog.pg_get_userbyid(p.proowner)             as owner,
                l.lanname                                          as language,
                pg_catalog.pg_get_function_arguments(p.oid)        as arguments,
                case when p.prokind <> 'p'
                     then pg_catalog.pg_get_function_result(p.oid) end as returns,
                p.proretset                                        as returns_set,
                case p.provolatile
                    when 'i' then 'immutable'
                    when 's' then 'stable'
                    when 'v' then 'volatile'
                end                                                as volatility,
                p.proisstrict                                      as strict,
                p.prosecdef                                        as security_definer,
                case p.proparallel
                    when 's' then 'safe'
                    when 'r' then 'restricted'
                    when 'u' then 'unsafe'
                end                                                as parallel,
                p.procost                                          as cost,
                case when p.proretset then p.prorows end           as rows,
                coalesce(p.prosrc, '')                             as body,
                case when p.prokind <> 'a'
                     then coalesce(pg_catalog.pg_get_functiondef(p.oid), '')
                     else '' end                                   as definition,
                pg_catalog.obj_description(p.oid, 'pg_proc')       as comment
            from pg_catalog.pg_proc p
                join pg_catalog.pg_namespace n on n.oid = p.pronamespace
                join pg_catalog.pg_language l on l.oid = p.prolang
            where true
            """
        )
        .when(
            schema_name != "*",
            "and n.nspname = %(schema_name)s",
            schema_name=schema_name,
        )
        .when(
            routine_name != "*",
            "and p.proname = %(routine_name)s",
            routine_name=routine_name,
        )
        .add("order by n.nspname, p.proname, p.oid")
    )

    return await run_and_collect(
        connection, builder.build(), RowWindow(offset=offset, limit=limit)
    )


@tool
async def pg_routine_arg_describe(
    connection: PgConnection,
    schema_name: SchemaFilter,
    routine_name: Annotated[
        str,
        Field(min_length=1, description="Имя рутины. `*` — все рутины схемы."),
    ],
    *,
    offset: PageOffset,
    limit: PageLimit,
) -> SqlResult:
    """Аргументы рутин из pg_proc (развёртка proallargtypes).

    Колонки: address, database, schema, routine, signature (для привязки
    к pg_routines_describe), position (0-базный), name, type, mode
    (in/out/inout/variadic/table). Одна строка на аргумент, перегрузки
    различаются по signature.
    """
    builder = (
        PgQueryBuilder()
        .add(
            """
            select
                concat_ws('.', current_database(), n.nspname, p.proname,
                          nullif(p.proargnames[u.n], ''))          as address,
                current_database()                                 as database,
                n.nspname                                          as schema,
                p.proname                                          as routine,
                pg_catalog.oidvectortypes(p.proargtypes)           as signature,
                u.n - 1                                            as position,
                nullif(p.proargnames[u.n], '')                     as name,
                pg_catalog.format_type(u.t, null)                  as type,
                case coalesce(p.proargmodes[u.n], 'i')
                    when 'i' then 'in'
                    when 'o' then 'out'
                    when 'b' then 'inout'
                    when 'v' then 'variadic'
                    when 't' then 'table'
                end                                                as mode
            from pg_catalog.pg_proc p
                join pg_catalog.pg_namespace n on n.oid = p.pronamespace
                cross join lateral
                    unnest(coalesce(p.proallargtypes, p.proargtypes::oid[]))
                    with ordinality as u(t, n)
            where true
            """
        )
        .when(
            schema_name != "*",
            "and n.nspname = %(schema_name)s",
            schema_name=schema_name,
        )
        .when(
            routine_name != "*",
            "and p.proname = %(routine_name)s",
            routine_name=routine_name,
        )
        .add("order by n.nspname, p.proname, p.oid, u.n")
    )

    return await run_and_collect(
        connection, builder.build(), RowWindow(offset=offset, limit=limit)
    )


@tool
async def pg_sequences_describe(
    connection: PgConnection,
    schema_name: SchemaFilter,
    sequence_name: Annotated[
        str,
        Field(min_length=1, description="Имя последовательности. `*` — все в схеме."),
    ],
    *,
    offset: PageOffset,
    limit: PageLimit,
) -> SqlResult:
    """Последовательности из pg_class/pg_sequence.

    Колонки: address, database, schema, name, type (bigint/integer/…),
    start, minimum, maximum, increment, cycle, cache, last_value (null,
    если ещё не вызывалась или нет SELECT-прав), owned_by
    (schema.table.column для SERIAL/IDENTITY), comment.
    """
    builder = (
        PgQueryBuilder()
        .add(
            """
            select
                concat_ws('.', current_database(), n.nspname, c.relname) as address,
                current_database()                                 as database,
                n.nspname                                          as schema,
                c.relname                                          as name,
                pg_catalog.format_type(s.seqtypid, null)           as type,
                s.seqstart                                         as start,
                s.seqmin                                           as minimum,
                s.seqmax                                           as maximum,
                s.seqincrement                                     as increment,
                s.seqcycle                                         as cycle,
                s.seqcache                                         as cache,
                (select ps.last_value from pg_catalog.pg_sequences ps
                    where ps.schemaname = n.nspname
                      and ps.sequencename = c.relname)             as last_value,
                (select on_.nspname || '.' || oc.relname || '.' || oa.attname
                    from pg_catalog.pg_depend dep
                    join pg_catalog.pg_class oc on oc.oid = dep.refobjid
                    join pg_catalog.pg_namespace on_ on on_.oid = oc.relnamespace
                    join pg_catalog.pg_attribute oa
                        on oa.attrelid = dep.refobjid and oa.attnum = dep.refobjsubid
                    where dep.objid = c.oid and dep.deptype = 'a'
                      and dep.classid = 'pg_class'::regclass
                    limit 1)                                       as owned_by,
                pg_catalog.obj_description(c.oid, 'pg_class')      as comment
            from pg_catalog.pg_class c
                join pg_catalog.pg_namespace n on n.oid = c.relnamespace
                join pg_catalog.pg_sequence s on s.seqrelid = c.oid
            where c.relkind = 'S'
            """
        )
        .when(
            schema_name != "*",
            "and n.nspname = %(schema_name)s",
            schema_name=schema_name,
        )
        .when(
            sequence_name != "*",
            "and c.relname = %(sequence_name)s",
            sequence_name=sequence_name,
        )
        .add("order by n.nspname, c.relname")
    )

    return await run_and_collect(
        connection, builder.build(), RowWindow(offset=offset, limit=limit)
    )


@tool
async def pg_types_describe(
    connection: PgConnection,
    schema_name: SchemaFilter,
    type_name: Annotated[
        str,
        Field(
            min_length=1, description="Имя типа. `*` — все пользовательские типы схемы."
        ),
    ],
    *,
    offset: PageOffset,
    limit: PageLimit,
) -> SqlResult:
    """Пользовательские типы из pg_type: enum, domain, composite, range.

    Колонки: address, database, schema, name, kind, owner, labels (значения
    enum), base_type и constraint (для domain), attributes (поля composite
    как JSON), comment.
    """
    builder = (
        PgQueryBuilder()
        .add(
            """
            select
                concat_ws('.', current_database(), n.nspname, t.typname) as address,
                current_database()                                 as database,
                n.nspname                                          as schema,
                t.typname                                          as name,
                case t.typtype
                    when 'e' then 'enum'
                    when 'd' then 'domain'
                    when 'c' then 'composite'
                    when 'r' then 'range'
                end                                                as kind,
                pg_catalog.pg_get_userbyid(t.typowner)             as owner,
                case when t.typtype = 'e' then array(
                    select e.enumlabel from pg_catalog.pg_enum e
                    where e.enumtypid = t.oid order by e.enumsortorder
                ) end                                              as labels,
                case when t.typtype = 'd'
                     then pg_catalog.format_type(t.typbasetype, t.typtypmod) end
                                                                   as base_type,
                case when t.typtype = 'd' then (
                    select string_agg(
                        pg_catalog.pg_get_constraintdef(dc.oid, true), ' ')
                    from pg_catalog.pg_constraint dc where dc.contypid = t.oid
                ) end                                              as constraint,
                case when t.typtype = 'c' then coalesce((
                    select jsonb_agg(
                        jsonb_build_object(
                            'name', a.attname,
                            'type', pg_catalog.format_type(a.atttypid, a.atttypmod))
                        order by a.attnum)
                    from pg_catalog.pg_attribute a
                    where a.attrelid = t.typrelid
                      and a.attnum > 0 and not a.attisdropped
                ), '[]'::jsonb) end                                as attributes,
                pg_catalog.obj_description(t.oid, 'pg_type')       as comment
            from pg_catalog.pg_type t
                join pg_catalog.pg_namespace n on n.oid = t.typnamespace
                left join pg_catalog.pg_class c on c.oid = t.typrelid
            where t.typtype in ('e', 'd', 'c', 'r')
              and (t.typtype <> 'c' or c.relkind = 'c')
            """
        )
        .when(
            schema_name != "*",
            "and n.nspname = %(schema_name)s",
            schema_name=schema_name,
        )
        .when(
            type_name != "*",
            "and t.typname = %(type_name)s",
            type_name=type_name,
        )
        .add("order by n.nspname, t.typname")
    )

    return await run_and_collect(
        connection, builder.build(), RowWindow(offset=offset, limit=limit)
    )


@tool
async def pg_address(connection: PgConnection) -> TableResult:
    """Базовый url соединения PostgreSQL: postgresql://host:port/database.

    Ничего не выполняет в базе. Объект адресуется ролями в query поверх
    этого url: ?schema=dm&table=fact_orders, ?schema=dm&table=t&column=c.
    """
    base = PgAddresses.base_of(connection)
    row = {
        AddressColumn.CONNECTION.value: connection.source.name,
        AddressColumn.URL.value: base.render(),
    }

    return TableResult(rows=[row])


EXPECTED: Mapping[type[Exception], SqlErrorKind] = {
    QueryBuildError: SqlErrorKind.SQL_FAILED,
    PostgresError: SqlErrorKind.DATABASE_UNAVAILABLE,
    psycopg.Error: SqlErrorKind.SQL_FAILED,
    ResultTooLargeError: SqlErrorKind.RESULT_TOO_LARGE,
}

TOOLS: Final = ToolMain.toolset(
    pg_list_tables,
    pg_describe_table,
    pg_query,
    pg_copy,
    pg_copy_out,
    pg_copy_in,
    pg_address,
    pg_database_describe,
    pg_schema_describe,
    pg_table_describe,
    pg_column_describe,
    pg_constraints_describe,
    pg_indexes_describe,
    pg_routines_describe,
    pg_routine_arg_describe,
    pg_sequences_describe,
    pg_types_describe,
)

if __name__ == "__main__":
    sys.exit(ToolMain.run(TOOLS))
