"""Tool describe_table: схема одной таблицы."""

from __future__ import annotations

from typing import Annotated

from pydantic import Field

from boba.markdown import format_markdown_table
from boba.settings import BobaFlatSettings, BobaSettingsConfigDict
from boba.tool.pg.executor import SqlExecutor, SqlExecutorConfig, SqlQueryError
from boba.tools import FromConfig, tool

__all__ = ["DescribeTableConfig", "describe_table"]


class DescribeTableConfig(BobaFlatSettings):
    """Конфиг tool describe_table (секция [tool.pg.describe_table])."""

    model_config = BobaSettingsConfigDict(
        case_sensitive=False,
        extra="ignore",
        config_path="tool.pg.describe_table",
        defaults_from=("tool.pg",),
    )

    executor: SqlExecutorConfig


@tool
def describe_table(
    cfg: Annotated[DescribeTableConfig, FromConfig()],
    target: Annotated[
        str,
        Field(
            min_length=1,
            description=(
                "Имя профиля БД"
            ),
        ),
    ],
    table: Annotated[
        str,
        Field(
            min_length=1,
            description=(
                "Имя таблицы (без схемы)"
            ),
        ),
    ],
    schema: Annotated[
        str,
        Field(
            min_length=1,
            description="PG schema таблицы. По умолчанию public",
        ),
    ] = "public",
) -> str:
    """Схема таблицы на профиле target: колонки, типы, nullable, default.

    Если таблицы нет, вернётся markdown с заглушкой (no rows).
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
            target=target,
            row_limit=executor.max_rows_cap,
            params=(schema, table),
        )
    except SqlQueryError as e:
        raise RuntimeError(str(e)) from e

    return format_markdown_table(
        columns=result.columns,
        rows=result.rows,
        max_cell_chars=executor.max_cell_chars,
        truncated=result.truncated,
    )
