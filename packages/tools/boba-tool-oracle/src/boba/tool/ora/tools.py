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
    или имя схемы/таблицы/колонки пустое или с кавычкой внутри.
ArrowStreamError — вход ora_stream_in не читается как поток Arrow IPC.
ContractError — декларация на колонку, которой нет в ответе; правило приёмника
    не сходится со схемами; ora_stream_in получил не arrow.
TransferError — стратегия схемы отказала; тип без пары у Oracle.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence
from enum import StrEnum
from typing import Annotated, Any, ClassVar, Final

from pydantic import Field

from boba.db.oracle import (
    OraLiterals,
    OraQuery,
    OraQueryBuilder,
)
from boba.db.oracle.address import OraAddresses
from boba.db.oracle.connection import OracleConfig
from boba.db.oracle.target import OraTableRef
from boba.toolkit.contract import (
    ArrowContract,
    ColumnDeclaration,
    ContractError,
    ContractTable,
    StreamContract,
)
from boba.toolkit.entry import ToolMain
from boba.toolkit.facade import Injected, UserConnection, tool
from boba.toolkit.ports import (
    ChunkBytes,
    Inbound,
    Outbound,
    StreamGroup,
)
from boba.toolkit.result import (
    MarkdownResult,
    SqlResult,
    SqlStatement,
    TableResult,
)
from boba.toolkit.sql import SqlLimits
from boba.toolkit.transfer import (
    ColumnRules,
    CommandJournal,
    CommandKind,
    CreateTemplate,
    DeleteStrategy,
    FailOnUnknown,
    InsertStrategy,
    SchemaStrategy,
    StreamWire,
    TransferFrame,
    TransferInbound,
    TransferOutbound,
    UnknownTypeStrategy,
)
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


class OraPump(StrEnum):
    """Имена насосов Oracle в журнале команд вызова."""

    STREAM_OUT = "ora_stream_out"
    STREAM_IN = "ora_stream_in"


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


async def run_steps(
    payload: Any, conn: Any, steps: Sequence[str], journal: CommandJournal
) -> None:
    """Стейтменты before/after насоса по одному, по порядку, на том же
    соединении: DML остаётся в транзакции насоса до commit вызывающего, DDL
    Oracle фиксирует сам. Строки выборок не собираются: шаг даёт число
    затронутых строк и предупреждение драйвера; каждый шаг — команда
    журнала вызова. payload и conn — PayloadOracle и соединение драйвера:
    его пакет есть только внутри песочницы."""
    for step in steps:
        query = OraQueryBuilder().raw_query(step).build()
        with journal.command(step, CommandKind.ACTION) as running:
            async with payload.rows(conn, query.text, query.params) as stream:
                running.status = step_status(stream)


def step_status(stream: Any) -> str:
    """Итог шага по RowStream: у выборки и команды без счётчика — done;
    предупреждение драйвера — через `;`."""
    status = f"{stream.affected} rows"
    if stream.names:
        status = "done"

    if stream.affected < 0:
        status = "done"

    if not stream.warning:
        return status

    return f"{status}; warning: {stream.warning}"


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
async def ora_stream_out(  # noqa: PLR0913
    connection: OraConnection,
    sql: Annotated[
        str,
        Field(
            min_length=1,
            description=(
                "Запрос SELECT целиком; выполняется как написан. Имена колонок "
                "Oracle отдаёт заглавными, для строчных — алиас в кавычках: "
                'col as "col".'
            ),
        ),
        MarkdownResult(language="sql"),
    ],
    columns: Annotated[
        Sequence[ColumnDeclaration],
        Field(
            description=(
                "Декларации колонок поверх описания драйвера: заданное "
                "перекрывает найденное, незаданное остаётся от драйвера."
            ),
        ),
    ] = (),
    before: BeforeSteps = (),
    after: AfterSteps = (),
    *,
    out: Annotated[Outbound[TransferFrame], Injected],
) -> SqlResult:
    """Источник sync-потока: строки запроса с контрактом колонок для приёмника.

    Первый кадр — контракт из описания стейтмента после parse (типы,
    точность, null_ok, тексты типов Oracle) с декларациями columns поверх;
    дальше кадры данных потоком Arrow IPC пачками драйвера. Стейтменты
    before и after идут в той же сессии до и после запроса, затем один
    commit. В ответ — состав контракта, строки, шаги скриптов и координаты
    сессии.
    """
    from boba.db.oracle.arrow_stream import OraArrowSource  # noqa: PLC0415
    from boba.db.oracle.payload import PayloadOracle  # noqa: PLC0415
    from boba.db.oracle.trace import OraSessionTrace  # noqa: PLC0415

    journal = CommandJournal(OraPump.STREAM_OUT)
    payload = PayloadOracle(connection)
    statement = OraQueryBuilder().raw_query(sql).build()
    outbound = TransferOutbound(out)
    try:
        async with payload.opened() as conn:
            trace = OraSessionTrace(conn)
            await run_steps(payload, conn, before, journal)
            specs = await OraArrowSource(conn, payload, trace, journal).stream(
                statement.text, columns, outbound
            )
            await run_steps(payload, conn, after, journal)
            await payload.commit(conn)
            contract = ContractTable()
            report = trace.report(
                contract.caption(StreamWire.ARROW.value, specs),
                statement.text,
                columns=contract.rows(specs),
            )
    except Exception as exc:
        raise journal.failed(exc) from exc

    statements = [report.sql_statement(), *journal.statements()]

    return SqlResult(engine=OraToolConfig.ENGINE, statements=statements)


@tool
async def ora_stream_in(  # noqa: PLR0913
    connection: OraConnection,
    schema_name: Annotated[
        str, Field(min_length=1, description="Схема таблицы-приёмника: HR")
    ],
    table_name: Annotated[
        str, Field(min_length=1, description="Таблица-приёмник в схеме: ORDERS")
    ],
    schema_strategy: Annotated[
        SchemaStrategy,
        Field(
            description=(
                "Что делать с таблицей до загрузки, объект с kind:\n"
                "   - create_if_not_exists — создать, если нет\n"
                "       есть — сверить и оставить\n"
                "   - error_if_not_exists — таблица обязана существовать\n"
                "   - error_if_schema_changed — таблица обязана совпадать с потоком\n"
                "   - drop_and_create_if_schema_changed — пересоздать при расхождении\n"
                "   - backup_and_create_if_schema_changed — при расхождении\n"
                "       переименовать в _bak_<время> и создать заново\n"
                "   - drop_and_create — всегда пересоздать\n"
                "   - backup_and_create — всегда переименовать в _bak_<время>\n"
                "       и создать заново\n"
                "   - do_nothing — таблицу не трогать и не сверять\n"
            ),
        ),
    ],
    delete_strategy: Annotated[
        DeleteStrategy,
        Field(
            description=(
                "Что удалить перед вставкой, объект с kind:\n"
                "   - nothing — ничего\n"
                "   - truncate — truncate table\n"
                "   - delete_all — delete без условия\n"
                "   - delete_where — delete по условию, текст условия в поле where\n"
            ),
        ),
    ],
    insert_strategy: Annotated[
        InsertStrategy,
        Field(
            description=(
                "Как вставить поток, объект с kind:\n"
                "   - full — вставить все строки\n"
                "   - nothing — только схема и удаление\n"
                "       поток прочитать и не вставлять\n"
            ),
        ),
    ],
    chunk_bytes: ChunkBytes,
    rules: Annotated[
        ColumnRules,
        Field(
            description=(
                "Правила колонок приёмника, имена колонок строчными:\n"
                "   - rename_columns — {колонка приёмника: поле потока}\n"
                "       только имя, данные не меняются\n"
                "   - column_types — {колонка приёмника: тип Oracle текстом}\n"
                "       перекрывает тип из потока и стратегию unknown_types\n"
            ),
        ),
    ] = ColumnRules(),
    unknown_types: Annotated[
        UnknownTypeStrategy,
        Field(
            description=(
                "Что делать с колонкой, для которой у приёмника нет типа "
                "(массивы, составные типы), объект с kind:\n"
                "   - fail_on_unknown — ошибка с типом источника\n"
                "   - fallback_as_varchar — колонка получает CLOB\n"
                "       явный rules.column_types перекрывает оба варианта\n"
            ),
        ),
    ] = FailOnUnknown(kind="fail_on_unknown"),
    create_table: Annotated[
        str,
        Field(
            min_length=1,
            description=(
                "Шаблон create table, когда стратегия схемы создаёт таблицу. "
                "Цельный стейтмент, переменные обязательны:\n"
                "   - {schema_name} — схема приёмника, экранированная\n"
                "   - {table_name} — имя таблицы, экранированное\n"
                "   - {columns} — колонки с типами из плана\n"
                "Сюда пишутся особенности таблицы: tablespace, partition by, "
                "compress. Литеральные фигурные скобки удваиваются.\n"
            ),
        ),
    ] = OraTableRef.CREATE_TABLE,
    before: BeforeSteps = (),
    after: AfterSteps = (),
    *,
    feed: Annotated[Inbound[TransferFrame], Injected],
    group: Annotated[StreamGroup, Injected],
) -> SqlResult:
    """Приёмник Oracle со стратегиями: поток arrow любого источника в таблицу.

    Контракт потока сверяется с таблицей по семействам типов: целые ложатся
    NUMBER(p), decimal — NUMBER(p, s), строки — VARCHAR2(n CHAR) или CLOB,
    моменты — TIMESTAMP(p), uuid и time — строками, boolean — BOOLEAN на 23
    и NUMBER(1) раньше. Стратегия схемы создаёт, оставляет, бэкапит или
    пересоздаёт таблицу либо отказывает с текстом расхождений; затем
    удаление, затем вставка пачек через executemany без разбора значений в
    Python. DDL Oracle фиксирует сам, удаление и вставка вместе с before и
    after — одна транзакция, коммит — после барьера группы связанных
    вызовов. В ответ — что сделано со схемой и почему, сверка
    по колонкам, что удалено, сколько вставлено.
    """
    from boba.db.oracle.arrow_stream import OraArrowLoader  # noqa: PLC0415
    from boba.db.oracle.payload import PayloadOracle  # noqa: PLC0415
    from boba.db.oracle.trace import OraSessionTrace  # noqa: PLC0415
    from boba.toolkit.contract import Engine as NeutralEngine  # noqa: PLC0415

    journal = CommandJournal(OraPump.STREAM_IN)
    template = CreateTemplate(create_table, OraTableRef.TEMPLATE_VARS)
    inbound = TransferInbound(feed, group)
    table = OraTableRef(schema=schema_name, name=table_name)
    payload = PayloadOracle(connection)
    try:
        head = await inbound.get_schema()
        if head.wire is not StreamWire.ARROW:
            raise ContractError(
                f"ora_stream_in takes the arrow wire only, got {head.wire.value} "
                f"from {head.source_engine.value}"
            )

        contract = ArrowContract.model_validate(head.contract)
        async with payload.opened() as conn:
            trace = OraSessionTrace(conn)
            await run_steps(payload, conn, before, journal)
            loader = OraArrowLoader(
                conn,
                table,
                StreamContract().specs(contract.columns),
                NeutralEngine(head.source_engine.value),
                inbound,
                chunk_bytes,
                payload,
                trace,
                journal,
            )
            report = await loader.run(
                schema_strategy,
                delete_strategy,
                insert_strategy,
                unknown_types,
                rules,
                template,
            )
            await run_steps(payload, conn, after, journal)
            await inbound.committing()
            await payload.commit(conn)
    except Exception as exc:
        raise journal.failed(exc) from exc

    statements = [report.summary(), *journal.statements()]

    return SqlResult(engine=OraToolConfig.ENGINE, statements=statements)


TOOLS: Final = ToolMain.toolset(
    ora_list_tables,
    ora_describe_table,
    ora_query,
    ora_address,
    ora_stream_out,
    ora_stream_in,
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
