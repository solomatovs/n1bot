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
from collections.abc import Sequence
from enum import StrEnum
from typing import Annotated, Any, ClassVar, Final

from pydantic import Field

from boba.db.clickhouse.address import ChAddresses
from boba.db.clickhouse.connection import ClickHouseConfig
from boba.db.clickhouse.query import ChQuery, ChQueryBuilder
from boba.db.clickhouse.target import (
    ChCluster,
    ChPlacement,
    ChStreamWire,
    ChTableRef,
)
from boba.toolkit.contract import ColumnDeclaration
from boba.toolkit.entry import ToolMain
from boba.toolkit.facade import Injected, UserConnection, tool
from boba.toolkit.ports import (
    ChunkBytes,
    Inbound,
    Outbound,
    StreamGroup,
)
from boba.toolkit.result import MarkdownResult, SqlResult, SqlStatement, TableResult
from boba.toolkit.sql import (
    SqlLimits,
)
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
    TransferError,
    TransferFrame,
    TransferInbound,
    TransferOutbound,
    UnknownTypeStrategy,
)
from boba.toolkit.types import SecretRevealing
from boba.toolkit.window import RowLimit, RowOffset, RowPage, RowWindow

ChConnection = Annotated[ClickHouseConfig, UserConnection]

DatabaseFilter = Annotated[
    str,
    Field(
        min_length=1,
        description=(
            "Имя базы. * — все пользовательские базы (без system/information_schema). Можно искать по like %name%, name%, %name, name"
        ),
    ),
]
"""LLM-аргумент database: имя базы. * — все пользовательские базы (без system/information_schema). Можно искать по like %name%, name%, %name, name"""

TableFilter = Annotated[
    str,
    Field(
        min_length=1,
        description="Имя таблицы или view. искать по like %name%, name%, %name, name или все таблицы  = * ",
    ),
]
"""LLM-аргумент table: Имя таблицы или view. искать по Like %name%, name%, %name, name или все таблицы  = * """


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


class ChPump(StrEnum):
    """Имена насосов ClickHouse в журнале команд вызова."""

    STREAM_OUT = "ch_stream_out"
    STREAM_IN = "ch_stream_in"


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


async def run_steps(client: Any, steps: Sequence[str], journal: CommandJournal) -> None:
    """Стейтменты before/after насоса по одному, по порядку, тем же клиентом:
    в сессии клиента они делят SET и временные таблицы с командой насоса.
    Строки выборок не собираются, шаг даёт ответ сервера; каждый шаг —
    команда журнала вызова. client — AsyncClient драйвера: его пакет есть
    только внутри песочницы."""
    from boba.db.clickhouse.payload import PayloadClickHouse  # noqa: PLC0415

    for step in steps:
        query = ChQueryBuilder().raw_query(step).build()
        with journal.command(step, CommandKind.ACTION) as running:
            running.status = await PayloadClickHouse.command(
                client, query.text, query.params
            )


@tool
async def ch_query(
    sql: Annotated[
        str,
        Field(
            min_length=1,
            description=(
                """ADQM, ClickHouse, произвольный SQL. Строки выборки возвращаются.
                С окном выборки данных offset/limit.
                """
            ),
        ),
        MarkdownResult(language="sql"),
    ],
    connection: ChConnection,
    *,
    offset: RowOffset,
    limit: RowLimit,
) -> SqlResult:
    """ADQM, ClickHouse, произвольный SQL.
    Выполнить SQL на выбранном соединении: строки окном offset/limit
    """

    return await run_and_collect(
        connection,
        ChQuery(text=sql, params={}),
        RowWindow(offset=offset, limit=limit),
    )


'''
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
                and table = {table:String}
            """,
            table=table,
        )
        .when(
            database is None,
            "and database not in {system_databases:Array(String)}",
            system_databases=SystemDatabase.names(),
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
'''


@tool
async def ch_database_describe(
    connection: ChConnection,
    database: Annotated[
        DatabaseFilter,
        Field(
            min_length=1,
            description=(
                """Имя базы из system.databases. * — все пользовательские базы,
                доступные текущему пользователю. Конкретное имя — одна строка.
                С окном выборки данных offset/limit."""
            ),
        ),
    ],
    *,
    offset: RowOffset,
    limit: RowLimit,
) -> SqlResult:
    """Для БД ADQM и ClickHouse, SQL запрос, строки выборки возвращаются:
    описание баз данных кластера из system.databases.
    Колонки: address (db), name, engine, data_path, metadata_path, uuid, comment.
    С окном выборки данных offset/limit.
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
            where 1=1
            """,
        )
        .when(
            database == "*",
            "and name not in {system_databases:Array(String)}",
            system_databases=SystemDatabase.names(),
        )
        .when(database != "*", "and name like {database:String}", database=database)
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
    """Для БД ADQM и ClickHouse, SQL запрос, описание таблиц, строки выборки возвращаются:
    system.tables: таблицы, view, матвью, dictionary, distributed и пр.
    Колонки: address, database, name, engine, is_temporary, total_rows,
    total_bytes, partition_key, sorting_key, primary_key, sampling_key,
    storage_policy, metadata_modification_time, comment.
    С окном выборки данных offset/limit.
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
            where 1=1
            """,
        )
        .when(
            database == "*",
            "and database not in {system_databases:Array(String)}",
            system_databases=SystemDatabase.names(),
        )
        .when(database != "*", "and database = {database:String}", database=database)
        .when(table != "*", "and name like {table:String}", table=table)
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
    """Для БД ADQM и ClickHouse, SQL запрос, для описания колонок,
    строки выборки возвращаются: system.columns.
    Колонки: address, database, table, name, position, type, default_kind,
    default_expression, data_compressed_bytes, data_uncompressed_bytes,
    marks_bytes, is_in_partition_key, is_in_sorting_key, is_in_primary_key,
    is_in_sampling_key, compression_codec, comment.
    С окном выборки данных offset/limit.
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
            """,
        )
        .when(
            database == "*",
            "and database not in {system_databases:Array(String)}",
            system_databases=SystemDatabase.names(),
        )
        .when(database != "*", "and database = {database:String}", database=database)
        .when(table != "*", "and table like {table:String}", table=table)
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
    """Для БД ADQM и ClickHouse, SQL запрос, для поиска связей между таблицами, строки выборки возвращаются:
    ограничения из system.constraints (CHECK / ASSUME).
    Колонки: address, database, table, name, type (CHECK/ASSUME), expression.
    В ClickHouse нет PRIMARY/UNIQUE/FOREIGN как отдельных объектов — их роль
    исполняют ключи в system.tables.
    С окном выборки данных offset/limit.
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
            where 1=1
            """,
        )
        .when(
            database == "*",
            "and database not in {system_databases:Array(String)}",
            system_databases=SystemDatabase.names(),
        )
        .when(database != "*", "and database = {database:String}", database=database)
        .when(table != "*", "and table like {table:String}", table=table)
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
    """Для БД ADQM и ClickHouse, SQL запрос, для поиска связей между таблицами, строки выборки возвращаются:
    индексы пропуска данных из system.data_skipping_indices.
    Колонки: address, database, table, name, type (minmax/set/bloom_filter/
    ngrambf_v1/tokenbf_v1), expr, granularity, data_compressed_bytes,
    data_uncompressed_bytes. Первичный ключ смотрите в ch_table_describe.
    С окном выборки данных offset/limit.
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
            where 1=1
            """,
        )
        .when(
            database == "*",
            "and database not in {system_databases:Array(String)}",
            system_databases=SystemDatabase.names(),
        )
        .when(database != "*", "and database = {database:String}", database=database)
        .when(table != "*", "and table like {table:String}", table=table)
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
                """Шаблон имени функции в синтаксисе LIKE: `array%`, `%date%`.
                `*` — все функции, включая системные (их очень много, 
                используйте фильтр)."""
            ),
        ),
    ] = "*",
    *,
    offset: RowOffset,
    limit: RowLimit,
) -> SqlResult:
    """Для БД ADQM и ClickHouse, SQL запрос, для поиска связей между таблицами, строки выборки возвращаются:
    функции из system.functions.
    Колонки: address, name, is_aggregate, case_insensitive, alias_to, origin
    (System/User/…), syntax, arguments, returned_value, description,
    categories. В ClickHouse нет процедур — есть встроенные и UDF-функции.
    С окном выборки данных offset/limit.
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
    """Для БД ADQM и ClickHouse, SQL запрос, для поиска связей между таблицами, строки выборки возвращаются:
    последовательности из system.sequences.
    Колонки: address, database, name, uuid, start_value, increment,
    min_value, max_value, cycle, cache, comment. На старых версиях таблицы
    нет — запрос упадёт с ошибкой сервера.
    С окном выборки данных offset/limit.
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
            where 1=1
            """,
        )
        .when(
            database == "*",
            "and database not in {system_databases:Array(String)}",
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
    """Для БД ADQM и ClickHouse, SQL запрос, для поиска связей между таблицами, строки выборки возвращаются:
    типы данных из system.data_type_families.
    Колонки: address, name, case_insensitive, alias_to. В ClickHouse нет
    enum/domain/composite; Enum-типы описываются прямо в колонке — смотрите
    ch_column_describe.
    С окном выборки данных offset/limit.
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
                "Запрос SELECT целиком, без FORMAT и без `;`; выполняется как "
                "написан. Для arrow строки — с settings "
                "output_format_arrow_string_as_string = 1, иначе они уедут "
                "двоичными."
            ),
        ),
        MarkdownResult(language="sql"),
    ],
    wire: Annotated[
        ChStreamWire,
        Field(
            description=(
                "Формат данных в потоке:\n"
                "   - tsv — текст TabSeparated, типы ClickHouse как есть\n"
                "       для приёмников ClickHouse и postgres: точная передача типов\n"
                "   - arrow — универсальный поток в формате Arrow IPC\n"
                "       для приёмников других движков и узлов преобразования\n"
            ),
        ),
    ],
    columns: Annotated[
        Sequence[ColumnDeclaration],
        Field(
            description=(
                "Декларации колонок поверх типов ответа, только для arrow: "
                "заданное перекрывает найденное, незаданное остаётся от "
                "сервера. У tsv типы ClickHouse едут как есть, деклараций нет."
            ),
        ),
    ] = (),
    chunk_bytes: ChunkBytes = 262144,
    before: BeforeSteps = (),
    after: AfterSteps = (),
    *,
    out: Annotated[Outbound[TransferFrame], Injected],
) -> SqlResult:
    """Источник sync-потока: строки запроса с контрактом колонок для приёмника.

    Запрос выполняется один раз. tsv: ответ идёт в
    TabSeparatedWithNamesAndTypes, две строки шапки уходят кадром schema как
    контракт с текстами типов ClickHouse, остальные байты — кадрами rows как
    TabSeparated. arrow: ответ идёт в ArrowStream, первый кадр — контракт из
    его схемы с декларациями columns поверх, дальше те же пачки Arrow IPC.
    Стейтменты before и after идут в той же сессии сервера до и после
    запроса. В ответ — состав контракта, сводка сервера и шаги скриптов.
    """
    from boba.db.clickhouse.arrow_stream import ChArrowSource  # noqa: PLC0415
    from boba.db.clickhouse.payload import PayloadClickHouse  # noqa: PLC0415
    from boba.db.clickhouse.transfer import ChTsvOut  # noqa: PLC0415

    if wire is ChStreamWire.TSV and columns:
        raise TransferError(
            "ch_stream_out: columns apply to wire arrow only; with tsv the types "
            "of clickhouse travel as they are"
        )

    journal = CommandJournal(ChPump.STREAM_OUT)
    statement = ChQueryBuilder().raw_query(sql).build()
    outbound = TransferOutbound(out)
    try:
        async with PayloadClickHouse.opened_for_scripts(
            connection, before, after
        ) as client:
            await run_steps(client, before, journal)
            match wire:
                case ChStreamWire.TSV:
                    report = await ChTsvOut(client, journal).stream(
                        statement.text, chunk_bytes, outbound
                    )
                case ChStreamWire.ARROW:
                    report = await ChArrowSource(client, journal).stream(
                        statement.text, columns, chunk_bytes, outbound
                    )

            await run_steps(client, after, journal)
    except Exception as exc:
        raise journal.failed(exc) from exc

    statements = [report.sql_statement(), *journal.statements()]

    return SqlResult(engine=ChToolConfig.ENGINE, statements=statements)


@tool
async def ch_stream_in(  # noqa: PLR0913
    connection: ChConnection,
    database: Annotated[str, Field(min_length=1, description="База таблицы-приёмника")],
    table_name: Annotated[
        str, Field(min_length=1, description="Таблица-приёмник в базе")
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
                "Какие прежние строки убрать, объект с kind:\n"
                "   - nothing — оставить все\n"
                "   - truncate — не оставить ни одной\n"
                "   - delete_all — не оставить ни одной, со счётчиком строк\n"
                "   - delete_where — убрать строки по условию в поле where\n"
                "Приёмник грузит в двойник <table>__ex, переносит туда\n"
                "оставшиеся строки и меняет таблицы местами exchange tables:\n"
                "читатели не видят частичной загрузки, прежняя версия\n"
                "остаётся в __ex.\n"
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
    rules: Annotated[
        ColumnRules,
        Field(
            description=(
                "Правила колонок приёмника:\n"
                "   - rename_columns — {колонка приёмника: поле потока}\n"
                "       только имя, данные не меняются\n"
                "   - column_types — {колонка приёмника: тип ClickHouse текстом}\n"
                "       перекрывает тип из потока и стратегию unknown_types\n"
            ),
        ),
    ] = ColumnRules(),
    unknown_types: Annotated[
        UnknownTypeStrategy,
        Field(
            description=(
                "Что делать с колонкой, для которой у ClickHouse нет типа, "
                "объект с kind:\n"
                "   - fail_on_unknown — ошибка с типом потока\n"
                "   - fallback_as_varchar — колонка получает String\n"
                "       явный rules.column_types перекрывает оба варианта\n"
            ),
        ),
    ] = FailOnUnknown(kind="fail_on_unknown"),
    cluster: Annotated[
        str,
        Field(
            description=(
                "Кластер из system.clusters, на котором идут DDL приёмника:\n"
                "   - имя — create, drop, rename, двойник и exchange tables\n"
                "       выполняются on cluster; вставка — на узел соединения\n"
                "   - пусто — без кластера\n"
                "ReplicatedMergeTree шаблона по умолчанию без кластера сервер не "
                "создаст: укажите кластер или шаблон с MergeTree.\n"
            ),
        ),
    ] = "",
    order_by: Annotated[
        str,
        Field(
            min_length=1,
            description=(
                "Ключ сортировки создаваемой таблицы, выражение как в SQL:\n"
                "   - id\n"
                "   - (dt, id)\n"
                "   - tuple() — без сортировки\n"
                "Колонки ключа не могут быть Nullable: объявите их not null у "
                "источника.\n"
            ),
        ),
    ] = ChTableRef.ORDER_BY,
    create_table: Annotated[
        str,
        Field(
            min_length=1,
            description=(
                "Шаблон create table, когда стратегия схемы создаёт таблицу: "
                "цельный стейтмент, в который подставляются только названные в "
                "нём переменные:\n"
                "   - {database} — база приёмника, экранированная\n"
                "   - {table_name} — имя таблицы, экранированное\n"
                "   - {columns} — колонки с типами из плана\n"
                "   - {order_by} — ключ сортировки из параметра order_by\n"
                "   - {cluster} — кластер из параметра cluster\n"
                "Часть в квадратных скобках [ on cluster {cluster}] выпадает "
                "целиком, если значения её переменной нет. Сюда пишутся engine, "
                "partition by, settings. Литеральные фигурные и квадратные "
                "скобки удваиваются.\n"
            ),
        ),
    ] = ChTableRef.CREATE_TABLE,
    before: BeforeSteps = (),
    after: AfterSteps = (),
    *,
    feed: Annotated[Inbound[TransferFrame], Injected],
    group: Annotated[StreamGroup, Injected],
) -> SqlResult:
    """Приёмник ClickHouse со стратегиями: поток любого источника в таблицу.

    Таблица по умолчанию — ReplicatedMergeTree с ключом order_by, DDL идут
    on cluster, если передан cluster. Поток arrow любого источника
    разбирается нейтральным контрактом:
    семейства без своего типа у ClickHouse (json, inet, interval, bytea)
    ложатся String, остальное — родными типами. Другие форматы берёт пара
    «движок источника -> ClickHouse» из реестра. База обязана быть Atomic:
    загрузка идёт в двойник <table>__ex и заканчивается exchange tables
    после барьера группы связанных вызовов.
    Стейтменты before и after идут в той же сессии сервера. В ответ — что
    сделано со схемой и почему, сверка по колонкам, что удалено, сколько
    вставлено.
    """
    from boba.db.clickhouse.arrow_stream import ChArrowLoader  # noqa: PLC0415
    from boba.db.clickhouse.payload import PayloadClickHouse  # noqa: PLC0415
    from boba.db.clickhouse.transfer import ChTransfers  # noqa: PLC0415
    from boba.toolkit.contract import ArrowContract, StreamContract  # noqa: PLC0415
    from boba.toolkit.contract import Engine as NeutralEngine  # noqa: PLC0415

    journal = CommandJournal(ChPump.STREAM_IN)
    payload = PayloadClickHouse
    template = CreateTemplate(create_table, ChTableRef.TEMPLATE_VARS)
    placement = ChPlacement(cluster=ChCluster(cluster), order_by=order_by)
    inbound = TransferInbound(feed, group)
    table = ChTableRef(database=database, name=table_name)
    try:
        head = await inbound.get_schema()
        async with payload.opened_for_scripts(connection, before, after) as client:
            await run_steps(client, before, journal)
            if head.wire is StreamWire.ARROW:
                contract = ArrowContract.model_validate(head.contract)
                loader = ChArrowLoader(
                    client,
                    table,
                    placement,
                    StreamContract().specs(contract.columns),
                    NeutralEngine(head.source_engine.value),
                    inbound,
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
            else:
                pair = ChTransfers.discover().pair(head.source_engine)
                report = await pair(
                    client, table, placement, head, inbound, journal
                ).run(
                    schema_strategy,
                    delete_strategy,
                    insert_strategy,
                    unknown_types,
                    rules,
                    template,
                )

            await run_steps(client, after, journal)
    except Exception as exc:
        raise journal.failed(exc) from exc

    statements = [report.summary(), *journal.statements()]

    return SqlResult(engine=ChToolConfig.ENGINE, statements=statements)


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


@tool
async def ch_edm_general_describe(
    connection: ChConnection,
    database: DatabaseFilter,
    table: TableFilter,
    system_name: Annotated[
        str,
        Field(
            min_length=1,
            description=(
                """Название системы в формате LIKE %system_name%, * — все системы"""
            ),
        ),
    ],
    *,
    offset: RowOffset,
    limit: RowLimit,
) -> SqlResult:
    """ ""Эталонные описания метаданных из ЕДМ (EDM) в ADQM из БД cmn_cds.
    С окном выборки данных offset/limit.
    Выдача постраничная: сколько показано и как листать, сказано в note.
    """
    builder = (
        ChQueryBuilder()
        .add(
            """
-- описания из ЕДМ
with w_pdm_table as (
select  al.etalon_id table_etalon_id 
      ,a.path table_path
      ,maxIf(al.value, a.type = 'pdm_table' and al.attribute_id = 'name') as table_name
      ,maxIf(al.value, al.attribute_id = 'short_description_edm') as table_short_description_edm
      ,maxIf(al.value, al.attribute_id = 'extended_description_edm') as table_extended_description_edm
      ,maxIf(al.value, al.attribute_id = 'description') as table_description_from_source
  from cmn_cds.dp_edm__com_dg_export_data__attribute_list_physical_current_versions al
         inner join cmn_cds.dp_edm__com_dg_export_data__assets_current_versions a 
           on a.id = al.etalon_id
where al.attribute_id in ('short_description_edm', 'extended_description_edm', 'description', 'name')
and a.type  in ('pdm_table')
--and path like '%ods_cnmd_gpn_journal%'
group by a.path, al.etalon_id)
, w_pdm_col as (
select  al.etalon_id column_etalon_id
      ,a.path column_path
      ,maxIf(al.value, al.attribute_id = 'name') as column_name
      ,maxIf(al.value, al.attribute_id = 'short_description_edm') as column_short_description_edm
      ,maxIf(al.value, al.attribute_id = 'extended_description_edm') as column_extended_description_edm
      ,maxIf(al.value, al.attribute_id = 'description') as column_description_from_source
  from cmn_cds.dp_edm__com_dg_export_data__attribute_list_physical_current_versions al
         inner join cmn_cds.dp_edm__com_dg_export_data__assets_current_versions a 
           on a.id = al.etalon_id
where al.attribute_id in ('short_description_edm', 'extended_description_edm', 'description', 'name')
and a.type  in ('pdm_table_column')
--and path like '%ods_cnmd_gpn_journal%'
group by a.path, al.etalon_id
)
, w_pdm as (
 select * from w_pdm_table t1
   inner join w_pdm_col c1 on c1.column_path = t1.table_path||'/'||table_name  )
, w_ed as (
      select r.etalon_id_from as etalon_id_ed
            ,r.etalon_id_to as etalon_id_pdm
            ,a.value as ed_name
        from cmn_cds.dp_edm__com_dg_export_data__relations_current_version r
         inner join cmn_cds.dp_edm__com_dg_export_data__attribute_list_current_versions a 
           on a.etalon_id = r.etalon_id_from
          and a.attribute_id in ('ed_entity_name', 'ed_attribute_name')
      where r.name = 'lnk_ldm_physical_relationship'       
)
, w_conn as (
      select a.etalon_id
            ,splitByChar('_', COALESCE(a.value, '_'))[4] as host
            ,CASE splitByChar('_', COALESCE(a.value, '_'))[3]
                  WHEN 'pg' THEN 5432
                  WHEN 'adb' THEN 5432
                  WHEN 'adqm' THEN 8443
                  WHEN 'oracle' THEN 1521
                  WHEN 'mssql' THEN 1433
                  WHEN '1c' THEN 443
                  ELSE 443
             END as port
            ,splitByChar('_', COALESCE(replace(a.value, 'gazprom-neft', ''), '_'))[5] as db
            ,CASE splitByChar('_', COALESCE(a.value, '_'))[3]
                  WHEN 'pg' THEN 'postgres'
                  WHEN 'adb' THEN 'postgres'
                  WHEN 'adqm' THEN 'clickhouse'
                  WHEN 'oracle' THEN 'oracle'
                  WHEN 'mssql' THEN 'mssql'
                  WHEN '1c' THEN 'https'
                  ELSE 'https'
             END as source_type
        from cmn_cds.dp_edm__com_dg_export_data__attribute_list_physical_current_versions a
      where a.attribute_id = 'connection'
),
 q1 as (
select pdm.*
      ,c1.source_type || '://' || c1.host || ':' || c1.port || '/' || c1.db as table_connection
      ,ed_tab.ed_name table_ed_name
      ,ed_col.ed_name column_ed_name
  from w_pdm pdm
         left join w_ed ed_tab
           on ed_tab.etalon_id_pdm = pdm.table_etalon_id
        left join w_conn c1 on c1.etalon_id = pdm.table_etalon_id
		left join w_ed ed_col
           on ed_col.etalon_id_pdm = pdm.column_etalon_id
           ) 
select * from q1
where (table_short_description_edm is not null 
or table_extended_description_edm is not null
or table_description_from_source is not null 
or table_ed_name is not null 
or column_short_description_edm is not null 
or column_extended_description_edm is not null
or column_description_from_source is not null 
or column_ed_name is not null )
""",
        )
        .when(
            database != "*", "and column_path like {database:String}", database=database
        )
        .when(table != "*", "and column_path like {table:String}", table=table)
        .when(
            system_name != "*",
            "and column_path like {system_name:String}",
            system_name=system_name,
        )
        .add("order by column_path")
    )

    return await run_and_collect(
        connection, builder.build(), RowWindow(offset=offset, limit=limit)
    )


@tool
async def ch_edm_table_describe(
    connection: ChConnection,
    database: DatabaseFilter,
    table: TableFilter,
    system_name: Annotated[
        str,
        Field(
            min_length=1,
            description=(
                """Название системы в формате LIKE %system_name%, * — все системы"""
            ),
        ),
    ],
    *,
    offset: RowOffset,
    limit: RowLimit,
) -> SqlResult:
    """Описания метаданных из ЕДМ (EDM) в ADQM из БД cmn_cds, структура таблиц.
    С окном выборки данных offset/limit.
    Выдача постраничная: сколько показано и как листать, сказано в note.
    """
    builder = (
        ChQueryBuilder()
        .add(
            """
with w_name as (
select * from cmn_cds.dp_edm__com_dg_export_data__attribute_list_physical_current_versions
where attribute_id = 'name'
) , w_conn as (
      select a.etalon_id
            ,splitByChar('_', COALESCE(a.value, '_'))[4] as host
            ,CASE splitByChar('_', COALESCE(a.value, '_'))[3]
                  WHEN 'pg' THEN 5432
                  WHEN 'adb' THEN 5432
                  WHEN 'adqm' THEN 8443
                  WHEN 'oracle' THEN 1521
                  WHEN 'mssql' THEN 1433
                  WHEN '1c' THEN 443
                  ELSE 443
             END as port
            ,splitByChar('_', COALESCE(replace(a.value, 'gazprom-neft', ''), '_'))[5] as db
            ,CASE splitByChar('_', COALESCE(a.value, '_'))[3]
                  WHEN 'pg' THEN 'postgres'
                  WHEN 'adb' THEN 'postgres'
                  WHEN 'adqm' THEN 'clickhouse'
                  WHEN 'oracle' THEN 'oracle'
                  WHEN 'mssql' THEN 'mssql'
                  WHEN '1c' THEN 'https'
                  ELSE 'https'
             END as source_type
        from cmn_cds.dp_edm__com_dg_export_data__attribute_list_physical_current_versions a
      where a.attribute_id = 'connection'
)
select r.etalon_id_to as etalon_id
     , r.etalon_id_from as etalon_id_parent
     , a.path || '/' || obn.value as path
     , c.source_type || '://' || c.host || ':' || c.port || '/' || c.db as conn_str
     , obn.value as table_name
     , an.value as column_name
  from cmn_cds.dp_edm__com_dg_export_data__relations_current_version r
         inner join cmn_cds.dp_edm__com_dg_export_data__assets_current_versions a 
           on a.id = r.etalon_id_from
         inner join cmn_cds.dp_edm__com_dg_export_data__relation_types rtl 
           on rtl.relation_type_id = r.relation_type_id
            and rtl.is_inner = 1
         inner join w_name obn 
           on obn.etalon_id = r.etalon_id_from
         inner join w_name an 
           on an.etalon_id = r.etalon_id_to
         left join w_conn c on c.etalon_id = r.etalon_id_from
where 1=1
   and rtl.type_to in ('pdm_table_column', 'pdm_view_column')
   and rtl.type_from in ('pdm_table', 'pdm_view')""",
        )
        .when(database != "*", "and path like {database:String}", database=database)
        .when(table != "*", "and path like {table:String}", table=table)
        .when(
            system_name != "*",
            "and path like {system_name:String}",
            system_name=system_name,
        )
        .add("order by path")
    )

    return await run_and_collect(
        connection, builder.build(), RowWindow(offset=offset, limit=limit)
    )


TOOLS: Final = ToolMain.toolset(
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
    ch_types_describe,
    ch_edm_general_describe,
    ch_edm_table_describe,
    ch_edm_structure,
    ch_edm_descriptions,
    ch_stream_out,
    ch_stream_in,
)

if __name__ == "__main__":
    sys.exit(ToolMain.run(TOOLS))
