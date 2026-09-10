"""ClickHouse-инструменты: функции уровня модуля, модуль — обычная программа.

Запуск: `python -m boba.tool.ch.tools <имя> --флаги` — та же команда у
launcher'а приложения и у человека в терминале.

Ошибки:
ClickHouseError — до базы не достучаться (сеть, TLS, kerberos).
ClickHouseQueryError — сервер отклонил запрос (синтаксис, права).
UnknownConnectionError — имя подключения вне whitelist'а конфига.
ResultTooLargeError — выдача превысила max_bytes конфига.
"""

from __future__ import annotations

import sys
from collections.abc import Mapping, Sequence
from typing import Annotated, Any, ClassVar, Final, cast

from pydantic import Field

from boba.db.clickhouse import ClickHouseError, ClickHouseQueryError
from boba.db.clickhouse.profile import ClickHouseConfig
from boba.toolkit.entry import ToolMain
from boba.toolkit.facade import Injected, UserConnection, tool
from boba.toolkit.result import MarkdownResult, ResultTooLargeError, SqlResult
from boba.toolkit.sql import (
    CatalogQuery,
    MaxChars,
    MaxRows,
    RowBudget,
    RowOffset,
    RowPage,
    RowWindow,
    SqlErrorKind,
    SqlLimits,
)
from boba.toolkit.types import SecretRevealing

ChParams = dict[str, Any]
"""Именованные параметры ClickHouse под подстановку {name:Type}."""


ChConnection = Annotated[ClickHouseConfig, UserConnection]


class ChToolConfig(SecretRevealing, SqlLimits):
    """Лимиты выдачи ch-инструментов; [tool.ch]."""

    SECTION: ClassVar[str] = "tool.ch"
    ENGINE: ClassVar[str] = "clickhouse"
    """Подпись движка в SqlResult."""


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


def _payload() -> Any:
    """Клиент базы: тянет clickhouse-connect, которого в приложении нет.

    Модуль инструмента читает хост ради объявлений, а драйвер живёт только
    в песочнице — поэтому импорт отложен до самого вызова.
    """
    from boba.db.clickhouse import payload  # noqa: PLC0415

    return payload.PayloadClickHouse


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
) -> SqlResult:
    """Каталожный запрос страницей окна: границы выдачи назначает вызов."""
    parameters = query.params
    if not parameters:
        parameters = None

    page = RowPage(window)

    client_module = _payload()

    async with client_module.opened_config(connection) as client:
        blocks = client_module.row_blocks(client, connection, query.text, parameters)
        async with blocks as rows:
            await _collect_page(rows.blocks, rows.names, page)

    return SqlResult(engine=ChToolConfig.ENGINE, statements=[page.statement()])


async def _query_rows(
    connection: ClickHouseConfig,
    query: CatalogQuery[ChParams],
    cfg: ChToolConfig,
) -> SqlResult:
    """Выполнить запрос и собрать выдачу таблицей; блоки стримятся с лимитом."""
    parameters = query.params
    if not parameters:
        parameters = None

    budget = RowBudget(max_rows=cfg.max_rows, max_bytes=cfg.max_bytes)

    client_module = _payload()

    async with client_module.opened_config(connection) as client:
        blocks = client_module.row_blocks(client, connection, query.text, parameters)
        async with blocks as rows:
            await _collect_blocks(rows.blocks, rows.names, budget)

    return SqlResult(engine=ChToolConfig.ENGINE, statements=[budget.statement()])


@tool
async def ch_list_tables(  # noqa: PLR0913 — окно выдачи задаёт вызов
    connection: ChConnection,
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
) -> SqlResult:
    """Список таблиц/view подключения. Колонки: database, table, engine.

    Выдача постраничная: сколько показано и как листать, сказано в note.
    """
    window = RowWindow(offset=offset, max_rows=max_rows, max_chars=max_chars)

    return await _catalog_page(connection, ChCatalog.tables(ch_database), window)


@tool
async def ch_describe_table(  # noqa: PLR0913 — окно выдачи задаёт вызов
    connection: ChConnection,
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
) -> SqlResult:
    """Схема таблицы: колонки, типы, default-выражения, комментарии.

    Широкая таблица приходит частями: как листать, сказано в note.
    """
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
        MarkdownResult(language="sql"),
    ],
    connection: ChConnection,
    cfg: Annotated[ChToolConfig, Injected],
) -> SqlResult:
    """Выполнить SQL на выбранном соединении."""

    return await _query_rows(connection, CatalogQuery(text=sql, params={}), cfg)


EXPECTED: Mapping[type[Exception], SqlErrorKind] = {
    ClickHouseError: SqlErrorKind.DATABASE_UNAVAILABLE,
    ClickHouseQueryError: SqlErrorKind.SQL_FAILED,
    ResultTooLargeError: SqlErrorKind.RESULT_TOO_LARGE,
}

TOOLS: Final = ToolMain.toolset(
    ch_list_tables,
    ch_describe_table,
    ch_query,
)

if __name__ == "__main__":
    sys.exit(ToolMain.run(TOOLS))
