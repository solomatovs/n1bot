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
from typing import Annotated, Any, ClassVar, Final

import psycopg
from psycopg.rows import dict_row
from pydantic import Field

from boba.db.postgres import PayloadPostgres, PostgresConfig, PostgresError
from boba.tool.pg.catalog import PgCatalog, PgCatalogQuery
from boba.toolkit.calls import ScriptCall
from boba.toolkit.entry import ToolMain
from boba.toolkit.facade import Injected, tool
from boba.toolkit.result import (
    AffectedSqlResult,
    MultiResult,
    ResultTooLargeError,
    TableResult,
    TextResult,
    ToolResult,
    pack_result,
)
from boba.toolkit.sql import (
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


class CopyDump:
    """Показ выгрузки COPY: чем разделены поля, знает только автор запроса.

    Дамп не разбирается — постгрес отдаёт его в формате, заданном самим
    стейтментом. Блок помечается csv: описание инструмента просит этот
    формат, а шапка блока — единственное, на что метка влияет.
    """

    LANG: ClassVar[str] = "csv"


class PgToolConfig(SecretRevealing, SqlProfiles[PostgresConfig]):
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


async def _query_rows(
    connection: PostgresConfig,
    query: PgCatalogQuery,
    cfg: PgToolConfig,
) -> tuple[str, ToolResult]:
    """Выполнить запрос и собрать итог каждой его команды по порядку.

    Команд в запросе может быть несколько (`select ...; update ...;`): без
    подготовки psycopg шлёт их простым протоколом, и postgres выполняет
    набор одной неявной транзакцией — падение любой команды откатывает всё.
    Один итог отдаётся сам собой, несколько — набором MultiResult.
    """
    results: list[ToolResult] = []
    spent = 0

    conn = await PayloadPostgres.connect_config(connection)
    async with conn, conn.cursor(row_factory=dict_row) as cur:
        # bytes: тип Query psycopg требует LiteralString, а текст собран кодом;
        # кодировка — client_encoding подключения, а не обязательно utf-8
        await cur.execute(query.text.encode(conn.info.encoding), query.params or None)

        while True:
            if cur.description is None:
                results.append(_affected(cur))
            else:
                table, size = await _fetch_table(cur, cfg, cfg.max_bytes - spent)
                spent += size
                results.append(table)

            if not cur.nextset():
                break

    if len(results) == 1:
        return pack_result(results[0])

    return pack_result(MultiResult(items=tuple(results)))


async def _catalog_page(
    connection: PostgresConfig,
    query: PgCatalogQuery,
    window: RowWindow,
) -> tuple[str, ToolResult]:
    """Каталожный запрос страницей окна: границы выдачи назначает вызов.

    Порядок строк задан самим запросом, поэтому окно повторяемо: тот же
    offset вернёт тот же кусок, пока каталог не изменился.
    """
    page = RowPage(window)

    conn = await PayloadPostgres.connect_config(connection)
    async with conn, conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(query.text.encode(conn.info.encoding), query.params or None)

        for row in await cur.fetchmany(window.probe()):
            if not page.add(row):
                break

    return pack_result(page.table())


async def _fetch_table(
    cur: psycopg.AsyncCursor[Any],
    cfg: PgToolConfig,
    max_bytes: int,
) -> tuple[TableResult, int]:
    """Выборка одной команды таблицей и её вес.

    Потолок строк действует на команду, потолок байтов — на весь вызов:
    выдача уходит в одно сообщение, поэтому следующей команде остаётся
    остаток.
    """
    budget = RowBudget(max_rows=cfg.max_rows, max_bytes=max_bytes)

    fetched = await cur.fetchmany(cfg.max_rows + 1)
    for row in fetched:
        if not budget.add(row):
            break

    return budget.table(), budget.size


def _affected(cur: psycopg.AsyncCursor[Any]) -> AffectedSqlResult:
    """Итог запроса без выборки; rowcount -1 у psycopg значит «счётчика нет»."""
    rowcount: int | None = cur.rowcount
    if cur.rowcount < 0:
        rowcount = None

    return AffectedSqlResult(affected_rows=rowcount, status=cur.statusmessage)


@tool
async def pg_connection_list(
    cfg: Annotated[PgToolConfig, Injected],
) -> tuple[str, ToolResult]:
    """Список доступных значений connection_name для postgres-инструментов."""
    return pack_result(cfg.targets_table())


@tool
async def pg_list_tables(  # noqa: PLR0913 — окно выдачи задаёт вызов
    connection_name: ConnectionName,
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
    offset: RowOffset,
    max_rows: MaxRows,
    max_chars: MaxChars,
    cfg: Annotated[PgToolConfig, Injected],
) -> tuple[str, ToolResult]:
    """Таблицы и view подключения из pg_catalog.

    Колонки: schema, table_name, kind, approx_rows, owner, total_bytes,
    comment. kind: r таблица, p партиционированная, v view,
    m материализованное view, f сторонняя таблица. Выдача постраничная:
    сколько показано и как листать дальше, сказано в note. Сложные условия
    по каталогу пишутся запросом к pg_catalog через pg_query.
    """
    connection = cfg.resolve(connection_name)
    window = RowWindow(offset=offset, max_rows=max_rows, max_chars=max_chars)

    query = PgCatalog.tables(pg_schema, table_pattern)
    return await _catalog_page(connection, query, window)


@tool
async def pg_describe_table(  # noqa: PLR0913 — окно выдачи задаёт вызов
    connection_name: ConnectionName,
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
    offset: RowOffset,
    max_rows: MaxRows,
    max_chars: MaxChars,
    cfg: Annotated[PgToolConfig, Injected],
) -> tuple[str, ToolResult]:
    """Схема таблицы из pg_catalog: колонки, нативные типы, ключи.

    Колонки: schema, position, column_name, type, nullable,
    default_expression, identity, generated, primary_key, comment. Широкая
    таблица приходит частями: как листать, сказано в note.
    """
    connection = cfg.resolve(connection_name)
    window = RowWindow(offset=offset, max_rows=max_rows, max_chars=max_chars)

    query = PgCatalog.columns(table, pg_schema)
    return await _catalog_page(connection, query, window)


@tool
async def pg_query(
    connection_name: ConnectionName,
    sql: Annotated[
        str,
        Field(
            min_length=1,
            description=(
                "Произвольный SQL. Запрос с выборкой возвращает строки; "
                "если их больше лимита — добавьте LIMIT в сам запрос. "
                "INSERT/UPDATE/DELETE/DDL возвращают число затронутых "
                "строк и статус сервера. Команд может быть несколько через "
                "`;` — они идут одной транзакцией, и в ответ придёт итог "
                "каждой по порядку; падение любой откатывает весь набор."
            ),
        ),
    ],
    cfg: Annotated[PgToolConfig, Injected],
) -> tuple[str, ToolResult]:
    """Выполнить SQL на подключении: строки либо счётчик затронутых."""
    connection = cfg.resolve(connection_name)

    return await _query_rows(connection, PgCatalogQuery(text=sql, params=()), cfg)


@tool
async def pg_copy(
    connection_name: ConnectionName,
    sql: Annotated[
        str,
        Field(
            min_length=1,
            description=(
                "Стейтмент COPY ... TO STDOUT целиком, например: "
                "COPY (select ...) TO STDOUT WITH (FORMAT CSV, HEADER). "
                "Выгружай форматом CSV — в таком виде вывод и показывается. "
                "Ответ возвращается текстом как есть. Если строк больше "
                "лимита — добавьте LIMIT в сам запрос."
            ),
        ),
    ],
    cfg: Annotated[PgToolConfig, Injected],
) -> tuple[str, ToolResult]:
    """Выгрузить данные стейтментом COPY ... TO STDOUT как есть."""
    connection = cfg.resolve(connection_name)

    parts: list[str] = []
    size = 0

    conn = await PayloadPostgres.connect_config(connection)

    # bytes: тип Query psycopg требует LiteralString, а запрос пишет LLM;
    # кодировка — client_encoding подключения, а не обязательно utf-8
    statement = sql.encode(conn.info.encoding)

    # блоки COPY режут символ в произвольном месте — декодер инкрементальный
    decoder = codecs.getincrementaldecoder(conn.info.encoding)(errors="replace")

    async with conn, conn.cursor() as cur, cur.copy(statement) as copy_out:
        async for block in copy_out:
            data = bytes(block)

            size += len(data)
            if size > cfg.max_bytes:
                raise ResultTooLargeError.bytes_limit(cfg.max_bytes)

            text = decoder.decode(data)
            if text:
                parts.append(text)

    tail = decoder.decode(b"", True)
    if tail:
        parts.append(tail)

    artifact = TextResult(text="".join(parts), language=CopyDump.LANG)
    return pack_result(artifact)


EXPECTED: Mapping[type[Exception], SqlErrorKind] = {
    PostgresError: SqlErrorKind.DATABASE_UNAVAILABLE,
    UnknownConnectionError: SqlErrorKind.UNKNOWN_TARGET,
    psycopg.Error: SqlErrorKind.SQL_FAILED,
    ResultTooLargeError: SqlErrorKind.RESULT_TOO_LARGE,
}

TOOLS: Final = ToolMain.toolset(
    pg_connection_list,
    pg_list_tables,
    pg_describe_table,
    pg_query,
    pg_copy,
    views={
        "pg_query": ScriptCall(arg="sql", lang="sql"),
        "pg_copy": ScriptCall(arg="sql", lang="sql"),
    },
)

if __name__ == "__main__":
    sys.exit(ToolMain.run(TOOLS))
