"""Tool `sql_describe_table`: схема одной таблицы (имена/типы колонок).

LLM зовёт после `sql_list_tables`, чтобы узнать структуру конкретной
таблицы перед написанием SQL. Возвращает markdown с колонками
`{column_name, data_type, is_nullable, default}`.
"""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import Field

from boba.tool.kb._markdown import format_markdown_table
from boba.tool.kb.sql.executor import SqlExecutor, SqlQueryError
from boba.tools import FromDI, Scope, tool

__all__ = ["sql_describe_table"]


@tool
def sql_describe_table(
    table: Annotated[
        str,
        Field(
            min_length=1,
            description=(
                "Имя таблицы (без схемы). Например, `kb_chunks`. Чтобы "
                "посмотреть колонки таблицы в нестандартной схеме, передай "
                "имя схемы через параметр `schema`."
            ),
        ),
    ],
    executor: Annotated[SqlExecutor, FromDI(Scope.APP)],
    schema: Annotated[
        str,
        Field(
            min_length=1,
            description="PG schema таблицы. По умолчанию `public`.",
        ),
    ] = "public",
) -> dict[str, Any]:
    """Схема одной таблицы: колонки, типы, nullable, default.

    Список колонок берётся из `information_schema.columns`. Поля:
    `column_name, data_type, is_nullable, column_default`. Если таблицы
    с таким именем нет (или у роли DSN'а нет прав на её view'у) —
    результат будет пустым.
    """
    query = (
        "SELECT column_name, data_type, is_nullable, column_default "
        "FROM information_schema.columns "
        "WHERE table_schema = %s AND table_name = %s "
        "ORDER BY ordinal_position"
    )
    try:
        result = executor.execute(
            query,
            row_limit=executor.max_rows_cap,
            params=(schema, table),
        )
    except SqlQueryError as e:
        raise RuntimeError(str(e)) from e

    table_md = format_markdown_table(
        columns=result.columns,
        rows=result.rows,
        max_cell_chars=executor.max_cell_chars,
        truncated=result.truncated,
    )
    return {
        "schema": schema,
        "table": table,
        "columns_table": table_md,
        "column_count": result.row_count,
    }
