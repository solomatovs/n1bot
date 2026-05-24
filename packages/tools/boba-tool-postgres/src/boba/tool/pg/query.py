"""Tool `query` + `QueryConfig`: произвольный SQL → markdown-таблица."""

from __future__ import annotations

from typing import Annotated

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
                "Произвольный SQL-запрос. Разрешены только read-only"
            ),
        ),
    ],
    row_limit: Annotated[
        int,
        Field(
            ge=1,
            description=(
                "Сколько строк вернуть. По умолчанию 20"
            ),
        ),
    ] = 20,
) -> str:
    """Выполнить произвольный SQL-запрос; результат — markdown-таблица.

    Подходит для exploratory-аналитики и ad-hoc отчётов. Перед вызовом
    стоит позвать `list_tables` (что доступно) и `describe_table`
    (схема нужной таблицы).

    Если в таблице есть footer `_... more rows omitted (увеличьте row_limit)_`,
    значит есть ещё строки сверх `row_limit` — увеличь его и повтори запрос.
    """
    executor = SqlExecutor(cfg=cfg.executor)
    try:
        result = executor.execute(sql, row_limit=row_limit)
    except SqlQueryError as e:
        raise RuntimeError(str(e)) from e

    return format_markdown_table(
        columns=result.columns,
        rows=result.rows,
        max_cell_chars=executor.max_cell_chars,
        truncated=result.truncated,
    )
