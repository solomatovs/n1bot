"""ClickHouse-инструменты: функции уровня модуля, модуль — обычная программа.

Запуск: `python -m boba.tool.ch.tools <имя> --флаги` — та же команда у
launcher'а приложения и у человека в терминале.

Ошибки:
ClickHouseError — до базы не достучаться (сеть, TLS, kerberos).
UnknownConnectionError — имя подключения вне whitelist'а конфига.
clickhouse DriverError — сервер отклонил запрос (синтаксис, права).
ResultTooLargeError — выдача превысила max_bytes конфига.
"""

from __future__ import annotations

import sys
from collections.abc import Mapping, Sequence
from typing import Annotated, Any, ClassVar, Final, cast

from clickhouse_connect.driver.exceptions import ClickHouseError as DriverError
from pydantic import Field

from boba.db.clickhouse import ClickHouseConfig, ClickHouseError
from boba.db.clickhouse.payload import PayloadClickHouse
from boba.toolkit.calls import ScriptCall
from boba.toolkit.entry import ToolMain
from boba.toolkit.facade import Injected, tool
from boba.toolkit.result import (
    ResultTooLargeError,
    ToolResult,
    pack_result,
)
from boba.toolkit.sql import (
    CatalogQuery,
    ConnectionName,
    MaxChars,
    MaxRows,
    RowBudget,
    RowOffset,
    RowPage,
    RowWindow,
    SqlErrorKind,
    SqlProfiles,
    UnknownConnectionError,
)
from boba.toolkit.types import SecretRevealing

ChParams = dict[str, Any]
"""Именованные параметры ClickHouse под подстановку {name:Type}."""


class ChToolConfig(SecretRevealing, SqlProfiles[ClickHouseConfig]):
    """Whitelist подключений и лимиты выдачи ch-инструментов; [tool.ch]."""

    SECTION: ClassVar[str] = "tool.ch"

    profiles: dict[str, ClickHouseConfig] = Field(
        default_factory=dict,
        description=(
            "dict[connection_name, clickhouse-профиль ссылкой]: "
            '`[tool.ch.profiles] main = "${clickhouse.main}"`. '
            "Ключ — значение tool-arg `connection_name` (LLM выбирает БД по нему)."
        ),
    )


class ChCatalog:
    """Каталожные запросы ClickHouse: фильтры уезжают именованными параметрами."""

    SYSTEM_DATABASES: ClassVar[str] = (
        "('system', 'INFORMATION_SCHEMA', 'information_schema')"
    )

    TABLES_SELECT: ClassVar[str] = """
select
    database,
    name as table,
    engine,
    total_rows
from
    system.tables
"""
    TABLES_ORDER: ClassVar[str] = """
order by
    database,
    name
"""
    COLUMNS_SELECT: ClassVar[str] = """
select
    name,
    type,
    default_kind,
    default_expression,
    comment
from
    system.columns
"""
    COLUMNS_ORDER: ClassVar[str] = """
order by
    position
"""

    @classmethod
    def tables(cls, ch_database: str | None) -> CatalogQuery[ChParams]:
        """Таблицы и view; без фильтра системные базы исключаются."""
        params: ChParams = {}

        if ch_database:
            condition = "database = {db:String}"
            params["db"] = ch_database
        else:
            condition = f"database not in {cls.SYSTEM_DATABASES}"

        text = cls._assemble(cls.TABLES_SELECT, [condition], cls.TABLES_ORDER)

        return CatalogQuery(text=text, params=params)

    @classmethod
    def columns(cls, table: str, ch_database: str | None) -> CatalogQuery[ChParams]:
        """Колонки таблицы; без базы берётся база по умолчанию у подключения."""
        params: ChParams = {"table": table}
        conditions = ["table = {table:String}"]

        if ch_database:
            conditions.append("database = {db:String}")
            params["db"] = ch_database
        else:
            conditions.append("database = currentDatabase()")

        text = cls._assemble(cls.COLUMNS_SELECT, conditions, cls.COLUMNS_ORDER)

        return CatalogQuery(text=text, params=params)

    @classmethod
    def _assemble(cls, select: str, conditions: list[str], order: str) -> str:
        where = "\n    and ".join(conditions)

        return f"{select}where\n    {where}\n{order}"


async def _collect_blocks(
    blocks: Any,
    names: Sequence[str],
    budget: RowBudget,
) -> None:
    """Строки из блоков стрима в копилку; потолок строк останавливает чтение."""
    async for block in blocks:
        for values in cast("Sequence[Sequence[Any]]", block):
            row = dict(zip(names, values, strict=True))
            if not budget.add(row):
                return


async def _collect_page(
    blocks: Any,
    names: Sequence[str],
    page: RowPage,
) -> None:
    """Строки из блоков стрима в страницу; набранное окно обрывает чтение."""
    async for block in blocks:
        for values in cast("Sequence[Sequence[Any]]", block):
            row = dict(zip(names, values, strict=True))
            if not page.add(row):
                return


async def _catalog_page(
    connection: ClickHouseConfig,
    query: CatalogQuery[ChParams],
    window: RowWindow,
) -> tuple[str, ToolResult]:
    """Каталожный запрос страницей окна: границы выдачи назначает вызов."""
    parameters = query.params
    if not parameters:
        parameters = None

    page = RowPage(window)

    async with PayloadClickHouse.opened_config(connection) as client:
        try:
            stream = await client.query_row_block_stream(
                query.text, parameters=parameters
            )
            async with stream as blocks:
                names: Sequence[str] = cast("Any", blocks.source).column_names
                await _collect_page(blocks, names, page)
        except DriverError as exc:
            msg = f"query failed: {type(exc).__name__}: {exc}"
            raise ClickHouseError(msg) from exc

    return pack_result(page.table())


async def _query_rows(
    connection: ClickHouseConfig,
    query: CatalogQuery[ChParams],
    cfg: ChToolConfig,
) -> tuple[str, ToolResult]:
    """Выполнить запрос и собрать выдачу таблицей; блоки стримятся с лимитом."""
    parameters = query.params
    if not parameters:
        parameters = None

    budget = RowBudget(max_rows=cfg.max_rows, max_bytes=cfg.max_bytes)

    async with PayloadClickHouse.opened_config(connection) as client:
        try:
            stream = await client.query_row_block_stream(
                query.text, parameters=parameters
            )
            async with stream as blocks:
                names: Sequence[str] = cast("Any", blocks.source).column_names
                await _collect_blocks(blocks, names, budget)
        except DriverError as exc:
            msg = f"query failed: {type(exc).__name__}: {exc}"
            raise ClickHouseError(msg) from exc

    return pack_result(budget.table())


@tool
async def ch_connection_list(
    cfg: Annotated[ChToolConfig, Injected],
) -> tuple[str, ToolResult]:
    """Список доступных значений connection_name для ClickHouse-инструментов."""
    return pack_result(cfg.targets_table())


@tool
async def ch_list_tables(  # noqa: PLR0913 — окно выдачи задаёт вызов
    connection_name: ConnectionName,
    ch_database: Annotated[
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
    max_rows: MaxRows,
    max_chars: MaxChars,
    cfg: Annotated[ChToolConfig, Injected],
) -> tuple[str, ToolResult]:
    """Список таблиц/view подключения. Колонки: database, table, engine.

    Выдача постраничная: сколько показано и как листать, сказано в note.
    """
    connection = cfg.resolve(connection_name)
    window = RowWindow(offset=offset, max_rows=max_rows, max_chars=max_chars)

    return await _catalog_page(connection, ChCatalog.tables(ch_database), window)


@tool
async def ch_describe_table(  # noqa: PLR0913 — окно выдачи задаёт вызов
    connection_name: ConnectionName,
    table: Annotated[
        str,
        Field(min_length=1, description="Имя таблицы (без базы)"),
    ],
    ch_database: Annotated[
        str | None,
        Field(
            description="База таблицы; пусто — база по умолчанию у подключения.",
        ),
    ] = None,
    *,
    offset: RowOffset,
    max_rows: MaxRows,
    max_chars: MaxChars,
    cfg: Annotated[ChToolConfig, Injected],
) -> tuple[str, ToolResult]:
    """Схема таблицы: колонки, типы, default-выражения, комментарии.

    Широкая таблица приходит частями: как листать, сказано в note.
    """
    connection = cfg.resolve(connection_name)
    window = RowWindow(offset=offset, max_rows=max_rows, max_chars=max_chars)

    return await _catalog_page(
        connection, ChCatalog.columns(table, ch_database), window
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
    ],
    connection_name: ConnectionName,
    cfg: Annotated[ChToolConfig, Injected],
) -> tuple[str, ToolResult]:
    """Выполнить SQL на подключении connection_name."""
    connection = cfg.resolve(connection_name)

    return await _query_rows(connection, CatalogQuery(text=sql, params={}), cfg)


EXPECTED: Mapping[type[Exception], SqlErrorKind] = {
    ClickHouseError: SqlErrorKind.DATABASE_UNAVAILABLE,
    UnknownConnectionError: SqlErrorKind.UNKNOWN_TARGET,
    DriverError: SqlErrorKind.SQL_FAILED,
    ResultTooLargeError: SqlErrorKind.RESULT_TOO_LARGE,
}

TOOLS: Final = ToolMain.toolset(
    ch_connection_list,
    ch_list_tables,
    ch_describe_table,
    ch_query,
    views={"ch_query": ScriptCall(arg="sql", lang="sql")},
)

if __name__ == "__main__":
    sys.exit(ToolMain.run(TOOLS))
