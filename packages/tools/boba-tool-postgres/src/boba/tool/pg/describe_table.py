"""Tool `describe_table` + `DescribeTableConfig`: схема одной таблицы.

LLM зовёт после `list_tables`, чтобы узнать структуру конкретной
таблицы перед написанием SQL. Возвращает markdown с колонками
`{column_name, data_type, is_nullable, default}`.
"""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import Field

from boba.markdown import format_markdown_table
from boba.settings import BobaFlatSettings, BobaSettingsConfigDict
from boba.tool.pg.executor import SqlExecutor, SqlExecutorConfig, SqlQueryError
from boba.tools import FromConfig, tool

__all__ = ["DescribeTableConfig", "describe_table"]


class DescribeTableConfig(BobaFlatSettings):
    """Self-contained конфиг tool'а `describe_table`.

    Config-секция: `[tool.pg.describe_table]`.
    """

    model_config = BobaSettingsConfigDict(
        case_sensitive=False,
        extra="ignore",
        config_path="tool.pg.describe_table",
        defaults_from=("postgres",),
    )

    executor: SqlExecutorConfig


@tool
def describe_table(
    cfg: Annotated[DescribeTableConfig, FromConfig()],
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
    executor = SqlExecutor(cfg=cfg.executor)
    sql = (
        "SELECT column_name, data_type, is_nullable, column_default "
        "FROM information_schema.columns "
        "WHERE table_schema = %s AND table_name = %s "
        "ORDER BY ordinal_position"
    )
    try:
        result = executor.execute(
            sql,
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
