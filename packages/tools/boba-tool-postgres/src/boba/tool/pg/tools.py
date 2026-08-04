"""SQL-инструменты только на чтение: список профилей, таблиц, схема, запрос."""

from __future__ import annotations

from typing import Annotated, Any, ClassVar

from langchain.tools import tool
from langchain_core.tools import BaseTool
from pydantic import Field

from boba.tool.pg.caller import PgCaller
from boba.tool.pg.copy_buffer import (
    BufferCapacityError,
    RowLimitExceededError,
)
from boba.tool.pg.executor import (
    SqlExecutor,
    SqlExecutorConfig,
    SqlQueryError,
)
from boba.toolkit.launcher import LauncherFactory
from boba.toolkit.result import (
    ErrorResult,
    PgCopyTextResult,
    TableResult,
    ToolResult,
    pack_result,
)

__all__ = ["PgTools", "build_pg_tools"]


class PgTools:
    """Собирает langchain-инструменты поверх SqlExecutor."""

    TABLES_SQL: ClassVar[str] = (
        "SELECT table_schema AS schema, table_name AS table, table_type AS kind "
        "FROM information_schema.tables "
    )
    COLUMNS_SQL: ClassVar[str] = (
        "SELECT column_name, data_type, is_nullable, column_default "
        "FROM information_schema.columns "
        "WHERE table_schema = %s AND table_name = %s "
        "ORDER BY ordinal_position"
    )

    def __init__(
        self,
        cfg: SqlExecutorConfig,
        launchers: LauncherFactory,
    ) -> None:
        self._caller = PgCaller("pg", launchers)
        self._cfg = cfg

    def build(self) -> list[BaseTool]:
        return [
            self._list_targets(),
            self._list_tables(),
            self._describe_table(),
            self._query(),
        ]

    @property
    def _executor(self) -> SqlExecutor:
        return SqlExecutor(cfg=self._cfg, caller=self._caller)

    @staticmethod
    def _note(executor: SqlExecutor, truncated: bool) -> str | None:
        if not truncated:
            return None
        return f"список усечён до max_rows ({executor.max_rows_cap})"

    @staticmethod
    def _failed(error: SqlQueryError) -> ErrorResult:
        return ErrorResult(message=str(error), error_kind="sql_failed")

    @staticmethod
    def _unknown_target(error: ValueError) -> ErrorResult:
        return ErrorResult(message=str(error), error_kind="unknown_target")

    def _list_targets(self) -> BaseTool:
        cfg = self._cfg

        @tool(response_format="content_and_artifact")
        def list_targets() -> tuple[str, ToolResult]:
            """Список доступных значений параметра target для PG-инструментов."""
            rows = [{"target": target} for target in cfg.targets()]
            return pack_result(TableResult(rows=rows))

        return list_targets

    def _list_tables(self) -> BaseTool:
        owner = self

        @tool(response_format="content_and_artifact")
        def list_tables(
            connection_name: Annotated[
                str,
                Field(min_length=1, description="Имя подключения"),
            ],
            pg_schema: Annotated[
                str | None,
                Field(
                    description=(
                        "Опциональный фильтр по схеме (например `public`). "
                        "Пусто = все user-schema'ы "
                        "(без pg_catalog/information_schema)."
                    ),
                ),
            ] = None,
        ) -> tuple[str, ToolResult]:
            """Список таблиц/view на профиле target. Колонки: schema, table, kind."""
            executor = owner._executor
            if pg_schema:
                sql = owner.TABLES_SQL + (
                    "where table_schema = %s order by table_schema, table_name"
                )
                params: tuple[Any, ...] = (pg_schema,)
            else:
                sql = owner.TABLES_SQL + (
                    "where 1=1 order by table_schema, table_name"
                )
                params = ()

            try:
                result = executor.execute(
                    sql,
                    connection_name=connection_name,
                    row_limit=executor.max_rows_cap,
                    params=params,
                )
            except ValueError as e:
                return pack_result(owner._unknown_target(e))
            except SqlQueryError as e:
                return pack_result(owner._failed(e))

            return pack_result(
                TableResult(
                    rows=result.rows,
                    note=owner._note(executor, result.truncated),
                )
            )

        return list_tables

    def _describe_table(self) -> BaseTool:
        owner = self

        @tool(response_format="content_and_artifact")
        def describe_table(
            connection_name: Annotated[
                str,
                Field(min_length=1, description="Имя коннекшина БД"),
            ],
            table: Annotated[
                str,
                Field(min_length=1, description="Имя таблицы (без схемы)"),
            ],
            pg_schema: Annotated[
                str,
                Field(
                    min_length=1,
                    description="PG schema таблицы. По умолчанию public",
                ),
            ] = "public",
        ) -> tuple[str, ToolResult]:
            """Схема таблицы на профиле target: колонки, типы, nullable, default."""
            executor = owner._executor
            try:
                result = executor.execute(
                    owner.COLUMNS_SQL,
                    connection_name=connection_name,
                    row_limit=executor.max_rows_cap,
                    params=(pg_schema, table),
                )
            except ValueError as e:
                return pack_result(owner._unknown_target(e))
            except SqlQueryError as e:
                return pack_result(owner._failed(e))

            return pack_result(
                TableResult(
                    rows=result.rows,
                    note=owner._note(executor, result.truncated),
                )
            )

        return describe_table

    def _query(self) -> BaseTool:
        owner = self

        @tool(response_format="content_and_artifact")
        def query(
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
                Field(min_length=1, description="Имя подключения"),
            ],
        ) -> tuple[str, ToolResult]:
            """Выполнить read-only SQL на профиле target, результат — CSV."""
            executor = owner._executor
            try:
                text = executor.execute_copy(sql, connection_name=target)
            except BufferCapacityError:
                return pack_result(
                    ErrorResult(
                        message=(
                            f"результат превысил лимит {executor.max_bytes} "
                            f"байт; добавьте LIMIT в запрос"
                        ),
                        error_kind="result_too_large",
                    )
                )
            except RowLimitExceededError:
                return pack_result(
                    ErrorResult(
                        message=(
                            f"запрос вернул больше {executor.max_rows_cap} "
                            f"строк; добавьте LIMIT в запрос"
                        ),
                        error_kind="too_many_rows",
                    )
                )
            except ValueError as e:
                return pack_result(owner._unknown_target(e))
            except SqlQueryError as e:
                return pack_result(owner._failed(e))

            return pack_result(PgCopyTextResult(text=text))

        return query


def build_pg_tools(
    cfg: SqlExecutorConfig,
    launchers: LauncherFactory,
) -> list[BaseTool]:
    return PgTools(cfg, launchers).build()
