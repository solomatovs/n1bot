"""ClickHouse-инструменты: функции уровня модуля, модуль — обычная программа.

Запуск: `python -m boba.tool.ch.tools <имя> --флаги` — та же команда у
launcher'а приложения и у человека в терминале.

Ошибки:
ClickHouseError — до базы не достучаться (сеть, TLS, kerberos).
ClickHouseQueryError — сервер отклонил запрос (синтаксис, права).
UnknownConnectionError — имя подключения вне whitelist'а конфига.
AddressError — у профиля соединения нет базы по умолчанию для ch_address.
QueryBuildError — сборщик получил один параметр с двумя разными значениями.
"""

from __future__ import annotations

import sys
from collections.abc import Mapping, Sequence
from enum import StrEnum
from typing import Annotated, ClassVar, Final

from pydantic import Field

from boba.connections.address import AddressError
from boba.db.clickhouse import ClickHouseError, ClickHouseQueryError
from boba.db.clickhouse.address import ChAddresses
from boba.db.clickhouse.connection import ClickHouseConfig
from boba.db.clickhouse.query import ChQuery, ChQueryBuilder
from boba.toolkit.entry import ToolMain
from boba.toolkit.facade import Injected, UserConnection, tool
from boba.toolkit.ports import ChunkBytes, RawInbound, RawOutbound
from boba.toolkit.result import MarkdownResult, SqlResult, SqlStatement, TableResult
from boba.toolkit.sql import (
    QueryBuildError,
    SqlErrorKind,
    SqlLimits,
)
from boba.toolkit.types import SecretRevealing
from boba.toolkit.window import RowLimit, RowOffset, RowPage, RowWindow

ChConnection = Annotated[ClickHouseConfig, UserConnection]

DatabaseFilter = Annotated[
    str,
    Field(
        min_length=1,
        description=(
            "Имя базы. `*` — все пользовательские базы (без system/information_schema)."
        ),
    ),
]
"""LLM-аргумент database: точное имя или `*`."""

TableFilter = Annotated[
    str,
    Field(min_length=1, description="Имя таблицы или view. `*` — все отношения базы."),
]
"""LLM-аргумент table: точное имя или `*`."""


class SystemDatabase(StrEnum):
    """Базы ClickHouse, которые каталожные инструменты не показывают."""

    SYSTEM = "system"
    INFORMATION_SCHEMA = "INFORMATION_SCHEMA"
    INFORMATION_SCHEMA_LOWER = "information_schema"

    @classmethod
    def names(cls) -> list[str]:
        return [member.value for member in cls]


BeforeSteps = Annotated[
    Sequence[str],
    Field(
        description=(
            "Стейтменты, которые выполняются по порядку перед командой насоса "
            "в той же сессии сервера: SET, create temporary table, проверки. "
            "Транзакций у ClickHouse нет, сессия даёт общие настройки и "
            "временные таблицы. Строки выборок не возвращаются, в ответ идёт "
            "итог каждого шага. Проверка, которая должна остановить насос, "
            "пишется выборкой с ошибкой: select throwIf(count() > 0, "
            "'target is not empty') from db.t."
        ),
    ),
]
AfterSteps = Annotated[
    Sequence[str],
    Field(
        description=(
            "Стейтменты, которые выполняются по порядку после команды насоса "
            "в той же сессии: insert ... select из временной таблицы, alter "
            "table ... replace partition, exchange tables. Отката нет: "
            "ошибка шага оставляет уже загруженные строки на месте. В ответ "
            "идёт итог каждого шага."
        ),
    ),
]


class AddressColumn(StrEnum):
    """Колонки выдачи ch_address."""

    CONNECTION = "connection"
    URL = "url"


class ChToolConfig(SecretRevealing, SqlLimits):
    """Лимиты выдачи ch-инструментов; [tool.ch]."""

    SECTION: ClassVar[str] = "tool.ch"
    ENGINE: ClassVar[str] = "clickhouse"
    """Подпись движка в SqlResult."""


async def run_and_collect(
    connection: ClickHouseConfig,
    query: ChQuery,
    window: RowWindow,
) -> SqlResult:
    """Запрос страницей окна: границы выдачи назначает вызов."""
    page = RowPage(window, skipped=0)

    from boba.db.clickhouse.payload import PayloadClickHouse  # noqa: PLC0415

    payload = PayloadClickHouse
    async with (
        payload.opened_config(connection) as client,
        payload.rows_stream_out(client, query.text, query.params) as stream,
    ):
        async for block in stream.blocks:
            if not page.add(dict(zip(stream.names, block, strict=True))):
                break

    statement = SqlStatement(rows=page.rows, note=page.note())

    return SqlResult(engine=ChToolConfig.ENGINE, statements=[statement])


@tool
async def ch_list_tables(
    connection: ChConnection,
    database: Annotated[
        str | None,
        Field(
            description=(
                "Опциональный фильтр по базе (например `default`). "
                "Пусто = все пользовательские базы "
                "(без system/information_schema)."
            ),
        ),
    ] = None,
    *,
    offset: RowOffset,
    limit: RowLimit,
) -> SqlResult:
    """Список таблиц/view подключения. Колонки: database, table, engine,
    total_rows.

    Выдача постраничная: сколько показано и как листать, сказано в note.
    """
    builder = (
        ChQueryBuilder()
        .add(
            """
            select
                database,
                name as table,
                engine,
                total_rows
            from
                system.tables
            where
                database not in {system_databases:Array(String)}
            """,
            system_databases=SystemDatabase.names(),
        )
        .when(
            database is not None,
            "and database = {database:String}",
            database=database,
        )
        .add("order by database, name")
    )

    return await run_and_collect(
        connection, builder.build(), RowWindow(offset=offset, limit=limit)
    )


@tool
async def ch_list_columns(
    connection: ChConnection,
    database: Annotated[
        str | None,
        Field(
            description=(
                "Опциональный фильтр по базе (например `default`). "
                "Пусто = все пользовательские базы."
            ),
        ),
    ] = None,
    table: Annotated[
        str | None,
        Field(description="Опциональный фильтр по таблице. Пусто = все таблицы."),
    ] = None,
    *,
    offset: RowOffset,
    limit: RowLimit,
) -> SqlResult:
    """Колонки таблиц подключения из system.columns.

    Колонки: database, table, name, position, type, default_kind,
    default_expression, размеры, признаки ключей, compression_codec, comment.
    Выдача постраничная: сколько показано и как листать, сказано в note.
    """
    builder = (
        ChQueryBuilder()
        .add(
            """
            select
                database,
                table,
                name,
                position,
                type,
                default_kind,
                default_expression,
                data_compressed_bytes,
                data_uncompressed_bytes,
                marks_bytes,
                is_in_partition_key,
                is_in_sorting_key,
                is_in_primary_key,
                is_in_sampling_key,
                compression_codec,
                comment
            from
                system.columns
            where
                database not in {system_databases:Array(String)}
            """,
            system_databases=SystemDatabase.names(),
        )
        .when(
            database is not None,
            "and database = {database:String}",
            database=database,
        )
        .when(table is not None, "and table = {table:String}", table=table)
        .add("order by database, table, position")
    )

    return await run_and_collect(
        connection, builder.build(), RowWindow(offset=offset, limit=limit)
    )


@tool
async def ch_query(
    sql: Annotated[
        str,
        Field(
            min_length=1,
            description=(
                "Произвольный SQL ClickHouse. Строки выборки возвращаются "
                "окном offset/limit."
            ),
        ),
        MarkdownResult(language="sql"),
    ],
    connection: ChConnection,
    *,
    offset: RowOffset,
    limit: RowLimit,
) -> SqlResult:
    """Выполнить SQL на выбранном соединении: строки окном offset/limit."""

    return await run_and_collect(
        connection,
        ChQuery(text=sql, params={}),
        RowWindow(offset=offset, limit=limit),
    )


@tool
async def ch_describe_table(
    connection: ChConnection,
    table: Annotated[
        str,
        Field(min_length=1, description="Имя таблицы (без базы)"),
    ],
    database: Annotated[
        str | None,
        Field(
            description="База таблицы; пусто — искать во всех пользовательских базах."
        ),
    ] = None,
    *,
    offset: RowOffset,
    limit: RowLimit,
) -> SqlResult:
    """Схема таблицы: колонки, типы, default-выражения, комментарии.

    Широкая таблица приходит частями: как листать, сказано в note.
    """
    builder = (
        ChQueryBuilder()
        .add(
            """
            select
                concat(database, '.', table, '.', name) as address,
                database,
                table,
                name,
                position,
                type,
                default_kind,
                default_expression,
                data_compressed_bytes,
                data_uncompressed_bytes,
                marks_bytes,
                is_in_partition_key,
                is_in_sorting_key,
                is_in_primary_key,
                is_in_sampling_key,
                compression_codec,
                comment
            from
                system.columns
            where 1=1
                and database not in {system_databases:Array(String)}
                and table = {table:String}
            """,
            system_databases=SystemDatabase.names(),
            table=table,
        )
        .when(
            database is not None,
            "and database = {database:String}",
            database=database,
        )
        .add("order by database, table, position")
    )

    return await run_and_collect(
        connection, builder.build(), RowWindow(offset=offset, limit=limit)
    )


@tool
async def ch_database_describe(
    connection: ChConnection,
    database: Annotated[
        str,
        Field(
            min_length=1,
            description=(
                "Имя базы из system.databases. `*` — все пользовательские базы, "
                "доступные текущему пользователю. Конкретное имя — одна строка."
            ),
        ),
    ],
    *,
    offset: RowOffset,
    limit: RowLimit,
) -> SqlResult:
    """ClickHouse и ADQM: описание баз данных кластера из system.databases.

    Колонки: address (db), name, engine, data_path, metadata_path, uuid,
    comment. Одна строка на базу. Выдача постраничная — как листать,
    сказано в note.
    """
    builder = (
        ChQueryBuilder()
        .add(
            """
            select
                name as address,
                name,
                engine,
                data_path,
                metadata_path,
                uuid,
                comment
            from
                system.databases
            where
                name not in {system_databases:Array(String)}
            """,
            system_databases=SystemDatabase.names(),
        )
        .when(database != "*", "and name = {database:String}", database=database)
        .add("order by name")
    )

    return await run_and_collect(
        connection, builder.build(), RowWindow(offset=offset, limit=limit)
    )


@tool
async def ch_table_describe(
    connection: ChConnection,
    database: DatabaseFilter,
    table: TableFilter,
    *,
    offset: RowOffset,
    limit: RowLimit,
) -> SqlResult:
    """ClickHouse и ADQM: описание таблиц из system.tables: таблицы, view,
    матвью, dictionary, distributed и пр.

    Колонки: address, database, name, engine, is_temporary, total_rows,
    total_bytes, partition_key, sorting_key, primary_key, sampling_key,
    storage_policy, metadata_modification_time, comment. Выдача
    постраничная — как листать, сказано в note.
    """
    builder = (
        ChQueryBuilder()
        .add(
            """
            select
                concat(database, '.', name) as address,
                database,
                name,
                engine,
                is_temporary,
                total_rows,
                total_bytes,
                partition_key,
                sorting_key,
                primary_key,
                sampling_key,
                storage_policy,
                metadata_modification_time,
                comment
            from
                system.tables
            where
                database not in {system_databases:Array(String)}
            """,
            system_databases=SystemDatabase.names(),
        )
        .when(
            database != "*",
            "and database = {database:String}",
            database=database,
        )
        .when(table != "*", "and name = {table:String}", table=table)
        .add("order by database, name")
    )

    return await run_and_collect(
        connection, builder.build(), RowWindow(offset=offset, limit=limit)
    )


@tool
async def ch_column_describe(
    connection: ChConnection,
    database: DatabaseFilter,
    table: TableFilter,
    *,
    offset: RowOffset,
    limit: RowLimit,
) -> SqlResult:
    """ClickHouse и ADQM: описание колонок из system.columns.

    Колонки: address, database, table, name, position, type, default_kind,
    default_expression, data_compressed_bytes, data_uncompressed_bytes,
    marks_bytes, is_in_partition_key, is_in_sorting_key, is_in_primary_key,
    is_in_sampling_key, compression_codec, comment. Для широких таблиц
    выдача приходит частями — как листать, сказано в note.
    """
    builder = (
        ChQueryBuilder()
        .add(
            """
            select
                concat(database, '.', table, '.', name) as address,
                database,
                table,
                name,
                position,
                type,
                default_kind,
                default_expression,
                data_compressed_bytes,
                data_uncompressed_bytes,
                marks_bytes,
                is_in_partition_key,
                is_in_sorting_key,
                is_in_primary_key,
                is_in_sampling_key,
                compression_codec,
                comment
            from
                system.columns
            where
                database not in {system_databases:Array(String)}
            """,
            system_databases=SystemDatabase.names(),
        )
        .when(
            database != "*",
            "and database = {database:String}",
            database=database,
        )
        .when(table != "*", "and table = {table:String}", table=table)
        .add("order by database, table, position")
    )

    return await run_and_collect(
        connection, builder.build(), RowWindow(offset=offset, limit=limit)
    )


@tool
async def ch_constraints_describe(
    connection: ChConnection,
    database: DatabaseFilter,
    table: TableFilter,
    *,
    offset: RowOffset,
    limit: RowLimit,
) -> SqlResult:
    """ClickHouse и ADQM: ограничения из system.constraints (CHECK / ASSUME).

    Колонки: address, database, table, name, type (CHECK/ASSUME), expression.
    В ClickHouse нет PRIMARY/UNIQUE/FOREIGN как отдельных объектов — их роль
    исполняют ключи в system.tables.
    """
    builder = (
        ChQueryBuilder()
        .add(
            """
            select
                concat(database, '.', table, '.', name) as address,
                database,
                table,
                name,
                type,
                expression
            from
                system.constraints
            where
                database not in {system_databases:Array(String)}
            """,
            system_databases=SystemDatabase.names(),
        )
        .when(
            database != "*",
            "and database = {database:String}",
            database=database,
        )
        .when(table != "*", "and table = {table:String}", table=table)
        .add("order by database, table, name")
    )

    return await run_and_collect(
        connection, builder.build(), RowWindow(offset=offset, limit=limit)
    )


@tool
async def ch_indexes_describe(
    connection: ChConnection,
    database: DatabaseFilter,
    table: TableFilter,
    *,
    offset: RowOffset,
    limit: RowLimit,
) -> SqlResult:
    """ClickHouse и ADQM: индексы пропуска данных из
    system.data_skipping_indices.

    Колонки: address, database, table, name, type (minmax/set/bloom_filter/
    ngrambf_v1/tokenbf_v1), expr, granularity, data_compressed_bytes,
    data_uncompressed_bytes. Первичный ключ смотрите в ch_table_describe.
    """
    builder = (
        ChQueryBuilder()
        .add(
            """
            select
                concat(database, '.', table, '.', name) as address,
                database,
                table,
                name,
                type,
                expr,
                granularity,
                data_compressed_bytes,
                data_uncompressed_bytes
            from
                system.data_skipping_indices
            where
                database not in {system_databases:Array(String)}
            """,
            system_databases=SystemDatabase.names(),
        )
        .when(
            database != "*",
            "and database = {database:String}",
            database=database,
        )
        .when(table != "*", "and table = {table:String}", table=table)
        .add("order by database, table, name")
    )

    return await run_and_collect(
        connection, builder.build(), RowWindow(offset=offset, limit=limit)
    )


@tool
async def ch_function_describe(
    connection: ChConnection,
    function: Annotated[
        str,
        Field(
            min_length=1,
            description=(
                "Шаблон имени функции в синтаксисе LIKE: `array%`, `%date%`. "
                "`*` — все функции, включая системные (их очень много, "
                "используйте фильтр)."
            ),
        ),
    ] = "*",
    *,
    offset: RowOffset,
    limit: RowLimit,
) -> SqlResult:
    """ClickHouse и ADQM: функции из system.functions.

    Колонки: address, name, is_aggregate, case_insensitive, alias_to, origin
    (System/User/…), syntax, arguments, returned_value, description,
    categories. В ClickHouse нет процедур — есть встроенные и UDF-функции.
    """
    builder = (
        ChQueryBuilder()
        .add(
            """
            select
                name as address,
                name,
                is_aggregate,
                case_insensitive,
                alias_to,
                origin,
                syntax,
                arguments,
                returned_value,
                description,
                categories
            from
                system.functions
            where 1=1
            """
        )
        .when(
            function != "*",
            "and name like {function:String}",
            function=function,
        )
        .add("order by name")
    )

    return await run_and_collect(
        connection, builder.build(), RowWindow(offset=offset, limit=limit)
    )


@tool
async def ch_sequences_describe(
    connection: ChConnection,
    database: DatabaseFilter = "*",
    *,
    offset: RowOffset,
    limit: RowLimit,
) -> SqlResult:
    """ClickHouse и ADQM: последовательности из system.sequences.

    Колонки: address, database, name, uuid, start_value, increment,
    min_value, max_value, cycle, cache, comment. На старых версиях таблицы
    нет — запрос упадёт с ошибкой сервера.
    """
    builder = (
        ChQueryBuilder()
        .add(
            """
            select
                concat(database, '.', name) as address,
                database,
                name,
                uuid,
                start_value,
                increment,
                min_value,
                max_value,
                cycle,
                cache,
                comment
            from
                system.sequences
            where
                database not in {system_databases:Array(String)}
            """,
            system_databases=SystemDatabase.names(),
        )
        .when(
            database != "*",
            "and database = {database:String}",
            database=database,
        )
        .add("order by database, name")
    )

    return await run_and_collect(
        connection, builder.build(), RowWindow(offset=offset, limit=limit)
    )


@tool
async def ch_types_describe(
    connection: ChConnection,
    name: Annotated[
        str,
        Field(
            min_length=1,
            description=(
                "Шаблон имени типа в синтаксисе LIKE: `UInt%`, `%String%`. "
                "`*` — все типы."
            ),
        ),
    ] = "*",
    *,
    offset: RowOffset,
    limit: RowLimit,
) -> SqlResult:
    """ClickHouse и ADQM: типы данных из system.data_type_families.

    Колонки: address, name, case_insensitive, alias_to. В ClickHouse нет
    enum/domain/composite; Enum-типы описываются прямо в колонке — смотрите
    ch_column_describe.
    """
    builder = (
        ChQueryBuilder()
        .add(
            """
            select
                name as address,
                name,
                case_insensitive,
                alias_to
            from
                system.data_type_families
            where 1=1
            """
        )
        .when(name != "*", "and name like {name:String}", name=name)
        .add("order by name")
    )

    return await run_and_collect(
        connection, builder.build(), RowWindow(offset=offset, limit=limit)
    )


class Edm(StrEnum):
    """Словарь имён выгрузки ЕДМ (data governance) в ClickHouse: таблицы,
    атрибуты объектов, типы объектов и имена связей; база — аргумент вызова."""

    ASSETS = "dp_edm__com_dg_export_data__assets_current_versions"
    RELATIONS = "dp_edm__com_dg_export_data__relations_current_version"
    RELATION_TYPES = "dp_edm__com_dg_export_data__relation_types"
    ATTRIBUTES = "dp_edm__com_dg_export_data__attribute_list_current_versions"
    ATTRIBUTES_PHYSICAL = (
        "dp_edm__com_dg_export_data__attribute_list_physical_current_versions"
    )

    NAME = "name"
    SHORT_DESCRIPTION = "short_description_edm"
    EXTENDED_DESCRIPTION = "extended_description_edm"
    DESCRIPTION = "description"
    ED_ENTITY_NAME = "ed_entity_name"
    ED_ATTRIBUTE_NAME = "ed_attribute_name"

    TABLE = "pdm_table"
    VIEW = "pdm_view"
    TABLE_COLUMN = "pdm_table_column"
    VIEW_COLUMN = "pdm_view_column"

    LOGICAL_TO_PHYSICAL = "lnk_ldm_physical_relationship"

    @classmethod
    def described_attributes(cls) -> list[str]:
        """Атрибуты описания физического объекта: имя и три текста."""
        return [
            cls.NAME.value,
            cls.SHORT_DESCRIPTION.value,
            cls.EXTENDED_DESCRIPTION.value,
            cls.DESCRIPTION.value,
        ]

    @classmethod
    def ed_name_attributes(cls) -> list[str]:
        """Атрибуты имени логического объекта (сущность и её атрибут)."""
        return [cls.ED_ENTITY_NAME.value, cls.ED_ATTRIBUTE_NAME.value]

    @classmethod
    def relation_types(cls) -> list[str]:
        """Типы объектов, у которых есть колонки: таблица и view."""
        return [cls.TABLE.value, cls.VIEW.value]

    @classmethod
    def column_types(cls) -> list[str]:
        """Типы колонок таблицы и view."""
        return [cls.TABLE_COLUMN.value, cls.VIEW_COLUMN.value]


@tool
async def ch_edm_structure(  # noqa: PLR0913
    connection: ChConnection,
    database: Annotated[
        str,
        Field(
            min_length=1,
            description="База ClickHouse с выгрузкой ЕДМ (например `cmn_cds`).",
        ),
    ],
    table: Annotated[
        str,
        Field(
            min_length=1,
            description="Имя таблицы или view. `*` — все отношения выгрузки.",
        ),
    ] = "*",
    path: Annotated[
        str,
        Field(
            min_length=1,
            description=(
                "Шаблон пути таблицы в синтаксисе LIKE: `%/dwh/%`, `/dwh/orders%`. "
                "`*` — без фильтра по пути."
            ),
        ),
    ] = "*",
    *,
    offset: RowOffset,
    limit: RowLimit,
) -> SqlResult:
    """Структура таблиц и view из выгрузки ЕДМ: одна строка на колонку.

    Колонки: etalon_id (id колонки в ЕДМ), etalon_id_parent (id таблицы),
    path (путь таблицы в ЕДМ), table_name, column_name. Фильтр table —
    точное имя таблицы или view, path — шаблон LIKE пути таблицы. Выдача
    постраничная: сколько показано и как листать, сказано в note.
    """
    builder = (
        ChQueryBuilder()
        .add(
            """
            with w_name as (
                select
                    etalon_id,
                    value
                from
                    {db:Identifier}.{attributes_physical:Identifier}
                where
                    attribute_id = {name_attribute:String}
            )
            select
                r.etalon_id_to      as etalon_id,
                r.etalon_id_from    as etalon_id_parent,
                a.path || '/' || obn.value as path,
                obn.value   as table_name,
                an.value    as column_name
            from
                {db:Identifier}.{relations:Identifier} r
                inner join {db:Identifier}.{assets:Identifier} a
                    on a.id = r.etalon_id_from
                inner join {db:Identifier}.{relation_types:Identifier} rtl
                    on rtl.relation_type_id = r.relation_type_id
                    and rtl.is_inner = 1
                inner join w_name obn
                    on obn.etalon_id = r.etalon_id_from
                inner join w_name an
                    on an.etalon_id = r.etalon_id_to
            where 1=1
                and rtl.type_to in {column_types:Array(String)}
                and rtl.type_from in {relation_types_from:Array(String)}
            """,
            db=database,
            attributes_physical=Edm.ATTRIBUTES_PHYSICAL.value,
            relations=Edm.RELATIONS.value,
            assets=Edm.ASSETS.value,
            relation_types=Edm.RELATION_TYPES.value,
            name_attribute=Edm.NAME.value,
            column_types=Edm.column_types(),
            relation_types_from=Edm.relation_types(),
        )
        .when(table != "*", "and obn.value = {table:String}", table=table)
        .when(
            path != "*",
            "and a.path || '/' || obn.value like {path:String}",
            path=path,
        )
        .add("order by path, column_name")
    )

    return await run_and_collect(
        connection, builder.build(), RowWindow(offset=offset, limit=limit)
    )


@tool
async def ch_edm_descriptions(  # noqa: PLR0913
    connection: ChConnection,
    database: Annotated[
        str,
        Field(
            min_length=1,
            description="База ClickHouse с выгрузкой ЕДМ (например `cmn_cds`).",
        ),
    ],
    name: Annotated[
        str,
        Field(
            min_length=1,
            description=(
                "Точное имя физического объекта ЕДМ (таблицы, view или колонки). "
                "`*` — все объекты."
            ),
        ),
    ] = "*",
    path: Annotated[
        str,
        Field(
            min_length=1,
            description=(
                "Шаблон пути объекта в синтаксисе LIKE: `%/dwh/%`, "
                "`/dwh/orders/%`. `*` — без фильтра по пути."
            ),
        ),
    ] = "*",
    *,
    offset: RowOffset,
    limit: RowLimit,
) -> SqlResult:
    """Описания физических объектов из выгрузки ЕДМ: таблицы, view и колонки.

    Колонки: name, path (путь объекта в ЕДМ), short_description_edm,
    extended_description_edm, description_from_source (описание из
    источника), ed_name (имя связанной логической сущности или атрибута;
    пусто, если связи нет). Фильтр name — точное имя объекта, path — шаблон
    LIKE пути. Выдача постраничная: сколько показано и как листать, сказано
    в note.
    """
    builder = (
        ChQueryBuilder()
        .add(
            """
            with w_pdm as (
                select
                    al.etalon_id,
                    a.path,
                    maxIf(al.value, al.attribute_id = {name_attribute:String})
                        as name,
                    maxIf(al.value, al.attribute_id = {short_attribute:String})
                        as short_description_edm,
                    maxIf(al.value, al.attribute_id = {extended_attribute:String})
                        as extended_description_edm,
                    maxIf(al.value, al.attribute_id = {source_attribute:String})
                        as description_from_source
                from
                    {db:Identifier}.{attributes_physical:Identifier} al
                    inner join {db:Identifier}.{assets:Identifier} a
                        on a.id = al.etalon_id
                where
                    al.attribute_id in {described_attributes:Array(String)}
                group by
                    al.etalon_id,
                    a.path
            ), w_ed as (
                select
                    r.etalon_id_from as etalon_id_ed,
                    r.etalon_id_to as etalon_id_pdm,
                    a.value as ed_name
                from
                    {db:Identifier}.{relations:Identifier} r
                    inner join {db:Identifier}.{attributes:Identifier} a
                        on a.etalon_id = r.etalon_id_from
                        and a.attribute_id in {ed_attributes:Array(String)}
                where
                    r.name = {logical_relation:String}
            )
            select
                pdm.name as name,
                pdm.path || '/' || pdm.name as path,
                pdm.short_description_edm as short_description_edm,
                pdm.extended_description_edm as extended_description_edm,
                pdm.description_from_source as description_from_source,
                ed.ed_name as ed_name
            from
                w_pdm pdm
                left join w_ed ed
                    on ed.etalon_id_pdm = pdm.etalon_id
            where true
            """,
            db=database,
            attributes_physical=Edm.ATTRIBUTES_PHYSICAL.value,
            attributes=Edm.ATTRIBUTES.value,
            assets=Edm.ASSETS.value,
            relations=Edm.RELATIONS.value,
            name_attribute=Edm.NAME.value,
            short_attribute=Edm.SHORT_DESCRIPTION.value,
            extended_attribute=Edm.EXTENDED_DESCRIPTION.value,
            source_attribute=Edm.DESCRIPTION.value,
            described_attributes=Edm.described_attributes(),
            ed_attributes=Edm.ed_name_attributes(),
            logical_relation=Edm.LOGICAL_TO_PHYSICAL.value,
        )
        .when(name != "*", "and pdm.name = {name:String}", name=name)
        .when(
            path != "*",
            "and pdm.path || '/' || pdm.name like {path:String}",
            path=path,
        )
        .add("order by path, name")
    )

    return await run_and_collect(
        connection, builder.build(), RowWindow(offset=offset, limit=limit)
    )


@tool
async def ch_stream_out(  # noqa: PLR0913
    connection: ChConnection,
    sql: Annotated[
        str,
        Field(
            min_length=1,
            description=(
                "Запрос ClickHouse целиком, с FORMAT в конце: "
                "SELECT ... FROM db.t FORMAT TabSeparated. Ответ сервера уходит "
                "следующему узлу байтами как есть, поэтому формат обязан "
                "совпадать с тем, что ждёт приёмник. Без FORMAT сервер отдаёт "
                "TabSeparated. Настройки формата пишутся в запросе: SELECT ... "
                "SETTINGS output_format_json_quote_denormals = 1 FORMAT "
                "JSONEachRow. В TabSeparated NULL это \\N, в CSV тоже \\N; "
                "с именами и типами колонок в шапке — TabSeparatedWithNamesAndTypes."
            ),
        ),
        MarkdownResult(language="sql"),
    ],
    chunk_bytes: ChunkBytes,
    before: BeforeSteps = (),
    after: AfterSteps = (),
    *,
    out: Annotated[RawOutbound, Injected],
) -> MarkdownResult:
    """Насос выгрузки: ответ запроса сырыми байтами в выходной порт.

    Данные идут в выходной порт другому насосу, а не в чат. Формат и
    настройки задаёт текст запроса, инструмент его не разбирает и отдаёт
    блоки ответа как пришли; размер блока — chunk_bytes. Стейтменты before
    и after идут в той же сессии сервера до и после запроса.
    """
    from boba.db.clickhouse.payload import (  # noqa: PLC0415
        PayloadClickHouse,
        ReadTuning,
    )

    payload = PayloadClickHouse
    statement = ChQueryBuilder().raw_query(sql).build()
    tuning = ReadTuning(socket_read_size=chunk_bytes, read_buffer_size=chunk_bytes)
    async with payload.opened_session(connection) as client:
        before_steps = await payload.script(client, before)

        async with payload.byte_stream_out(
            client, statement.text, tuning=tuning
        ) as stream:
            total = 0
            async for block in stream.blocks:
                total += len(block)
                await out.send(block)

            report = stream.trace.report(f"copied out {total} bytes", statement.text)

        after_steps = await payload.script(client, after)

    return MarkdownResult(text=report.scripted(before_steps, after_steps).render())


@tool
async def ch_stream_in(  # noqa: PLR0913
    connection: ChConnection,
    sql: Annotated[
        str,
        Field(
            min_length=1,
            description=(
                "Стейтмент INSERT целиком, с FORMAT в конце: "
                "INSERT INTO db.t (id, name) FORMAT TabSeparated. Тело приходит "
                "от предыдущего узла байтами как есть, формат обязан совпадать с "
                "тем, что отдал источник. Настройки пишутся перед FORMAT: "
                "INSERT INTO db.t SETTINGS input_format_skip_unknown_fields = 0 "
                "FORMAT JSONEachRow. Привести типы или переименовать колонки на "
                "лету можно табличной функцией input: INSERT INTO db.t SELECT "
                "toUInt64(c1), upper(c2) FROM input('c1 String, c2 String') "
                "FORMAT CSV. Форматы с именами в шапке сопоставляют колонки по "
                "именам, лишнюю колонку сервер молча пропускает."
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
    """Насос загрузки: тело из входного порта одним INSERT ... FORMAT.

    Данные приходят во входной порт от другого насоса и уезжают
    серверу как есть, блоками по chunk_bytes, без разбора на клиенте;
    стейтмент тоже уходит как написан. Стейтменты before и after идут в
    той же сессии сервера до и после INSERT: временная таблица из before
    видна INSERT и after. В ответ — число записанных строк по сводке
    сервера и шаги скриптов.
    """
    from boba.db.clickhouse.payload import PayloadClickHouse  # noqa: PLC0415

    payload = PayloadClickHouse
    statement = ChQueryBuilder().raw_query(sql).build()
    async with payload.opened_session(connection) as client:
        before_steps = await payload.script(client, before)
        trace = await payload.byte_stream_in(
            client, statement.text, blocks=feed.blocks(chunk_bytes)
        )
        after_steps = await payload.script(client, after)

    report = trace.report(f"{trace.written_rows} rows written", statement.text)

    return MarkdownResult(text=report.scripted(before_steps, after_steps).render())


@tool
async def ch_arrow_out(  # noqa: PLR0913
    connection: ChConnection,
    sql: Annotated[
        str,
        Field(
            min_length=1,
            description=(
                "Запрос SELECT целиком, без FORMAT: формат ArrowStream добавляет "
                "инструмент. Ответ уходит следующему узлу потоком Arrow IPC: "
                "схема, затем пачки записей. Настройки пишутся в запросе: "
                "SELECT ... SETTINGS output_format_arrow_string_as_string = 1. "
                "Что приводить для приёмника Oracle: DateTime — "
                "toDateTime64(col, 0, 'UTC') (иначе uint32), Bool — toUInt8(col), "
                "UUID и String с байтами — hex(col)."
            ),
        ),
        MarkdownResult(language="sql"),
    ],
    chunk_bytes: ChunkBytes,
    before: BeforeSteps = (),
    after: AfterSteps = (),
    *,
    out: Annotated[RawOutbound, Injected],
) -> MarkdownResult:
    """Насос выгрузки потоком Arrow IPC: ch_stream_out с форматом ArrowStream,
    который дописывает драйвер; сервер пишет поток сам, блоки уходят в порт
    как пришли. Стейтменты before и after идут в той же сессии сервера до и
    после запроса.
    """
    from boba.db.clickhouse.payload import (  # noqa: PLC0415
        PayloadClickHouse,
        ReadTuning,
    )

    payload = PayloadClickHouse
    statement = ChQueryBuilder().raw_query(sql).build()
    tuning = ReadTuning(socket_read_size=chunk_bytes, read_buffer_size=chunk_bytes)
    async with payload.opened_session(connection) as client:
        before_steps = await payload.script(client, before)

        async with payload.byte_stream_out(
            client, statement.text, "ArrowStream", tuning=tuning
        ) as stream:
            total = 0
            async for block in stream.blocks:
                total += len(block)
                await out.send(block)

            report = stream.trace.report(
                f"streamed out arrow ipc: {total} bytes", statement.text
            )

        after_steps = await payload.script(client, after)

    return MarkdownResult(text=report.scripted(before_steps, after_steps).render())


@tool
async def ch_arrow_in(  # noqa: PLR0913
    connection: ChConnection,
    sql: Annotated[
        str,
        Field(
            min_length=1,
            description=(
                "Стейтмент INSERT целиком с FORMAT ArrowStream в конце: "
                "INSERT INTO db.t FORMAT ArrowStream. Тело — поток Arrow IPC от "
                "предыдущего узла, колонки сопоставляются по именам полей схемы. "
                "Если источник — Oracle, имена в схеме заглавные: INSERT INTO "
                "db.t SETTINGS input_format_arrow_case_insensitive_column_matching "
                "= 1 FORMAT ArrowStream, иначе новые версии сервера молча пишут "
                "значения по умолчанию, а 22.12 отвечает THERE_IS_NO_COLUMN. "
                "Nullable-колонки таблицы принимают null-биты Arrow, обычные "
                "получают значение по умолчанию."
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
    """Насос загрузки потоком Arrow IPC: ch_stream_in для тела Arrow, поток
    уходит серверу как есть, разбирает его сервер. Стейтменты before и after
    идут в той же сессии сервера до и после INSERT.
    """
    from boba.db.clickhouse.payload import PayloadClickHouse  # noqa: PLC0415

    payload = PayloadClickHouse
    statement = ChQueryBuilder().raw_query(sql).build()
    async with payload.opened_session(connection) as client:
        before_steps = await payload.script(client, before)
        trace = await payload.byte_stream_in(
            client, statement.text, blocks=feed.blocks(chunk_bytes)
        )
        after_steps = await payload.script(client, after)

    report = trace.report(f"{trace.written_rows} rows written", statement.text)

    return MarkdownResult(text=report.scripted(before_steps, after_steps).render())


@tool
async def ch_address(connection: ChConnection) -> TableResult:
    """Базовый url соединения ClickHouse: clickhouse://host:port/database.

    Ничего не выполняет в базе. В url — база по умолчанию соединения;
    объект в другой базе адресуется заменой сегмента пути. Роли объекта —
    в query поверх url: ?table=events, ?table=events&column=user_id.
    """
    base = ChAddresses.base_of(connection)
    row = {
        AddressColumn.CONNECTION.value: connection.source.name,
        AddressColumn.URL.value: base.render(),
    }

    return TableResult(rows=[row])


EXPECTED: Mapping[type[Exception], SqlErrorKind] = {
    AddressError: SqlErrorKind.UNKNOWN_TARGET,
    QueryBuildError: SqlErrorKind.SQL_FAILED,
    ClickHouseError: SqlErrorKind.DATABASE_UNAVAILABLE,
    ClickHouseQueryError: SqlErrorKind.SQL_FAILED,
}

TOOLS: Final = ToolMain.toolset(
    ch_list_tables,
    ch_list_columns,
    ch_describe_table,
    ch_query,
    ch_address,
    ch_database_describe,
    ch_table_describe,
    ch_column_describe,
    ch_constraints_describe,
    ch_indexes_describe,
    ch_function_describe,
    ch_sequences_describe,
    ch_types_describe,
    ch_edm_structure,
    ch_edm_descriptions,
    ch_stream_out,
    ch_stream_in,
    ch_arrow_out,
    ch_arrow_in,
)

if __name__ == "__main__":
    sys.exit(ToolMain.run(TOOLS))
