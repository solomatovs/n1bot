"""Tool `query` + `QueryConfig`: произвольный SQL → markdown-таблица."""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import Field

from boba.markdown import format_markdown_table
from boba.settings import BobaFlatSettings, BobaSettingsConfigDict
from boba.tool.pg.executor import SqlExecutor, SqlExecutorConfig, SqlQueryError
from boba.tools import FromConfig, tool

__all__ = ["QueryConfig", "query"]


class QueryConfig(BobaFlatSettings):
    """Self-contained конфиг tool'а `query`.

    Config-секция: `[tool.pg.query]`.
    """

    model_config = BobaSettingsConfigDict(
        case_sensitive=False,
        extra="ignore",
        config_path="tool.pg.query",
        defaults_from=("postgres",),
    )

    executor: SqlExecutorConfig


@tool
def query(
    cfg: Annotated[QueryConfig, FromConfig()],
    sql: Annotated[
        str,
        Field(
            min_length=1,
            description=(
                "Произвольный SQL-запрос. Разрешены только read-only "
                "операции — это гарантируется правами DSN-роли и "
                "`default_transaction_read_only=on` в DSN. Попытка "
                "INSERT/UPDATE/DROP/etc упадёт с `permission denied` или "
                "`cannot execute X in a read-only transaction`."
            ),
        ),
    ],
    row_limit: Annotated[
        int,
        Field(
            ge=1,
            description=(
                "Сколько строк вернуть. По умолчанию 20; жёсткий потолок — "
                "`cfg.executor.max_rows`."
            ),
        ),
    ] = 20,
) -> dict[str, Any]:
    """Выполнить произвольный SQL-запрос; результат — markdown-таблица.

    Подходит для exploratory-аналитики и ad-hoc отчётов. Перед вызовом
    стоит позвать `list_tables` (что доступно) и `describe_table`
    (схема нужной таблицы).

    Возвращает `{table: "<markdown>", columns: [...], row_count: N,
    limit_applied: M, truncated: bool}`. `truncated=true` → есть ещё
    строки сверх `row_limit`; увеличь его и повтори запрос.
    """
    executor = SqlExecutor(cfg=cfg.executor)
    try:
        result = executor.execute(sql, row_limit=row_limit)
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
