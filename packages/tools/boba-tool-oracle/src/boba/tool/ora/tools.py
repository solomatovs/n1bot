"""Oracle-инструменты: функции уровня модуля, модуль — обычная программа.

Запуск: `python -m boba.tool.ora.tools <имя> --флаги` — та же команда у
launcher'а приложения и у человека в терминале. Словарь читается через
представления all_*: видно то, на что у учётки есть права, грантов сверх
`create session` не нужно.

Ошибки:
OracleError — до базы не достучаться (сеть, listener, вход).
OracleQueryError — сервер отклонил запрос (синтаксис, права) или оборвал чтение.
UnknownConnectionError — имя подключения вне whitelist'а конфига.
AddressError — адрес базы не собрался из профиля соединения.
QueryBuildError — сборщик получил один параметр с двумя разными значениями
    или имя таблицы/колонки для ora_csv_in пустое или с кавычкой внутри.
ArrowStreamError — вход ora_arrow_in не читается как поток Arrow IPC.
"""

from __future__ import annotations

import codecs
import csv
import sys
from collections.abc import Iterator, Mapping, Sequence
from enum import StrEnum
from typing import Annotated, ClassVar, Final

from pydantic import Field

from boba.connections.address import AddressError
from boba.db.oracle import (
    OracleError,
    OracleQueryError,
    OraLiterals,
    OraQuery,
    OraQueryBuilder,
)
from boba.db.oracle.address import OraAddresses
from boba.db.oracle.connection import OracleConfig
from boba.toolkit.entry import ToolMain
from boba.toolkit.facade import Injected, UserConnection, tool
from boba.toolkit.ports import (
    ArrowInbound,
    ArrowOutbound,
    ArrowStreamError,
    ChunkBytes,
    RawInbound,
    RawOutbound,
)
from boba.toolkit.result import MarkdownResult, SqlResult, SqlStatement, TableResult
from boba.toolkit.sql import QueryBuildError, SqlErrorKind, SqlLimits
from boba.toolkit.types import SecretRevealing
from boba.toolkit.window import RowLimit, RowOffset, RowPage, RowWindow

OraConnection = Annotated[OracleConfig, UserConnection]

SchemaFilter = Annotated[
    str,
    Field(
        min_length=1,
        description=(
            "Имя схемы (владельца) заглавными, например `HR`. `*` — все схемы, "
            "кроме служебных схем Oracle."
        ),
    ),
]
"""LLM-аргумент schema_name: точное имя или `*`."""

TableFilter = Annotated[
    str,
    Field(min_length=1, description="Имя таблицы заглавными. `*` — все таблицы схемы."),
]
"""LLM-аргумент table: точное имя или `*`."""


BeforeSteps = Annotated[
    Sequence[str],
    Field(
        description=(
            "Стейтменты, которые выполняются по порядку перед командой насоса "
            "в той же сессии: alter session, delete из staging, проверки. "
            "Каждый элемент — одна команда без `;` в конце либо один анонимный "
            "блок PL/SQL. DML остаётся в транзакции насоса до общего commit "
            "после after; DDL (truncate, exchange partition) Oracle фиксирует "
            "сам. Строки выборок не возвращаются, в ответ идёт число "
            "затронутых строк каждого шага. Проверка, которая должна "
            "остановить насос, пишется блоком: begin if ... then "
            "raise_application_error(-20001, '...'); end if; end;"
        ),
    ),
]
AfterSteps = Annotated[
    Sequence[str],
    Field(
        description=(
            "Стейтменты, которые выполняются по порядку после команды насоса в "
            "той же сессии, затем один commit: insert ... select из "
            "временной или staging-таблицы, merge, exchange partition. "
            "Ошибка шага до commit откатывает DML вызова, включая загруженные "
            "строки; DDL уже зафиксирован. В ответ идёт число затронутых "
            "строк каждого шага."
        ),
    ),
]


class AddressColumn(StrEnum):
    """Колонки выдачи ora_address."""

    CONNECTION = "connection"
    URL = "url"


class ObjectKind(StrEnum):
    """Виды объектов словаря, которые показывают инструменты."""

    TABLE = "TABLE"
    VIEW = "VIEW"
    MATERIALIZED_VIEW = "MATERIALIZED VIEW"
    PROCEDURE = "PROCEDURE"
    FUNCTION = "FUNCTION"
    PACKAGE = "PACKAGE"
    TYPE = "TYPE"

    @classmethod
    def relations(cls) -> OraLiterals:
        return OraLiterals((cls.TABLE, cls.VIEW, cls.MATERIALIZED_VIEW))

    @classmethod
    def routines(cls) -> OraLiterals:
        return OraLiterals((cls.PROCEDURE, cls.FUNCTION, cls.PACKAGE, cls.TYPE))


class CsvContract(StrEnum):
    """Формат потока между насосами: CSV без заголовка, NULL как `\\N`, бинарное
    поле шестнадцатеричной строкой с префиксом `\\x` (как bytea у postgres)."""

    NULL = "\\N"
    HEX_PREFIX = "\\x"
    ENCODING = "utf-8"
    LINE_END = "\n"


class OraToolConfig(SecretRevealing, SqlLimits):
    """Лимиты выдачи ora-инструментов; [tool.ora]."""

    SECTION: ClassVar[str] = "tool.ora"
    ENGINE: ClassVar[str] = "oracle"
    """Подпись движка в SqlResult."""


async def run_and_collect(
    connection: OracleConfig,
    query: OraQuery,
    window: RowWindow,
) -> SqlResult:
    """Выборка страницей окна: границы выдачи назначает вызов."""
    page = RowPage(window, skipped=0)

    from boba.db.oracle.payload import PayloadOracle  # noqa: PLC0415

    payload = PayloadOracle(connection)
    async with (
        payload.opened() as conn,
        payload.rows(conn, query.text, query.params) as stream,
    ):
        async for block in stream.blocks:
            if not page.add(dict(zip(stream.names, block, strict=True))):
                break

    statement = SqlStatement(rows=page.rows, note=page.note())

    return SqlResult(engine=OraToolConfig.ENGINE, statements=[statement])


async def run_statement(
    connection: OracleConfig,
    text: str,
    window: RowWindow,
) -> SqlResult:
    """Произвольная команда пользователя: выборка окном либо счётчик затронутых
    строк. Команда одна: Oracle не принимает несколько через `;` одним вызовом.
    DML фиксируется сразу: соединение живёт только этот вызов."""
    from boba.db.oracle.payload import PayloadOracle  # noqa: PLC0415

    payload = PayloadOracle(connection)
    async with payload.opened() as conn:
        async with payload.rows(conn, text) as stream:
            if stream.names:
                page = RowPage(window, skipped=0)
                async for block in stream.blocks:
                    if not page.add(dict(zip(stream.names, block, strict=True))):
                        break

                statement = SqlStatement(rows=page.rows, note=page.note())
            else:
                statement = SqlStatement(affected_rows=stream.affected)

        if statement.rows is None:
            await payload.commit(conn)

    return SqlResult(engine=OraToolConfig.ENGINE, statements=[statement])


@tool
async def ora_list_tables(
    connection: OraConnection,
    schema_name: Annotated[
        str | None,
        Field(
            description=(
                "Опциональный фильтр по схеме (владельцу), например `HR`. "
                "Пусто = все схемы, кроме служебных схем Oracle."
            ),
        ),
    ] = None,
    *,
    offset: RowOffset,
    limit: RowLimit,
) -> SqlResult:
    """Список таблиц, представлений и mview, доступных соединению. Колонки:
    schema, name, kind, status, last_ddl_time.

    Выдача постраничная: сколько показано и как листать, сказано в note.
    """
    builder = (
        OraQueryBuilder()
        .add(
            """
            select
                o.owner as schema,
                o.object_name as name,
                o.object_type as kind,
                o.status,
                o.last_ddl_time
            from
                all_objects o
                join all_users u on u.username = o.owner
            where
                o.object_type in (""",
            ObjectKind.relations(),
            ")",
        )
        .when(schema_name is None, "and u.oracle_maintained = 'N'")
        .when(schema_name is not None, "and o.owner = :owner", owner=schema_name)
        .add("order by o.owner, o.object_name")
    )

    return await run_and_collect(
        connection, builder.build(), RowWindow(offset=offset, limit=limit)
    )


@tool
async def ora_describe_table(
    connection: OraConnection,
    table: Annotated[
        str,
        Field(min_length=1, description="Имя таблицы или представления заглавными."),
    ],
    schema_name: Annotated[
        str | None,
        Field(description="Схема таблицы; пусто — искать во всех доступных схемах."),
    ] = None,
    *,
    offset: RowOffset,
    limit: RowLimit,
) -> SqlResult:
    """Схема таблицы: колонки, типы, обязательность, умолчания, комментарии.

    Широкая таблица приходит частями: как листать, сказано в note.
    """
    builder = (
        OraQueryBuilder()
        .add(
            """
            select
                c.owner || '.' || c.table_name || '.' || c.column_name as address,
                c.owner as schema,
                c.table_name,
                c.column_name,
                c.column_id,
                c.data_type,
                c.data_length,
                c.data_precision,
                c.data_scale,
                c.nullable,
                c.data_default,
                m.comments
            from
                all_tab_columns c
                left join all_col_comments m
                    on m.owner = c.owner
                    and m.table_name = c.table_name
                    and m.column_name = c.column_name
            where
                c.table_name = :table_name
            """,
            table_name=table,
        )
        .when(schema_name is not None, "and c.owner = :owner", owner=schema_name)
        .add("order by c.owner, c.table_name, c.column_id")
    )

    return await run_and_collect(
        connection, builder.build(), RowWindow(offset=offset, limit=limit)
    )


@tool
async def ora_query(
    sql: Annotated[
        str,
        Field(
            min_length=1,
            description=(
                "Одна команда SQL без `;` в конце или один анонимный блок "
                "PL/SQL (у него `;` часть синтаксиса). Выборка возвращает "
                "строки окном offset/limit; INSERT/UPDATE/DELETE/DDL "
                "возвращают число затронутых строк и фиксируются сразу. "
                "Несколько команд через `;` Oracle одним вызовом не принимает: "
                "зовите инструмент на каждую."
            ),
        ),
        MarkdownResult(language="sql"),
    ],
    connection: OraConnection,
    *,
    offset: RowOffset,
    limit: RowLimit,
) -> SqlResult:
    """Выполнить SQL на подключении: строки либо счётчик затронутых."""

    return await run_statement(connection, sql, RowWindow(offset=offset, limit=limit))


@tool
async def ora_database_describe(
    connection: OraConnection,
) -> SqlResult:
    """Oracle: база, к которой ведёт соединение. Колонки: address (сервис),
    con_name (контейнер PDB), db_name, version (баннер сервера), charset.
    Одна строка, грантов не требует."""
    builder = OraQueryBuilder().add(
        """
        select
            sys_context('userenv', 'service_name') as address,
            sys_context('userenv', 'con_name') as con_name,
            sys_context('userenv', 'db_name') as db_name,
            (select banner from v$version where rownum = 1) as version,
            (select value from nls_database_parameters
             where parameter = 'NLS_CHARACTERSET') as charset
        from dual
        """
    )

    return await run_and_collect(
        connection, builder.build(), RowWindow(offset=0, limit=1)
    )


@tool
async def ora_schema_describe(
    connection: OraConnection,
    schema_name: SchemaFilter,
    *,
    offset: RowOffset,
    limit: RowLimit,
) -> SqlResult:
    """Oracle: схемы (пользователи-владельцы) из all_users. Колонки: address,
    name, created, oracle_maintained. `*` — все схемы, кроме служебных схем
    Oracle. Выдача постраничная — как листать, сказано в note."""
    builder = (
        OraQueryBuilder()
        .add(
            """
            select
                u.username as address,
                u.username as name,
                u.created,
                u.oracle_maintained
            from
                all_users u
            where 1=1
            """
        )
        .when(schema_name == "*", "and u.oracle_maintained = 'N'")
        .when(schema_name != "*", "and u.username = :owner", owner=schema_name)
        .add("order by u.username")
    )

    return await run_and_collect(
        connection, builder.build(), RowWindow(offset=offset, limit=limit)
    )


@tool
async def ora_table_describe(
    connection: OraConnection,
    schema_name: SchemaFilter,
    table: TableFilter,
    *,
    offset: RowOffset,
    limit: RowLimit,
) -> SqlResult:
    """Oracle: таблицы из all_tables с комментарием. Колонки: address, schema,
    name, tablespace_name, partitioned, temporary, iot_type, num_rows,
    last_analyzed, comments. Представления смотрите в ora_list_tables и
    ora_describe_table. Выдача постраничная — как листать, сказано в note."""
    builder = (
        OraQueryBuilder()
        .add(
            """
            select
                t.owner || '.' || t.table_name as address,
                t.owner as schema,
                t.table_name as name,
                t.tablespace_name,
                t.partitioned,
                t.temporary,
                t.iot_type,
                t.num_rows,
                t.last_analyzed,
                m.comments
            from
                all_tables t
                join all_users u on u.username = t.owner
                left join all_tab_comments m
                    on m.owner = t.owner and m.table_name = t.table_name
            where 1=1
            """
        )
        .when(schema_name == "*", "and u.oracle_maintained = 'N'")
        .when(schema_name != "*", "and t.owner = :owner", owner=schema_name)
        .when(table != "*", "and t.table_name = :table_name", table_name=table)
        .add("order by t.owner, t.table_name")
    )

    return await run_and_collect(
        connection, builder.build(), RowWindow(offset=offset, limit=limit)
    )


@tool
async def ora_column_describe(
    connection: OraConnection,
    schema_name: SchemaFilter,
    table: TableFilter,
    *,
    offset: RowOffset,
    limit: RowLimit,
) -> SqlResult:
    """Oracle: колонки таблиц и представлений из all_tab_columns с комментарием.
    Колонки: address, schema, table_name, column_name, column_id, data_type,
    data_length, data_precision, data_scale, nullable, data_default, comments.
    Для широких таблиц выдача приходит частями — как листать, сказано в note."""
    builder = (
        OraQueryBuilder()
        .add(
            """
            select
                c.owner || '.' || c.table_name || '.' || c.column_name as address,
                c.owner as schema,
                c.table_name,
                c.column_name,
                c.column_id,
                c.data_type,
                c.data_length,
                c.data_precision,
                c.data_scale,
                c.nullable,
                c.data_default,
                m.comments
            from
                all_tab_columns c
                join all_users u on u.username = c.owner
                left join all_col_comments m
                    on m.owner = c.owner
                    and m.table_name = c.table_name
                    and m.column_name = c.column_name
            where 1=1
            """
        )
        .when(schema_name == "*", "and u.oracle_maintained = 'N'")
        .when(schema_name != "*", "and c.owner = :owner", owner=schema_name)
        .when(table != "*", "and c.table_name = :table_name", table_name=table)
        .add("order by c.owner, c.table_name, c.column_id")
    )

    return await run_and_collect(
        connection, builder.build(), RowWindow(offset=offset, limit=limit)
    )


@tool
async def ora_constraints_describe(
    connection: OraConnection,
    schema_name: SchemaFilter,
    table: TableFilter,
    *,
    offset: RowOffset,
    limit: RowLimit,
) -> SqlResult:
    """Oracle: ограничения из all_constraints с колонками через запятую.
    Колонки: address, schema, table_name, constraint_name, constraint_type
    (P первичный, U уникальный, R внешний, C check и not null, V check option,
    O read only), columns, search_condition, r_owner, r_constraint_name,
    delete_rule, status. Выдача постраничная — как листать, сказано в note."""
    builder = (
        OraQueryBuilder()
        .add(
            """
            select
                k.owner || '.' || k.table_name || '.' || k.constraint_name as address,
                k.owner as schema,
                k.table_name,
                k.constraint_name,
                k.constraint_type,
                (select listagg(cc.column_name, ', ')
                        within group (order by cc.position)
                 from all_cons_columns cc
                 where cc.owner = k.owner
                   and cc.constraint_name = k.constraint_name) as columns,
                k.search_condition_vc as search_condition,
                k.r_owner,
                k.r_constraint_name,
                k.delete_rule,
                k.status
            from
                all_constraints k
                join all_users u on u.username = k.owner
            where 1=1
            """
        )
        .when(schema_name == "*", "and u.oracle_maintained = 'N'")
        .when(schema_name != "*", "and k.owner = :owner", owner=schema_name)
        .when(table != "*", "and k.table_name = :table_name", table_name=table)
        .add("order by k.owner, k.table_name, k.constraint_type, k.constraint_name")
    )

    return await run_and_collect(
        connection, builder.build(), RowWindow(offset=offset, limit=limit)
    )


@tool
async def ora_indexes_describe(
    connection: OraConnection,
    schema_name: SchemaFilter,
    table: TableFilter,
    *,
    offset: RowOffset,
    limit: RowLimit,
) -> SqlResult:
    """Oracle: индексы из all_indexes с колонками через запятую. Колонки:
    address, schema, table_name, index_name, index_type, uniqueness, columns,
    tablespace_name, status. Выражение функционального индекса в списке
    колонок стоит именем скрытой колонки SYS_NC…; само выражение смотрите в
    all_ind_expressions через ora_query. Выдача постраничная — как листать,
    сказано в note."""
    builder = (
        OraQueryBuilder()
        .add(
            """
            select
                i.table_owner || '.' || i.table_name || '.' || i.index_name as address,
                i.owner as schema,
                i.table_name,
                i.index_name,
                i.index_type,
                i.uniqueness,
                (select listagg(ic.column_name, ', ')
                        within group (order by ic.column_position)
                 from all_ind_columns ic
                 where ic.index_owner = i.owner
                   and ic.index_name = i.index_name) as columns,
                i.tablespace_name,
                i.status
            from
                all_indexes i
            join
                all_users u on u.username = i.owner
            where 1=1
            """
        )
        .when(schema_name == "*", "and u.oracle_maintained = 'N'")
        .when(schema_name != "*", "and i.table_owner = :owner", owner=schema_name)
        .when(table != "*", "and i.table_name = :table_name", table_name=table)
        .add("order by i.table_owner, i.table_name, i.index_name")
    )

    return await run_and_collect(
        connection, builder.build(), RowWindow(offset=offset, limit=limit)
    )


@tool
async def ora_routines_describe(
    connection: OraConnection,
    schema_name: SchemaFilter,
    routine: Annotated[
        str,
        Field(
            min_length=1,
            description=(
                "Шаблон имени в синтаксисе LIKE заглавными: `ORDER%`, `%API`. "
                "`*` — все подпрограммы схемы."
            ),
        ),
    ] = "*",
    *,
    offset: RowOffset,
    limit: RowLimit,
) -> SqlResult:
    """Oracle: процедуры, функции, пакеты и типы из all_objects. Колонки:
    address, schema, name, kind, status, created, last_ddl_time. Тела и
    аргументы смотрите в all_source и all_arguments через ora_query.
    Выдача постраничная — как листать, сказано в note."""
    builder = (
        OraQueryBuilder()
        .add(
            """
            select
                o.owner || '.' || o.object_name as address,
                o.owner as schema,
                o.object_name as name,
                o.object_type as kind,
                o.status,
                o.created,
                o.last_ddl_time
            from
                all_objects o
                join all_users u on u.username = o.owner
            where
                o.object_type in (""",
            ObjectKind.routines(),
            ")",
        )
        .when(schema_name == "*", "and u.oracle_maintained = 'N'")
        .when(schema_name != "*", "and o.owner = :owner", owner=schema_name)
        .when(routine != "*", "and o.object_name like :routine", routine=routine)
        .add("order by o.owner, o.object_name")
    )

    return await run_and_collect(
        connection, builder.build(), RowWindow(offset=offset, limit=limit)
    )


@tool
async def ora_sequences_describe(
    connection: OraConnection,
    schema_name: SchemaFilter = "*",
    *,
    offset: RowOffset,
    limit: RowLimit,
) -> SqlResult:
    """Oracle: последовательности из all_sequences. Колонки: address, schema,
    name, min_value, max_value, increment_by, cycle_flag, cache_size,
    last_number. Выдача постраничная — как листать, сказано в note."""
    builder = (
        OraQueryBuilder()
        .add(
            """
            select
                s.sequence_owner || '.' || s.sequence_name as address,
                s.sequence_owner as schema,
                s.sequence_name as name,
                s.min_value,
                s.max_value,
                s.increment_by,
                s.cycle_flag,
                s.cache_size,
                s.last_number
            from
                all_sequences s
                join all_users u on u.username = s.sequence_owner
            where 1=1
            """
        )
        .when(schema_name == "*", "and u.oracle_maintained = 'N'")
        .when(schema_name != "*", "and s.sequence_owner = :owner", owner=schema_name)
        .add("order by s.sequence_owner, s.sequence_name")
    )

    return await run_and_collect(
        connection, builder.build(), RowWindow(offset=offset, limit=limit)
    )


@tool
async def ora_types_describe(
    connection: OraConnection,
    schema_name: SchemaFilter = "*",
    *,
    offset: RowOffset,
    limit: RowLimit,
) -> SqlResult:
    """Oracle: объектные типы и коллекции из all_types. Колонки: address,
    schema, name, typecode (OBJECT, COLLECTION), attributes, methods.
    Выдача постраничная — как листать, сказано в note."""
    builder = (
        OraQueryBuilder()
        .add(
            """
            select
                t.owner || '.' || t.type_name as address,
                t.owner as schema,
                t.type_name as name,
                t.typecode,
                t.attributes,
                t.methods
            from
                all_types t
                join all_users u on u.username = t.owner
            where
                t.owner is not null
            """
        )
        .when(schema_name == "*", "and u.oracle_maintained = 'N'")
        .when(schema_name != "*", "and t.owner = :owner", owner=schema_name)
        .add("order by t.owner, t.type_name")
    )

    return await run_and_collect(
        connection, builder.build(), RowWindow(offset=offset, limit=limit)
    )


@tool
async def ora_address(connection: OraConnection) -> TableResult:
    """Базовый url соединения Oracle: oracle://host:port/service.

    Ничего не выполняет в базе. Роли объекта — в query поверх url:
    ?schema=HR&table=EMPLOYEES, ?schema=HR&table=EMPLOYEES&column=EMAIL.
    """
    base = OraAddresses.base_of(connection)
    row = {
        AddressColumn.CONNECTION.value: connection.source.name,
        AddressColumn.URL.value: base.render(),
    }

    return TableResult(rows=[row])


@tool
async def ora_csv_out(
    connection: OraConnection,
    sql: Annotated[
        str,
        Field(
            min_length=1,
            description=(
                "Запрос SELECT целиком. Ответ уходит следующему узлу CSV без "
                "заголовка: строки всегда в двойных кавычках, числа и даты без, "
                "NULL — пустое поле, DATE и TIMESTAMP — ISO с пробелом, float — "
                "nan/inf строчными. Это формат COPY ... FROM STDIN (FORMAT CSV) "
                "postgres по умолчанию. Запрос обязан сам привести: NUMBER без "
                "точности с дробью и FLOAT — to_char(col, 'TM9'); RAW — "
                "rawtohex(col); BLOB — rawtohex(dbms_lob.substr(col, 2000, n)) "
                "кусками; INTERVAL — to_char или число; XMLTYPE — "
                "xmlserialize(document col as clob); JSON — json_serialize(col "
                "returning clob); VECTOR — from_vector(col). Молча теряются: "
                "смещение TIMESTAMP WITH TIME ZONE (to_char(col, "
                "'yyyy-mm-dd hh24:mi:ss.ff6tzh:tzm')) и наносекунды "
                "TIMESTAMP(9) (to_char с ff9)."
            ),
        ),
        MarkdownResult(language="sql"),
    ],
    before: BeforeSteps = (),
    after: AfterSteps = (),
    *,
    out: Annotated[RawOutbound, Injected],
) -> MarkdownResult:
    """Насос выгрузки: строки запроса CSV-байтами в выходной порт.

    Данные идут в выходной порт другому насосу, а не в чат. Пачки
    Arrow по arraysize строк pyarrow пишет в порт сам, Python делает один шаг
    на пачку. Стейтменты before и after идут в той же сессии до и после
    выборки, после них commit. В ответ возвращается состав колонок и шаги
    скриптов.
    """
    from boba.db.oracle.payload import PayloadOracle  # noqa: PLC0415
    from boba.db.oracle.trace import OraSessionTrace  # noqa: PLC0415

    payload = PayloadOracle(connection)
    statement = OraQueryBuilder().raw_query(sql).build()
    async with payload.opened() as conn:
        trace = OraSessionTrace(conn)
        before_steps = await payload.script(conn, before, trace)
        names = await payload.csv_into(conn, statement.text, out, trace)
        after_steps = await payload.script(conn, after, trace)
        await payload.commit(conn)
        report = trace.report(f"streamed out csv: {', '.join(names)}", statement.text)

    return MarkdownResult(text=report.scripted(before_steps, after_steps).render())


class CsvFields:
    """Запись CSV в строку bind'ов: `\\N` это NULL, остальное текст как есть —
    типы значениям даёт стейтмент."""

    def row(self, record: Sequence[str]) -> tuple[str | None, ...]:
        values: list[str | None] = []
        for text in record:
            if text == CsvContract.NULL:
                values.append(None)
                continue

            values.append(text)

        return tuple(values)


class CsvFeed:
    """Записи CSV из сырого входного порта: порции байт склеиваются в строки, а
    csv.reader собирает записи, в том числе с переводом строки внутри кавычек."""

    def __init__(self, feed: RawInbound, chunk_bytes: int) -> None:
        self._feed = feed
        self._chunk_bytes = chunk_bytes
        self.consumed = 0

    def records(self) -> Iterator[Sequence[str]]:
        yield from csv.reader(self._lines())

    def _lines(self) -> Iterator[str]:
        decoder = codecs.getincrementaldecoder(CsvContract.ENCODING)()
        tail = ""
        for chunk in self._feed.chunks(self._chunk_bytes):
            self.consumed += len(chunk)
            text = tail + decoder.decode(chunk)
            head, sep, tail = text.rpartition(CsvContract.LINE_END)
            if not sep:
                continue

            yield from (head + sep).splitlines(keepends=True)

        tail += decoder.decode(b"", True)
        if tail:
            yield tail


@tool
async def ora_csv_in(  # noqa: PLR0913
    connection: OraConnection,
    sql: Annotated[
        str,
        Field(
            min_length=1,
            description=(
                "Стейтмент INSERT с позиционными bind'ами :1..:n в порядке полей "
                "CSV, например: insert into hr.employees (id, name, hired) values "
                "(:1, :2, to_timestamp(:3, 'yyyy-mm-dd hh24:mi:ss.ff6')). Каждое "
                "поле приходит строкой как в потоке, NULL (\\N) — как NULL; "
                "числа, даты и RAW приводит сам стейтмент: to_number, "
                "to_timestamp с форматом, hextoraw(substr(:k, 3)) для "
                "\\x-hex postgres."
            ),
        ),
        MarkdownResult(language="sql"),
    ],
    chunk_bytes: ChunkBytes,
    before: BeforeSteps = (),
    after: AfterSteps = (),
    *,
    feed: Annotated[RawInbound, Injected],
) -> MarkdownResult:
    """Насос загрузки: CSV из входного порта в стейтмент пачками executemany.

    Данные приходят во входной порт от другого насоса. Формат: CSV
    без заголовка, NULL как `\\N`, переводы строк внутри кавычек допустимы
    — то есть COPY (...) TO STDOUT (FORMAT CSV, NULL '\\N') postgres.
    Поля уходят строками, типы задаёт сам стейтмент. Стейтменты before и
    after идут в той же сессии до и после загрузки, DML всего вызова — одна
    транзакция с одним commit после after: ошибка откатывает всё. В ответ
    возвращается счётчик байтов и строк и шаги скриптов.
    """
    from boba.db.oracle.payload import PayloadOracle  # noqa: PLC0415
    from boba.db.oracle.trace import OraSessionTrace  # noqa: PLC0415

    payload = PayloadOracle(connection)
    statement = OraQueryBuilder().raw_query(sql).build()
    rows = 0
    source = CsvFeed(feed, chunk_bytes)
    fields = CsvFields()

    async with payload.opened() as conn:
        trace = OraSessionTrace(conn)
        before_steps = await payload.script(conn, before, trace)
        batch: list[tuple[str | None, ...]] = []
        for record in source.records():
            batch.append(fields.row(record))
            if len(batch) < connection.arraysize:
                continue

            rows += await payload.executemany(conn, statement.text, batch, trace)
            batch = []

        if batch:
            rows += await payload.executemany(conn, statement.text, batch, trace)

        after_steps = await payload.script(conn, after, trace)
        await payload.commit(conn)
        report = trace.report(
            f"copied in {source.consumed} bytes, {rows} rows", statement.text
        )

    return MarkdownResult(text=report.scripted(before_steps, after_steps).render())


@tool
async def ora_arrow_out(
    connection: OraConnection,
    sql: Annotated[
        str,
        Field(
            min_length=1,
            description=(
                "Запрос SELECT целиком. Ответ уходит следующему узлу потоком "
                "Arrow IPC (stream): схема, затем пачки по arraysize строк. "
                "Имена колонок — как их отдаёт Oracle, заглавными; нужны "
                'строчные — алиас в кавычках: col as "col". Типы: NUMBER(p, s) '
                "— decimal128, NUMBER без точности — decimal128(38, 0) (дробь — "
                "ошибка, приведите to_char или cast), BINARY_FLOAT/DOUBLE — "
                "float/double, VARCHAR2/CLOB — large_string, RAW/BLOB — "
                "large_binary, DATE — timestamp[s], TIMESTAMP — timestamp[us] "
                "или [ns], BOOLEAN — bool. INTERVAL, XMLTYPE, JSON, ROWID запрос "
                "приводит сам (to_char, xmlserialize, json_serialize, "
                "rowidtochar). TIMESTAMP WITH TIME ZONE теряет смещение — "
                "sys_extract_utc(col)."
            ),
        ),
        MarkdownResult(language="sql"),
    ],
    before: BeforeSteps = (),
    after: AfterSteps = (),
    *,
    out: Annotated[ArrowOutbound, Injected],
) -> MarkdownResult:
    """Насос выгрузки: строки запроса потоком Arrow IPC в выходной порт.

    Данные идут в выходной порт другому насосу, а не в чат. Пачки
    Arrow драйвера pyarrow пишет в порт сам, без перевода в текст.
    Стейтменты before и after идут в той же сессии до и после выборки,
    после них commit. В ответ — состав схемы потока и шаги скриптов.
    """
    from boba.db.oracle.payload import PayloadOracle  # noqa: PLC0415
    from boba.db.oracle.trace import OraSessionTrace  # noqa: PLC0415

    payload = PayloadOracle(connection)
    statement = OraQueryBuilder().raw_query(sql).build()
    async with payload.opened() as conn:
        trace = OraSessionTrace(conn)
        before_steps = await payload.script(conn, before, trace)
        schema = await payload.arrow_into(conn, statement.text, out, trace)
        after_steps = await payload.script(conn, after, trace)
        await payload.commit(conn)
        report = trace.report(
            f"streamed out arrow ipc: {', '.join(schema.names)}", statement.text
        )

    return MarkdownResult(text=report.scripted(before_steps, after_steps).render())


@tool
async def ora_arrow_in(  # noqa: PLR0913
    connection: OraConnection,
    sql: Annotated[
        str,
        Field(
            min_length=1,
            description=(
                "Стейтмент INSERT с позиционными bind'ами :1..:n в порядке полей "
                "схемы Arrow входного потока, например: insert into hr.employees "
                "(id, name, hired) values (:1, :2, :3). Значения драйвер берёт "
                "из колонок пачки как есть; приведения пишутся в стейтменте."
            ),
        ),
        MarkdownResult(language="sql"),
    ],
    chunk_bytes: ChunkBytes,
    before: BeforeSteps = (),
    after: AfterSteps = (),
    *,
    feed: Annotated[ArrowInbound, Injected],
) -> MarkdownResult:
    """Насос загрузки: поток Arrow IPC из входного порта в стейтмент.

    Данные приходят во входной порт от другого насоса. Каждая пачка
    Arrow уходит одной командой executemany, значения драйвер берёт из
    колонок пачки без разбора в Python. Стейтменты before и after идут в
    той же сессии до и после загрузки, DML всего вызова — одна транзакция с
    одним commit после after: ошибка откатывает всё. В ответ — число
    записанных строк и шаги скриптов.
    """
    from boba.db.oracle.payload import PayloadOracle  # noqa: PLC0415
    from boba.db.oracle.trace import OraSessionTrace  # noqa: PLC0415
    from boba.toolkit.arrow import ArrowIpc  # noqa: PLC0415

    payload = PayloadOracle(connection)
    statement = OraQueryBuilder().raw_query(sql).build()
    inbound = await ArrowIpc().open_in(feed, chunk_bytes)

    rows = 0
    async with payload.opened() as conn:
        trace = OraSessionTrace(conn)
        before_steps = await payload.script(conn, before, trace)
        async for batch in inbound.batches:
            rows += await payload.executemany_arrow(conn, statement.text, batch, trace)

        after_steps = await payload.script(conn, after, trace)
        await payload.commit(conn)
        report = trace.report(f"{rows} rows written", statement.text)

    return MarkdownResult(text=report.scripted(before_steps, after_steps).render())


EXPECTED: Mapping[type[Exception], SqlErrorKind] = {
    AddressError: SqlErrorKind.UNKNOWN_TARGET,
    QueryBuildError: SqlErrorKind.SQL_FAILED,
    OracleError: SqlErrorKind.DATABASE_UNAVAILABLE,
    OracleQueryError: SqlErrorKind.SQL_FAILED,
    ArrowStreamError: SqlErrorKind.SQL_FAILED,
}

TOOLS: Final = ToolMain.toolset(
    ora_list_tables,
    ora_describe_table,
    ora_query,
    ora_address,
    ora_csv_out,
    ora_csv_in,
    ora_arrow_out,
    ora_arrow_in,
    ora_database_describe,
    ora_schema_describe,
    ora_table_describe,
    ora_column_describe,
    ora_constraints_describe,
    ora_indexes_describe,
    ora_routines_describe,
    ora_sequences_describe,
    ora_types_describe,
)

if __name__ == "__main__":
    sys.exit(ToolMain.run(TOOLS))
