"""Postgres-инструменты: функции уровня модуля, модуль — обычная программа.

Запуск: `python -m boba.tool.pg.tools <имя> --флаги` — та же команда у
launcher'а приложения и у человека в терминале.

Ошибки:
PostgresError — до базы не достучаться (сеть, отказ libpq, kerberos).
UnknownConnectionError — имя подключения вне whitelist'а конфига.
psycopg.Error — сервер отклонил запрос (синтаксис, права).
ResultTooLargeError — выдача превысила max_bytes конфига.
"""

from __future__ import annotations

import codecs
import sys
from collections.abc import Mapping
from enum import StrEnum
from typing import Annotated, Any, ClassVar, Final

import psycopg
from langchain_core.tools import InjectedToolArg, tool
from psycopg.rows import dict_row
from pydantic import Field

from boba.db.postgres import PayloadPostgres, PostgresConfig, PostgresError
from boba.tool.pg.catalog import PgCatalog, PgCatalogQuery
from boba.toolkit.entry import ToolMain
from boba.toolkit.launcher import RowStream
from boba.toolkit.result import (
    AffectedSqlResult,
    PgCopyTextResult,
    TableResult,
    ToolResult,
    render_for_llm,
)
from boba.toolkit.sql import SqlProfiles, UnknownConnectionError

_CONNECTION_DESCRIPTION = "Имя подключения"


class PgToolConfig(SqlProfiles[PostgresConfig]):
    """Whitelist подключений и лимиты выдачи pg-инструментов; [tool.pg]."""

    SECTION: ClassVar[str] = "tool.pg"

    profiles: dict[str, PostgresConfig] = Field(
        default_factory=dict,
        description=(
            "dict[connection_name, postgres-профиль ссылкой]: "
            '`[tool.pg.profiles] main = "${postgres.main}"`. '
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
            context={PostgresConfig.REVEAL_SECRETS: True},
        )


class ResultTooLargeError(Exception):
    """Выдача превысила потолок байт; текст готов для пользователя."""

    def __init__(self, max_bytes: int) -> None:
        msg = f"result exceeded {max_bytes} bytes; add LIMIT to the query"
        super().__init__(msg)


class PgErrorKind(StrEnum):
    """Ожидаемые отказы pg-инструментов."""

    DATABASE_UNAVAILABLE = "database_unavailable"
    UNKNOWN_TARGET = "unknown_target"
    SQL_FAILED = "sql_failed"
    RESULT_TOO_LARGE = "result_too_large"


async def _query_rows(
    connection: PostgresConfig,
    query: PgCatalogQuery,
    cfg: PgToolConfig,
) -> tuple[str, ToolResult]:
    """Выполнить запрос с параметрами и собрать выдачу таблицей."""
    conn = await PayloadPostgres.connect_config(connection)
    async with conn, conn.cursor(row_factory=dict_row) as cur:
        # bytes: тип Query psycopg требует LiteralString, а текст собран кодом
        await cur.execute(query.text.encode(), query.params or None)

        if cur.description is None:
            affected = _affected(cur)
            return render_for_llm(affected), affected

        fetched = await cur.fetchmany(cfg.max_rows + 1)

    truncated = len(fetched) > cfg.max_rows

    rows: list[dict[str, Any]] = []
    size = 0
    for row in fetched[: cfg.max_rows]:
        plain = RowStream.plain(row)

        size += len(RowStream.encode(plain))
        if size > cfg.max_bytes:
            raise ResultTooLargeError(cfg.max_bytes)

        rows.append(plain)

    note = None
    if truncated:
        note = f"truncated to max_rows ({cfg.max_rows})"

    table = TableResult(rows=rows, note=note)
    return render_for_llm(table), table


def _affected(cur: psycopg.AsyncCursor[Any]) -> AffectedSqlResult:
    """Итог запроса без выборки; rowcount -1 у psycopg значит «счётчика нет»."""
    rowcount: int | None = cur.rowcount
    if cur.rowcount < 0:
        rowcount = None

    return AffectedSqlResult(affected_rows=rowcount, status=cur.statusmessage)


@tool(response_format="content_and_artifact")
async def pg_list_targets(
    cfg: Annotated[PgToolConfig, InjectedToolArg],
) -> tuple[str, ToolResult]:
    """Список доступных значений connection_name для postgres-инструментов."""
    rows: list[dict[str, Any]] = []
    for target in cfg.targets():
        rows.append({"connection_name": target})

    table = TableResult(rows=rows)
    return render_for_llm(table), table


@tool(response_format="content_and_artifact")
async def pg_list_tables(
    connection_name: Annotated[
        str,
        Field(min_length=1, description=_CONNECTION_DESCRIPTION),
    ],
    pg_schema: Annotated[
        str | None,
        Field(
            description=(
                "Схема; пусто — все схемы, включая системные "
                "(pg_catalog, information_schema). Их много, и выдача "
                "упрётся в max_rows — сузьте фильтр."
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
    cfg: Annotated[PgToolConfig, InjectedToolArg],
) -> tuple[str, ToolResult]:
    """Таблицы и view подключения из pg_catalog.

    Колонки: schema, table_name, kind, approx_rows, owner, total_bytes,
    comment. kind: r таблица, p партиционированная, v view,
    m материализованное view, f сторонняя таблица. Сложные условия по
    каталогу пишутся запросом к pg_catalog через pg_query.
    """
    connection = cfg.resolve(connection_name)

    query = PgCatalog.tables(pg_schema, table_pattern)
    return await _query_rows(connection, query, cfg)


@tool(response_format="content_and_artifact")
async def pg_describe_table(
    connection_name: Annotated[
        str,
        Field(min_length=1, description=_CONNECTION_DESCRIPTION),
    ],
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
    cfg: Annotated[PgToolConfig, InjectedToolArg],
) -> tuple[str, ToolResult]:
    """Схема таблицы из pg_catalog: колонки, нативные типы, ключи.

    Колонки: schema, position, column_name, type, nullable,
    default_expression, identity, generated, primary_key, comment.
    """
    connection = cfg.resolve(connection_name)

    query = PgCatalog.columns(table, pg_schema)
    return await _query_rows(connection, query, cfg)


@tool(response_format="content_and_artifact")
async def pg_query(
    connection_name: Annotated[
        str,
        Field(min_length=1, description=_CONNECTION_DESCRIPTION),
    ],
    sql: Annotated[
        str,
        Field(
            min_length=1,
            description=(
                "Произвольный SQL. Запрос с выборкой возвращает строки; "
                "если их больше лимита — добавьте LIMIT в сам запрос. "
                "INSERT/UPDATE/DELETE/DDL возвращают число затронутых "
                "строк и статус сервера."
            ),
        ),
    ],
    cfg: Annotated[PgToolConfig, InjectedToolArg],
) -> tuple[str, ToolResult]:
    """Выполнить SQL на подключении: строки либо счётчик затронутых."""
    connection = cfg.resolve(connection_name)

    return await _query_rows(connection, PgCatalogQuery(text=sql, params=()), cfg)


@tool(response_format="content_and_artifact")
async def pg_copy(
    connection_name: Annotated[
        str,
        Field(min_length=1, description=_CONNECTION_DESCRIPTION),
    ],
    sql: Annotated[
        str,
        Field(
            min_length=1,
            description=(
                "Read-only SQL для выгрузки. Выполняется через "
                "COPY (...) TO STDOUT и возвращается текстом; запись и "
                "DDL внутри COPY сервер отклонит. Если строк больше "
                "лимита — добавьте LIMIT в сам запрос."
            ),
        ),
    ],
    cfg: Annotated[PgToolConfig, InjectedToolArg],
) -> tuple[str, ToolResult]:
    """Выгрузить результат SELECT текстом через COPY ... TO STDOUT."""
    connection = cfg.resolve(connection_name)

    # bytes: тип Query psycopg требует LiteralString, а запрос пишет LLM
    statement = f"COPY ({sql}) TO STDOUT WITH (FORMAT TEXT, HEADER)".encode()

    # блоки COPY режут utf-8 в произвольном месте — декодер инкрементальный
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    parts: list[str] = []
    size = 0

    conn = await PayloadPostgres.connect_config(connection)
    async with conn, conn.cursor() as cur, cur.copy(statement) as copy_out:
        async for block in copy_out:
            data = bytes(block)

            size += len(data)
            if size > cfg.max_bytes:
                raise ResultTooLargeError(cfg.max_bytes)

            text = decoder.decode(data)
            if text:
                parts.append(text)

    tail = decoder.decode(b"", True)
    if tail:
        parts.append(tail)

    artifact = PgCopyTextResult(text="".join(parts))
    return render_for_llm(artifact), artifact


EXPECTED: Mapping[type[Exception], PgErrorKind] = {
    PostgresError: PgErrorKind.DATABASE_UNAVAILABLE,
    UnknownConnectionError: PgErrorKind.UNKNOWN_TARGET,
    psycopg.Error: PgErrorKind.SQL_FAILED,
    ResultTooLargeError: PgErrorKind.RESULT_TOO_LARGE,
}

TOOLS: Final = ToolMain.toolset(
    pg_list_targets,
    pg_list_tables,
    pg_describe_table,
    pg_query,
    pg_copy,
)

if __name__ == "__main__":
    sys.exit(ToolMain.run(TOOLS))
