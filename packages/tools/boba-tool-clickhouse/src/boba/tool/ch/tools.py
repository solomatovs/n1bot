"""ClickHouse-инструменты: функции уровня модуля, модуль — обычная программа.

Запуск: `python -m boba.tool.ch.tools <имя> --флаги` — та же команда у
launcher'а приложения и у человека в терминале.

Ошибки:
ClickHouseError — до базы не достучаться (сеть, TLS, kerberos).
ClickHouseQueryError — сервер отклонил запрос (синтаксис, права).
UnknownConnectionError — имя подключения вне whitelist'а конфига.
ResultTooLargeError — выдача превысила max_bytes конфига.
AddressError — у профиля соединения нет базы по умолчанию для ch_address.
"""

from __future__ import annotations

import logging
import random
import string
import sys
from collections.abc import Mapping
from enum import StrEnum
from typing import (
    Annotated,
    Any,
    ClassVar,
    Final,
    Self,
    TypeVar,
)

from pydantic import BaseModel, Field, PrivateAttr

from boba.connections.address import AddressError
from boba.db.clickhouse import ClickHouseError, ClickHouseQueryError
from boba.db.clickhouse.address import ChAddresses
from boba.db.clickhouse.profile import ClickHouseConfig
from boba.toolkit.entry import ToolMain
from boba.toolkit.facade import UserConnection, tool
from boba.toolkit.result import (
    MarkdownResult,
    ResultTooLargeError,
    SqlResult,
    TableResult,
)
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

logger = logging.getLogger(__name__)

ChParams = dict[str, Any]
"""Именованные параметры ClickHouse под подстановку {name:Type}."""


ChConnection = Annotated[ClickHouseConfig, UserConnection]


def get_payload() -> Any:
    """Клиент базы: тянет clickhouse-connect, которого в приложении нет.

    Модуль инструмента читает хост ради объявлений, а драйвер живёт только
    в песочнице — поэтому импорт отложен до самого вызова.
    """
    from boba.db.clickhouse import payload  # noqa: PLC0415

    return payload.PayloadClickHouse


class AddressColumn(StrEnum):
    """Колонки выдачи ch_address."""

    CONNECTION = "connection"
    URL = "url"


class ChToolConfig(SecretRevealing, SqlLimits):
    """Лимиты выдачи ch-инструментов; [tool.ch]."""

    SECTION: ClassVar[str] = "tool.ch"
    ENGINE: ClassVar[str] = "clickhouse"
    """Подпись движка в SqlResult."""


T = TypeVar("T")


class ChFieldQueryBuilder(BaseModel):
    param_name: str
    bind_type: str
    compare: str
    val: Any
    alias: str | None = None
    prefix_condition: str | None = Field(default="and")

    _param_attr_name: str = PrivateAttr()
    _param_value_name: str = PrivateAttr()

    @classmethod
    def random_name(cls, length: int) -> str:
        return "".join(
            random.choices(  # noqa: S311
                string.ascii_letters,
                k=length,
            )
        )

    def model_post_init(self, __context: Any) -> None:
        self._param_attr_name = self.random_name(8)
        self._param_value_name = self.random_name(8)

    def get_param_placeholder(self):
        return f"{{{self._param_value_name}:{self.bind_type}}}"

    def get_attr_placeholder(self):
        alias = ""
        if self.alias:
            alias = f"{self.alias}."

        res = f"{{{self._param_attr_name}:Identifier}}"
        return f"{alias}{res}"

    def get_prefix_condition(self):
        return self.prefix_condition or ""

    def get_compare(self):
        return self.compare

    def get_parameters(self) -> dict[str, Any]:
        return {
            self._param_attr_name: self.param_name,
            self._param_value_name: self.val,
        }


class ChQueryBuilder:
    def __init__(self, query: str) -> None:
        self._query = query
        self._condition_filters: list[ChFieldQueryBuilder] = []

    def condition(self, f: ChFieldQueryBuilder) -> Self:
        self._condition_filters.append(f)
        return self

    def build(self) -> AbstractQuery[str, ChParams]:
        """Формирует запрос для получения списка databases"""
        base_query = self._query
        condition = ["where 1=1"]
        params: ChParams = {}

        for c in self._condition_filters:
            prefix_condition = c.get_prefix_condition()

            condition.append(
                f"{prefix_condition} {c.get_attr_placeholder()} "
                f"{c.get_compare()} {c.get_param_placeholder()}"
            )

            params.update(c.get_parameters())

        text = base_query.format_map(
            {
                "condition": "\n\t".join(condition),
            }
        )

        return AbstractQuery(
            text=text,
            params=params,
        )


def set_database_filter_bootstrap(
    builder: ChQueryBuilder,
    param_name: str,
    bind_type: str,
    compare: str,
    database: str | None,
):
    """
    Заполняет QueryBuilder фильтрацией по базам данных
    Типичная для многих инструментов
    """

    builder.condition(
        ChFieldQueryBuilder(
            param_name=param_name,
            bind_type="Array(String)",
            compare="not in",
            val=[
                "system",
                "INFORMATION_SCHEMA",
                "information_schema",
            ],
        )
    )

    if database and database != "*":
        builder.condition(
            ChFieldQueryBuilder(
                param_name=param_name,
                bind_type=bind_type,
                compare=compare,
                val=database,
            )
        )


async def run_and_collect(
    connection: ClickHouseConfig,
    query: AbstractQuery[str, ChParams],
    window: RowWindow,
) -> SqlResult:
    """Каталожный запрос страницей окна: границы выдачи назначает вызов."""
    parameters = query.params
    if not parameters:
        parameters = None

    page = RowPage(window)

    async with get_payload().row_blocks(
        connection,
        query.text,
        query.params,
    ) as stream:
        async for block in stream.blocks:
            if not page.add(dict(zip(stream.names, block, strict=True))):
                break

    return SqlResult(
        engine=ChToolConfig.ENGINE,
        statements=[page.statement()],
    )


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
    """Список таблиц/view подключения. Колонки: database, table, engine.

    Выдача постраничная: сколько показано и как листать, сказано в note.
    """
    builder = ChQueryBuilder("""
        select
            database,
            name as table,
            engine,
            total_rows
        from
            system.tables
        {condition}
        order by
            database,
            name
    """)

    set_database_filter_bootstrap(
        builder,
        "database",
        "String",
        "=",
        database,
    )

    return await run_and_collect(
        connection,
        builder.build(),
        RowWindow(
            offset=offset,
            limit=limit,
        ),
    )


@tool
async def ch_list_columns(
    connection: ChConnection,
    database: Annotated[
        str | None,
        Field(
            description=(
                "Опциональный фильтр по базе (например `default`). "
                "Пусто = все пользовательские базы "
            ),
        ),
    ] = None,
    table: Annotated[
        str | None,
        Field(
            description=(
                "Опциональный фильтр по таблице (например `default`). "
                "Пусто = все пользовательские базы "
            ),
        ),
    ] = None,
    *,
    offset: RowOffset,
    limit: RowLimit,
) -> SqlResult:
    """Список таблиц/view подключения. Колонки: database, table, engine.

    Выдача постраничная: сколько показано и как листать, сказано в note.
    """
    builder = ChQueryBuilder("""
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
        {condition}
        order by
            database,
            table,
            position
    """)

    set_database_filter_bootstrap(
        builder,
        "database",
        "String",
        "=",
        database,
    )

    if table and table != "*":
        builder.condition(
            ChFieldQueryBuilder(
                param_name="table",
                bind_type="String",
                compare="=",
                val=table,
            )
        )

    return await run_and_collect(
        connection,
        builder.build(),
        RowWindow(
            offset=offset,
            limit=limit,
        ),
    )


@tool
async def ch_query(
    sql: Annotated[
        str,
        Field(
            min_length=1,
            description=(
                "Произвольный SQL ClickHouse. Если строк больше лимита "
                "— добавьте LIMIT в сам запрос."
            ),
        ),
        MarkdownResult(language="sql"),
    ],
    connection: ChConnection,
) -> SqlResult:
    """Выполнить SQL на выбранном соединении."""

    return await run_and_collect(
        connection,
        AbstractQuery(text=sql, params={}),
        RowWindow(
            offset=0,
            limit=None,
        ),
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
            description="База таблицы; пусто — база по умолчанию у подключения.",
        ),
    ] = None,
    *,
    offset: RowOffset,
    limit: RowLimit,
) -> SqlResult:
    """Схема таблицы: колонки, типы, default-выражения, комментарии.

    Широкая таблица приходит частями: как листать, сказано в note.
    """
    builder = ChQueryBuilder("""
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
        {condition}
        order by
            database,
            table,
            position
    """)

    set_database_filter_bootstrap(
        builder,
        "database",
        "String",
        "=",
        database,
    )

    if table and table != "*":
        builder.condition(
            ChFieldQueryBuilder(
                param_name="table",
                bind_type="String",
                compare="=",
                val=table,
            )
        )

    return await run_and_collect(
        connection,
        builder.build(),
        RowWindow(
            offset=offset,
            limit=limit,
        ),
    )


@tool
async def ch_database_discribe(
    connection: ChConnection,
    database: Annotated[
        str,
        Field(
            min_length=1,
            description=(
                "Имя базы из system.databases. `*` — все базы кластера, "
                "доступные текущему пользователю. Конкретное имя — "
                "одна строка."
            ),
        ),
    ],
    *,
    offset: RowOffset,
    limit: RowLimit,
) -> SqlResult:
    """Для ClickHouse и ADQM - Описание баз данных кластера из system.databases.
    Колонки: address (db), name, engine, data_path, metadata_path, uuid,
    comment. Одна строка на базу. Выдача постраничная — как листать,
    сказано в note.
    """
    builder = ChQueryBuilder("""
        select
            name        as address,
            name        as name,
            engine,
            data_path,
            metadata_path,
            uuid,
            comment
        from
            system.databases
        {condition}
        order by
            name
    """)

    set_database_filter_bootstrap(
        builder,
        "name",
        "String",
        "=",
        database,
    )

    return await run_and_collect(
        connection,
        builder.build(),
        RowWindow(
            offset=offset,
            limit=limit,
        ),
    )


@tool
async def ch_table_discribe(
    connection: ChConnection,
    database: Annotated[
        str,
        Field(
            min_length=1,
            description=(
                "Имя базы (схемы). `*` — все пользовательские базы "
                "(без system/information_schema)."
            ),
        ),
    ],
    table: Annotated[
        str,
        Field(
            min_length=1,
            description=(
                "Имя таблицы/view. `*` — все отношения базы. "
                "Широкая выдача — сузьте фильтр или увеличьте max_rows."
            ),
        ),
    ],
    *,
    offset: RowOffset,
    limit: RowLimit,
) -> SqlResult:
    """ADQM, ClickHouse, описание таблиц из system.tables: таблицы, view, матвью,
    dictionary, distributed и пр.
    Колонки: address, database, name, engine, is_temporary, total_rows,
    total_bytes, partition_key, sorting_key, primary_key, sampling_key,
    storage_policy, comment. Выдача постраничная — как листать, сказано
    в note.
    """
    builder = ChQueryBuilder("""
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
        {condition}
        order by
            database,
            name
    """)

    set_database_filter_bootstrap(
        builder,
        "database",
        "String",
        "=",
        database,
    )

    if table and table != "*":
        builder.condition(
            ChFieldQueryBuilder(
                param_name="name",
                bind_type="String",
                compare="=",
                val=table,
            )
        )

    return await run_and_collect(
        connection,
        builder.build(),
        RowWindow(
            offset=offset,
            limit=limit,
        ),
    )


@tool
async def ch_column_discribe(
    connection: ChConnection,
    database: Annotated[
        str,
        Field(
            min_length=1,
            description=(
                "Имя базы. `*` — все пользовательские базы "
                "(без system/information_schema)."
            ),
        ),
    ],
    table: Annotated[
        str,
        Field(
            min_length=1,
            description=("Имя отношения (таблица/view). `*` — все отношения базы."),
        ),
    ],
    *,
    offset: RowOffset,
    limit: RowLimit,
) -> SqlResult:
    """ADQM, ClickHouse, описания полей, колонок, из system.columns.
    Колонки: address, database, table, name, position, type,
    default_kind, default_expression, data_compressed_bytes,
    data_uncompressed_bytes, marks_bytes, is_in_partition_key,
    is_in_sorting_key, is_in_primary_key, is_in_sampling_key,
    compression_codec, comment. Для широких таблиц выдача приходит
    частями — как листать, сказано в note.
    """
    builder = ChQueryBuilder("""
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
        {condition}
        order by
            database,
            table,
            position
    """)

    set_database_filter_bootstrap(
        builder,
        "database",
        "String",
        "=",
        database,
    )

    if table and table != "*":
        builder.condition(
            ChFieldQueryBuilder(
                param_name="table",
                bind_type="String",
                compare="=",
                val=table,
            )
        )

    return await run_and_collect(
        connection,
        builder.build(),
        RowWindow(
            offset=offset,
            limit=limit,
        ),
    )


@tool
async def ch_constraints_discribe(
    connection: ChConnection,
    database: Annotated[
        str,
        Field(
            min_length=1,
            description=("Имя базы. `*` — все пользовательские базы."),
        ),
    ],
    table: Annotated[
        str,
        Field(
            min_length=1,
            description="Имя таблицы. `*` — все таблицы базы.",
        ),
    ],
    *,
    offset: RowOffset,
    limit: RowLimit,
) -> SqlResult:
    """ADQM, ClickHouse, описание ограничений из system.constraints (CHECK / ASSUME).
    Колонки: address, database, table, name, type (CHECK/ASSUME),
    expression. В ClickHouse нет PRIMARY/UNIQUE/FOREIGN как отдельных
    объектов — их роль исполняют ключи в system.tables.
    """
    builder = ChQueryBuilder("""
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
        {condition}
        order by
            database,
            table,
            name
    """)

    set_database_filter_bootstrap(
        builder,
        "database",
        "String",
        "=",
        database,
    )

    if table and table != "*":
        builder.condition(
            ChFieldQueryBuilder(
                param_name="table",
                bind_type="String",
                compare="=",
                val=table,
            )
        )

    return await run_and_collect(
        connection,
        builder.build(),
        RowWindow(
            offset=offset,
            limit=limit,
        ),
    )


@tool
async def ch_indexes_discribe(
    connection: ChConnection,
    database: Annotated[
        str,
        Field(
            min_length=1,
            description="Имя базы. `*` — все пользовательские базы.",
        ),
    ],
    table: Annotated[
        str,
        Field(
            min_length=1,
            description="Имя таблицы. `*` — все таблицы базы.",
        ),
    ],
    *,
    offset: RowOffset,
    limit: RowLimit,
) -> SqlResult:
    """ADQM, ClickHouse, описание индексов пропуска данных из
    system.data_skipping_indices.
    Колонки: address, database, table, name, type (minmax/set/bloom_filter/
    ngrambf_v1/tokenbf_v1), expr, granularity, data_compressed_bytes,
    data_uncompressed_bytes. Первичный ключ смотрите в ch_table_discribe.
    """
    builder = ChQueryBuilder("""
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
        {condition}
        order by
            database,
            table,
            name
    """)

    set_database_filter_bootstrap(
        builder,
        "database",
        "String",
        "=",
        database,
    )

    if table and table != "*":
        builder.condition(
            ChFieldQueryBuilder(
                param_name="table",
                bind_type="String",
                compare="=",
                val=table,
            )
        )

    return await run_and_collect(
        connection,
        builder.build(),
        RowWindow(
            offset=offset,
            limit=limit,
        ),
    )


@tool
async def ch_function_discribe(
    connection: ChConnection,
    function: Annotated[
        str,
        Field(
            min_length=1,
            description=(
                "Шаблон имени функции в синтаксисе LIKE: `array%`, "
                "`%date%`. `*` — все функции, включая системные "
                "(их очень много, используйте фильтр)."
            ),
        ),
    ] = "*",
    *,
    offset: RowOffset,
    limit: RowLimit,
) -> SqlResult:
    """ADQM, ClickHouse, функции из system.functions.
    Колонки: name, is_aggregate, case_insensitive, alias_to, origin
    (System/User/…), syntax, arguments (строка), returned_value,
    description, categories. В ClickHouse нет процедур/хранимых
    программ  — есть встроенные и UDF-функции.
    """
    builder = ChQueryBuilder("""
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
        {condition}
        order by
            name
    """)

    if function and function != "*":
        builder.condition(
            ChFieldQueryBuilder(
                param_name="name",
                bind_type="String",
                compare="=",
                val=function,
            )
        )

    return await run_and_collect(
        connection,
        builder.build(),
        RowWindow(
            offset=offset,
            limit=limit,
        ),
    )


@tool
async def ch_sequences_discribe(
    connection: ChConnection,
    database: Annotated[
        str,
        Field(
            min_length=1,
            description="Имя базы. `*` — все пользовательские базы.",
        ),
    ] = "*",
    *,
    offset: RowOffset,
    limit: RowLimit,
) -> SqlResult:
    """ADQM, ClickHouse, описание последовательностей из system.sequences
    Колонки: address, database, name, uuid, start_value, increment,
    min_value, max_value, cycle, cache, comment. На старых версиях
    таблицы нет — запрос упадёт с ошибкой сервера.
    """
    builder = ChQueryBuilder("""
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
        {condition}
        order by
            database,
            name
    """)

    set_database_filter_bootstrap(
        builder,
        "database",
        "String",
        "=",
        database,
    )

    return await run_and_collect(
        connection,
        builder.build(),
        RowWindow(
            offset=offset,
            limit=limit,
        ),
    )


@tool
async def ch_types_discribe(
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
    """ADQM, ClickHouse, описание типов данных из system.data_type_families.
    Колонки: name, case_insensitive, alias_to. В ClickHouse нет
    enum/domain/composite; Enum-типы описываются
    прямо в колонке — смотрите ch_column_discribe.
    """
    builder = ChQueryBuilder("""
        select
            name as address,
            name,
            case_insensitive,
            alias_to
        from
            system.data_type_families
        {condition}
        order by
            name
    """)

    if name and name != "*":
        builder.condition(
            ChFieldQueryBuilder(
                param_name="name",
                bind_type="String",
                compare="=",
                val=name,
            )
        )

    return await run_and_collect(
        connection,
        builder.build(),
        RowWindow(
            offset=offset,
            limit=limit,
        ),
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
    ClickHouseError: SqlErrorKind.DATABASE_UNAVAILABLE,
    ClickHouseQueryError: SqlErrorKind.SQL_FAILED,
    ResultTooLargeError: SqlErrorKind.RESULT_TOO_LARGE,
}

TOOLS: Final = ToolMain.toolset(
    ch_list_tables,
    ch_list_columns,
    ch_describe_table,
    ch_query,
    ch_address,
    ch_database_discribe,
    ch_table_discribe,
    ch_column_discribe,
    ch_constraints_discribe,
    ch_indexes_discribe,
    ch_function_discribe,
    ch_sequences_discribe,
    ch_types_discribe,
)

if __name__ == "__main__":
    sys.exit(ToolMain.run(TOOLS))
