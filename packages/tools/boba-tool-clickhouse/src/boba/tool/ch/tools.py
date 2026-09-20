"""ClickHouse-инструменты: функции уровня модуля, модуль — обычная программа.

Запуск: `python -m boba.tool.ch.tools <имя> --флаги` — та же команда у
launcher'а приложения и у человека в терминале.

Ошибки:
ClickHouseError — до базы не достучаться (сеть, TLS, kerberos).
ClickHouseQueryError — сервер отклонил запрос (синтаксис, права).
UnknownConnectionError — имя подключения вне whitelist'а конфига.
AddressError — у профиля соединения нет базы по умолчанию для ch_address.
ChQueryError — сборщик получил один параметр с двумя разными значениями.
"""

from __future__ import annotations

import sys
from collections.abc import Mapping
from enum import StrEnum
from typing import Annotated, Any, ClassVar, Final, Self

from pydantic import Field

from boba.connections.address import AddressError
from boba.db.clickhouse import ClickHouseError, ClickHouseQueryError
from boba.db.clickhouse.address import ChAddresses
from boba.db.clickhouse.profile import ClickHouseConfig
from boba.toolkit.entry import ToolMain
from boba.toolkit.facade import UserConnection, tool
from boba.toolkit.result import MarkdownResult, SqlResult, TableResult
from boba.toolkit.sql import (
    AbstractQuery,
    RowLimit,
    RowOffset,
    RowPage,
    RowWindow,
    SqlErrorKind,
    SqlLimits,
)
from boba.toolkit.types import SecretRevealing

ChConnection = Annotated[ClickHouseConfig, UserConnection]

ChParams = dict[str, Any]
"""Серверные параметры ClickHouse: {name:Type} в тексте, значение в словаре."""

ChQuery = AbstractQuery[str, ChParams]
"""Собранный запрос: текст с {name:Type} плюс словарь параметров."""

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


class AddressColumn(StrEnum):
    """Колонки выдачи ch_address."""

    CONNECTION = "connection"
    URL = "url"


class ChQueryError(Exception):
    """Сборщик запроса получил противоречивые куски."""


class ChToolConfig(SecretRevealing, SqlLimits):
    """Лимиты выдачи ch-инструментов; [tool.ch]."""

    SECTION: ClassVar[str] = "tool.ch"
    ENGINE: ClassVar[str] = "clickhouse"
    """Подпись движка в SqlResult."""


class ChQueryBuilder:
    """Запрос кусками, которые инструмент добавляет по ходу своей логики.

    Подстановку делает сервер: в куске `{name:Type}` это параметр запроса
    ClickHouse, значение уезжает в словаре параметров, а идентификатор
    пишется как `{name:Identifier}`. Кусок с условием попадает в запрос
    только при истинном условии, так инструмент держит весь SQL у себя и
    решает, какие фильтры включить.
    """

    def __init__(self) -> None:
        self._parts: list[str] = []
        self._params: ChParams = {}

    def add(self, text: str, /, **bind: Any) -> Self:
        for name, value in bind.items():
            if name in self._params and self._params[name] != value:
                msg = (
                    f"query builder: parameter {name!r} bound twice with different "
                    f"values: {self._params[name]!r} and {value!r}"
                )
                raise ChQueryError(msg)

            self._params[name] = value

        self._parts.append(text)

        return self

    def when(self, condition: bool, text: str, /, **bind: Any) -> Self:
        if not condition:
            return self

        return self.add(text, **bind)

    def build(self) -> ChQuery:
        return ChQuery(text="\n".join(self._parts), params=dict(self._params))


def get_payload() -> Any:
    """Клиент базы: тянет clickhouse-connect, которого в приложении нет.

    Модуль инструмента читает хост ради объявлений, а драйвер живёт только
    в песочнице — поэтому импорт отложен до самого вызова.
    """
    from boba.db.clickhouse import payload  # noqa: PLC0415

    return payload.PayloadClickHouse


async def run_and_collect(
    connection: ClickHouseConfig,
    query: ChQuery,
    window: RowWindow,
) -> SqlResult:
    """Запрос страницей окна: границы выдачи назначает вызов."""
    page = RowPage(window)

    async with get_payload().row_blocks(connection, query.text, query.params) as stream:
        async for block in stream.blocks:
            if not page.add(dict(zip(stream.names, block, strict=True))):
                break

    return SqlResult(engine=ChToolConfig.ENGINE, statements=[page.statement()])


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
            from system.tables
            where database not in {system_databases:Array(String)}
            """,
            system_databases=SystemDatabase.names(),
        )
        .when(
            database is not None, "and database = {database:String}", database=database
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
            from system.columns
            where database not in {system_databases:Array(String)}
            """,
            system_databases=SystemDatabase.names(),
        )
        .when(
            database is not None, "and database = {database:String}", database=database
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
            from system.columns
            where database not in {system_databases:Array(String)}
              and table = {table:String}
            """,
            system_databases=SystemDatabase.names(),
            table=table,
        )
        .when(
            database is not None, "and database = {database:String}", database=database
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
            from system.databases
            where name not in {system_databases:Array(String)}
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
            from system.tables
            where database not in {system_databases:Array(String)}
            """,
            system_databases=SystemDatabase.names(),
        )
        .when(database != "*", "and database = {database:String}", database=database)
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
            from system.columns
            where database not in {system_databases:Array(String)}
            """,
            system_databases=SystemDatabase.names(),
        )
        .when(database != "*", "and database = {database:String}", database=database)
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
            from system.constraints
            where database not in {system_databases:Array(String)}
            """,
            system_databases=SystemDatabase.names(),
        )
        .when(database != "*", "and database = {database:String}", database=database)
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
            from system.data_skipping_indices
            where database not in {system_databases:Array(String)}
            """,
            system_databases=SystemDatabase.names(),
        )
        .when(database != "*", "and database = {database:String}", database=database)
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
            from system.functions
            where true
            """
        )
        .when(function != "*", "and name like {function:String}", function=function)
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
            from system.sequences
            where database not in {system_databases:Array(String)}
            """,
            system_databases=SystemDatabase.names(),
        )
        .when(database != "*", "and database = {database:String}", database=database)
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
            from system.data_type_families
            where true
            """
        )
        .when(name != "*", "and name like {name:String}", name=name)
        .add("order by name")
    )

    return await run_and_collect(
        connection, builder.build(), RowWindow(offset=offset, limit=limit)
    )


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
    ChQueryError: SqlErrorKind.SQL_FAILED,
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
)

if __name__ == "__main__":
    sys.exit(ToolMain.run(TOOLS))
