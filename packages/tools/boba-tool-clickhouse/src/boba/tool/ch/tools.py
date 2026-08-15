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
from enum import StrEnum
from typing import Annotated, Any, ClassVar, Final, cast

from clickhouse_connect.driver.exceptions import ClickHouseError as DriverError
from langchain_core.tools import InjectedToolArg, tool
from pydantic import Field

from boba.db.clickhouse import ClickHouseConfig, ClickHouseError
from boba.db.clickhouse.payload import PayloadClickHouse
from boba.toolkit.entry import ToolMain
from boba.toolkit.launcher import RowStream
from boba.toolkit.result import TableResult, ToolResult, render_for_llm
from boba.toolkit.sql import CatalogQuery, SqlProfiles, UnknownConnectionError

_CONNECTION_DESCRIPTION = "Имя подключения"

ChParams = dict[str, Any]
"""Именованные параметры ClickHouse под подстановку {name:Type}."""


class ChToolConfig(SqlProfiles[ClickHouseConfig]):
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

    def revealed(self) -> dict[str, object]:
        """JSON-совместимый дамп с раскрытыми секретами профилей.

        Едет только в tool_stdin песочного вызова; обязан собираться обратно
        в тот же тип — SecretStr оживает из открытой строки.
        """
        return self.model_dump(
            mode="json",
            context={ClickHouseConfig.REVEAL_SECRETS: True},
        )


class ResultTooLargeError(Exception):
    """Выдача превысила потолок байт; текст готов для пользователя."""

    def __init__(self, max_bytes: int) -> None:
        msg = f"result exceeded {max_bytes} bytes; add LIMIT to the query"
        super().__init__(msg)


class ChErrorKind(StrEnum):
    """Ожидаемые отказы ch-инструментов."""

    DATABASE_UNAVAILABLE = "database_unavailable"
    UNKNOWN_TARGET = "unknown_target"
    SQL_FAILED = "sql_failed"
    RESULT_TOO_LARGE = "result_too_large"


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


async def _query_rows(
    connection: ClickHouseConfig,
    query: CatalogQuery[ChParams],
    cfg: ChToolConfig,
) -> tuple[str, ToolResult]:
    """Выполнить запрос и собрать выдачу таблицей; блоки стримятся с лимитом."""
    parameters = query.params or None

    rows: list[dict[str, Any]] = []
    size = 0
    truncated = False

    async with PayloadClickHouse.opened_config(connection) as client:
        try:
            stream = await client.query_row_block_stream(
                query.text, parameters=parameters
            )
            async with stream as blocks:
                names: Sequence[str] = cast("Any", blocks.source).column_names
                async for block in blocks:
                    for values in cast("Sequence[Sequence[Any]]", block):
                        if len(rows) >= cfg.max_rows:
                            truncated = True
                            break

                        plain = RowStream.plain(dict(zip(names, values, strict=True)))

                        size += len(RowStream.encode(plain))
                        if size > cfg.max_bytes:
                            raise ResultTooLargeError(cfg.max_bytes)

                        rows.append(plain)

                    if truncated:
                        break
        except DriverError as exc:
            msg = f"query failed: {type(exc).__name__}: {exc}"
            raise ClickHouseError(msg) from exc

    note = None
    if truncated:
        note = f"truncated to max_rows ({cfg.max_rows})"

    table = TableResult(rows=rows, note=note)
    return render_for_llm(table), table


@tool(response_format="content_and_artifact")
async def ch_list_targets(
    cfg: Annotated[ChToolConfig, InjectedToolArg],
) -> tuple[str, ToolResult]:
    """Список доступных значений connection_name для ClickHouse-инструментов."""
    rows: list[dict[str, Any]] = []
    for target in cfg.targets():
        rows.append({"connection_name": target})

    table = TableResult(rows=rows)
    return render_for_llm(table), table


@tool(response_format="content_and_artifact")
async def ch_list_tables(
    connection_name: Annotated[
        str,
        Field(min_length=1, description=_CONNECTION_DESCRIPTION),
    ],
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
    cfg: Annotated[ChToolConfig, InjectedToolArg],
) -> tuple[str, ToolResult]:
    """Список таблиц/view подключения. Колонки: database, table, engine."""
    connection = cfg.resolve(connection_name)

    return await _query_rows(connection, ChCatalog.tables(ch_database), cfg)


@tool(response_format="content_and_artifact")
async def ch_describe_table(
    connection_name: Annotated[
        str,
        Field(min_length=1, description=_CONNECTION_DESCRIPTION),
    ],
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
    cfg: Annotated[ChToolConfig, InjectedToolArg],
) -> tuple[str, ToolResult]:
    """Схема таблицы: колонки, типы, default-выражения, комментарии."""
    connection = cfg.resolve(connection_name)

    return await _query_rows(connection, ChCatalog.columns(table, ch_database), cfg)


@tool(response_format="content_and_artifact")
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
    connection_name: Annotated[
        str,
        Field(min_length=1, description=_CONNECTION_DESCRIPTION),
    ],
    cfg: Annotated[ChToolConfig, InjectedToolArg],
) -> tuple[str, ToolResult]:
    """Выполнить SQL на подключении connection_name."""
    connection = cfg.resolve(connection_name)

    return await _query_rows(connection, CatalogQuery(text=sql, params={}), cfg)


EXPECTED: Mapping[type[Exception], ChErrorKind] = {
    ClickHouseError: ChErrorKind.DATABASE_UNAVAILABLE,
    UnknownConnectionError: ChErrorKind.UNKNOWN_TARGET,
    DriverError: ChErrorKind.SQL_FAILED,
    ResultTooLargeError: ChErrorKind.RESULT_TOO_LARGE,
}

TOOLS: Final = ToolMain.toolset(
    ch_list_targets,
    ch_list_tables,
    ch_describe_table,
    ch_query,
)

if __name__ == "__main__":
    sys.exit(ToolMain.run(TOOLS))
