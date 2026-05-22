"""Tool `sql_query`: execute arbitrary SQL, return markdown table.

LLM пишет произвольный SQL → executor запускает в read-only транзакции
(`SET LOCAL default_transaction_read_only=on` + `SET LOCAL statement_timeout`)
→ результат форматируется в markdown-таблицу.

Open-mode: whitelist таблиц нет, защита — на уровне DSN-роли в
`[tool.kb.sql]` (оператор даёт LLM ограниченную SELECT-роль). DDL/DML
отклоняются PG-side как `permission denied` / `cannot execute X in a
read-only transaction`.
"""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import Field

from boba.tool.kb.core._markdown import format_markdown_table
from boba.tool.kb.sql.executor import SqlExecutor, SqlQueryError
from boba.tools import FromDI, Scope, tool

__all__ = ["sql_query"]


@tool
def sql_query(
    query: Annotated[
        str,
        Field(
            min_length=1,
            description=(
                "Произвольный SQL-запрос. Разрешены только read-only "
                "операции — это гарантируется правами DSN-роли и "
                "`SET LOCAL default_transaction_read_only=on`. Попытка "
                "INSERT/UPDATE/DROP/etc упадёт с `permission denied` или "
                "`cannot execute X in a read-only transaction`. Если в "
                "запросе нет `LIMIT` — авто-инжектится `LIMIT row_limit`."
            ),
        ),
    ],
    executor: Annotated[SqlExecutor, FromDI(Scope.APP)],
    row_limit: Annotated[
        int,
        Field(
            ge=1,
            description=(
                "Сколько строк вернуть. По умолчанию 20; жёсткий потолок — "
                "`[tool.kb.sql].max_rows`."
            ),
        ),
    ] = 20,
) -> dict[str, Any]:
    """Выполнить произвольный SQL-запрос; результат — markdown-таблица.

    Подходит для exploratory-аналитики и ad-hoc отчётов. Перед вызовом
    стоит позвать `sql_list_tables` (что доступно) и `sql_describe_table`
    (схема нужной таблицы).

    **Ограничения** (защита от случайностей и злоупотреблений):
    - DSN-роль read-only (это уровень оператора) — PG отклоняет DDL/DML.
    - `SET LOCAL default_transaction_read_only = on` — двойная защита.
    - `LIMIT` инжектится автоматически, если отсутствует в запросе.
    - Длинные значения в cell'ах режутся по `max_cell_chars`.
    - Statement timeout — `[tool.kb.sql].statement_timeout_ms`.

    Возвращает `{table: "<markdown>", columns: [...], row_count: N,
    limit_applied: M, truncated: bool}`. `truncated=true` → есть ещё
    строки сверх `row_limit`; увеличь его и повтори запрос.
    """
    try:
        result = executor.execute(query, row_limit=row_limit)
    except SqlQueryError as e:
        raise RuntimeError(str(e)) from e

    table_md = format_markdown_table(
        columns=result.columns,
        rows=result.rows,
        max_cell_chars=executor.max_cell_chars,
        truncated=result.truncated,
    )
    return {
        "table": table_md,
        "columns": result.columns,
        "row_count": result.row_count,
        "limit_applied": result.limit_applied,
        "truncated": result.truncated,
    }
