"""Tool `sql_list_tables` + `SqlListTablesConfig`: список таблиц.

Помогает LLM понять, что лежит в БД, прежде чем писать SQL. Возвращает
markdown-таблицу `{schema, table, kind}` для всех таблиц/view, к которым
у DSN-роли есть права (PG отфильтрует автоматически).

Системные схемы `pg_catalog` / `information_schema` исключаются по
умолчанию (LLM редко полезно туда ходить). LLM может явно указать
`schema` для фильтрации по одной.
"""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import Field

from boba.settings import BobaFlatSettings, BobaSettingsConfigDict
from boba.tool.kb.core._markdown import format_markdown_table
from boba.tool.kb.sql.executor import SqlExecutor, SqlExecutorConfig, SqlQueryError
from boba.tools import FromConfig, tool

__all__ = ["SqlListTablesConfig", "sql_list_tables"]


class SqlListTablesConfig(BobaFlatSettings):
    """
    sql_list_tables config
    """

    model_config = BobaSettingsConfigDict(
        case_sensitive=False,
        extra="ignore",
        config_path="tool.kb.sql_list_tables",
    )

    executor: SqlExecutorConfig


@tool
def sql_list_tables(
    cfg: Annotated[SqlListTablesConfig, FromConfig()],
    schema: Annotated[
        str | None,
        Field(
            description=(
                "Опциональный фильтр по схеме (например `public`). "
                "Пусто = все user-schema'ы (без pg_catalog/information_schema)."
            ),
        ),
    ] = None,
) -> dict[str, Any]:
    """
    Список таблиц/view

    Возвращает markdown с колонками:
    `schema, table, kind` (BASE TABLE / VIEW / MATERIALIZED VIEW)
    """
    executor = SqlExecutor(cfg=cfg.executor)
    if schema:
        query = (
            "SELECT table_schema AS schema, table_name AS table, "
            "table_type AS kind "
            "FROM information_schema.tables "
            "WHERE table_schema = %s "
            "ORDER BY table_schema, table_name"
        )
        params: tuple[Any, ...] = (schema,)
    else:
        # `'pg_%'`-LIKE передаём параметром (psycopg иначе ловит `%'`
        # как несуществующий placeholder).
        query = (
            "SELECT table_schema AS schema, table_name AS table, "
            "table_type AS kind "
            "FROM information_schema.tables "
            "WHERE table_schema NOT IN ('pg_catalog', 'information_schema') "
            "AND table_schema NOT LIKE %s "
            "ORDER BY table_schema, table_name"
        )
        params = ("pg_%",)

    # row_limit здесь = max_rows: introspection-tool обычно показывает «всё»
    # в пределах общего safety-капа.
    try:
        result = executor.execute(
            query, row_limit=executor.max_rows_cap, params=params,
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
        "table": table_md,
        "row_count": result.row_count,
        "truncated": result.truncated,
    }
