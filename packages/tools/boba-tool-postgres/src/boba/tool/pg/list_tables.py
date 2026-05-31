"""Tool list_tables: список таблиц/view."""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import Field

from boba.settings import BobaFlatSettings, BobaSettingsConfigDict
from boba.tool.pg.executor import SqlExecutor, SqlExecutorConfig, SqlQueryError
from boba.tools import FromConfig, tool
from boba.tools.domain import TableResult

__all__ = ["ListTablesConfig", "list_tables"]


class ListTablesConfig(BobaFlatSettings):
    """Конфиг tool list_tables (секция [tool.pg.list_tables])."""

    model_config = BobaSettingsConfigDict(
        case_sensitive=False,
        extra="ignore",
        config_path="tool.pg.list_tables",
        defaults_from=("tool.pg",),
    )

    executor: SqlExecutorConfig


@tool
def list_tables(
    cfg: Annotated[ListTablesConfig, FromConfig()],
    target: Annotated[
        str,
        Field(
            min_length=1,
            description=(
                "Имя подключения"
            ),
        ),
    ],
    pg_schema: Annotated[
        str | None,
        Field(
            description=(
                "Опциональный фильтр по схеме (например `public`). "
                "Пусто = все user-schema'ы (без pg_catalog/information_schema)."
            ),
        ),
    ] = None,
) -> TableResult:
    """Список таблиц/view на профиле target. Колонки: schema, table, kind."""
    executor = SqlExecutor(cfg=cfg.executor)
    if pg_schema:
        sql = (
            "SELECT table_schema AS schema, table_name AS table, "
            "table_type AS kind "
            "FROM information_schema.tables "
            "WHERE table_schema = %s "
            "ORDER BY table_schema, table_name"
        )
        params: tuple[Any, ...] = (pg_schema,)
    else:
        sql = (
            "SELECT table_schema AS schema, table_name AS table, "
            "table_type AS kind "
            "FROM information_schema.tables "
            "WHERE table_schema NOT IN ('pg_catalog', 'information_schema') "
            "AND table_schema NOT LIKE %s "
            "ORDER BY table_schema, table_name"
        )
        params = ("pg_%",)

    try:
        result = executor.execute(
            sql, target=target, row_limit=executor.max_rows_cap, params=params,
        )
    except SqlQueryError as e:
        raise RuntimeError(str(e)) from e

    note = (
        f"список усечён до max_rows ({executor.max_rows_cap})"
        if result.truncated
        else None
    )
    return TableResult(
        rows=result.rows,
        note=note,
    )
