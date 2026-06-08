"""Tool query: произвольный SQL -> CSV (COPY TO STDOUT)."""

from __future__ import annotations

from typing import Annotated

from pydantic import Field

from boba.tool.pg.copy_buffer import BufferCapacityError, RowLimitExceededError
from boba.tool.pg.executor import SqlExecutor, SqlExecutorConfig, SqlQueryError
from boba.tools import FromConfig, tool
from boba.tools.domain import ErrorResult, PgCopyTextResult

__all__ = ["query"]


@tool
def query(
    cfg: Annotated[SqlExecutorConfig, FromConfig()],
    sql: Annotated[
        str,
        Field(
            min_length=1,
            description=(
                "Произвольный read-only SQL-запрос. Выполняется через "
                "COPY (...) TO STDOUT и возвращается как CSV. Если строк "
                "больше лимита — добавьте LIMIT в сам запрос."
            ),
        ),
    ],
    target: Annotated[
        str,
        Field(
            min_length=1,
            description=("Имя подключения"),
        ),
    ],
) -> PgCopyTextResult | ErrorResult:
    """Выполнить SQL на профиле target и вернуть результат как CSV.

    Перед вызовом стоит позвать list_tables и describe_table.
    Запрос исполняется как есть (COPY-обёртка); ошибки SQL уходят дословно.
    Слишком много строк / большой объём -> ошибка с просьбой добавить LIMIT.
    """
    executor = SqlExecutor(cfg=cfg)
    try:
        buf = executor.execute_copy(sql, target=target)
    except BufferCapacityError:
        return ErrorResult(
            message=(
                f"результат превысил лимит {executor.max_bytes} байт; "
                f"добавьте LIMIT в запрос"
            ),
            error_kind="result_too_large",
        )
    except RowLimitExceededError:
        return ErrorResult(
            message=(
                f"запрос вернул больше {executor.max_rows_cap} строк; "
                f"добавьте LIMIT в запрос"
            ),
            error_kind="too_many_rows",
        )
    except SqlQueryError as e:
        raise RuntimeError(str(e)) from e

    return PgCopyTextResult(text=buf.decode())
