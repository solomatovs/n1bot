"""Tool query: произвольный SQL в markdown-таблицу."""

from __future__ import annotations

from typing import Annotated

from pydantic import Field

from boba.markdown import format_markdown_table
from boba.settings import BobaFlatSettings, BobaSettingsConfigDict
from boba.tool.pg.executor import SqlExecutor, SqlExecutorConfig, SqlQueryError
from boba.tools import FromConfig, tool

__all__ = ["QueryConfig", "query"]


class QueryConfig(BobaFlatSettings):
    """Конфиг tool query (секция [tool.pg.query])."""

    model_config = BobaSettingsConfigDict(
        case_sensitive=False,
        extra="ignore",
        config_path="tool.pg.query",
        defaults_from=("tool.pg",),
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
    target: Annotated[
        str,
        Field(
            min_length=1,
            description=(
                "Имя подключения"
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
    """Выполнить SQL на профиле target и вернуть markdown-таблицу.

    Перед вызовом стоит позвать list_tables и describe_table.
    Footer "more rows omitted (увеличьте row_limit)" — есть ещё строки сверх row_limit.
    """
    executor = SqlExecutor(cfg=cfg.executor)
    try:
        result = executor.execute(sql, target=target, row_limit=row_limit)
    except SqlQueryError as e:
        raise RuntimeError(str(e)) from e

    return format_markdown_table(
        columns=result.columns,
        rows=result.rows,
        max_cell_chars=executor.max_cell_chars,
        truncated=result.truncated,
    )
