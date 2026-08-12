"""ClickHouse-инструменты только на чтение: профили, таблицы, схема, запрос."""

from __future__ import annotations

from typing import Annotated, Any, ClassVar

from langchain.tools import tool
from langchain_core.tools import BaseTool
from pydantic import Field

from boba.tool.ch.caller import ChCaller
from boba.tool.ch.executor import (
    ChExecutor,
    ChExecutorConfig,
    ChQueryError,
)
from boba.toolkit.launcher import (
    CollectorCapacityError,
    CollectorRowLimitError,
    LauncherFactory,
)
from boba.toolkit.result import (
    ErrorResult,
    TableResult,
    ToolResult,
    pack_result,
)

__all__ = ["ChTools", "build_ch_tools"]


class ChTools:
    """Собирает langchain-инструменты поверх ChExecutor."""

    TABLES_SQL: ClassVar[str] = """
select
    database,
    name as table,
    engine,
    total_rows
from
    system.tables
"""
    SYSTEM_DATABASES: ClassVar[str] = (
        "('system', 'INFORMATION_SCHEMA', 'information_schema')"
    )
    COLUMNS_SQL: ClassVar[str] = """
select
    name,
    type,
    default_kind,
    default_expression,
    comment
from
    system.columns
"""

    def __init__(
        self,
        cfg: ChExecutorConfig,
        launchers: LauncherFactory,
    ) -> None:
        self._caller = ChCaller("ch", launchers)
        self._cfg = cfg

    def build(self) -> list[BaseTool]:
        return [
            self._list_targets(),
            self._list_tables(),
            self._describe_table(),
            self._query(),
        ]

    @property
    def _executor(self) -> ChExecutor:
        return ChExecutor(cfg=self._cfg, caller=self._caller)

    @staticmethod
    def _note(executor: ChExecutor, truncated: bool) -> str | None:
        if not truncated:
            return None
        return f"список усечён до max_rows ({executor.max_rows_cap})"

    @staticmethod
    def _failed(error: ChQueryError) -> ErrorResult:
        return ErrorResult(message=str(error), error_kind="sql_failed")

    @staticmethod
    def _unknown_target(error: ValueError) -> ErrorResult:
        return ErrorResult(message=str(error), error_kind="unknown_target")

    @staticmethod
    def _too_large(executor: ChExecutor) -> ErrorResult:
        return ErrorResult(
            message=(
                f"результат превысил лимит {executor.max_bytes} символов; "
                f"добавьте LIMIT в запрос"
            ),
            error_kind="result_too_large",
        )

    @staticmethod
    def _too_many_rows(executor: ChExecutor) -> ErrorResult:
        return ErrorResult(
            message=(
                f"запрос вернул больше {executor.max_rows_cap} строк; "
                f"добавьте LIMIT в запрос"
            ),
            error_kind="too_many_rows",
        )

    def _list_targets(self) -> BaseTool:
        cfg = self._cfg

        @tool(response_format="content_and_artifact")
        def ch_list_targets() -> tuple[str, ToolResult]:
            """Список доступных значений connection_name для ClickHouse-инструментов."""
            rows = [{"connection_name": target} for target in cfg.targets()]
            return pack_result(TableResult(rows=rows))

        return ch_list_targets

    def _list_tables(self) -> BaseTool:
        owner = self

        @tool(response_format="content_and_artifact")
        def ch_list_tables(
            connection_name: Annotated[
                str,
                Field(min_length=1, description="Имя подключения"),
            ],
            ch_database: Annotated[
                str | None,
                Field(
                    description=(
                        "Опциональный фильтр по базе (например `default`). "
                        "Пусто = все пользовательские базы "
                        "(без system/information_schema)."
                    ),
                ),
            ] = None,
        ) -> tuple[str, ToolResult]:
            """Список таблиц/view подключения. Колонки: database, table, engine."""
            executor = owner._executor
            params: dict[str, Any] = {}
            if ch_database:
                sql = owner.TABLES_SQL + (
                    "where\n"
                    "    database = {db:String}\n"
                    "order by\n"
                    "    database,\n"
                    "    name\n"
                )
                params = {"db": ch_database}
            else:
                sql = owner.TABLES_SQL + (
                    "where\n"
                    f"    database not in {owner.SYSTEM_DATABASES}\n"
                    "order by\n"
                    "    database,\n"
                    "    name\n"
                )

            try:
                result = executor.execute(
                    sql,
                    connection_name=connection_name,
                    params=params,
                )
            except ValueError as e:
                return pack_result(owner._unknown_target(e))
            except ChQueryError as e:
                return pack_result(owner._failed(e))

            return pack_result(
                TableResult(
                    rows=result.rows,
                    note=owner._note(executor, result.truncated),
                )
            )

        return ch_list_tables

    def _describe_table(self) -> BaseTool:
        owner = self

        @tool(response_format="content_and_artifact")
        def ch_describe_table(
            connection_name: Annotated[
                str,
                Field(min_length=1, description="Имя коннекшина БД"),
            ],
            table: Annotated[
                str,
                Field(min_length=1, description="Имя таблицы (без базы)"),
            ],
            ch_database: Annotated[
                str | None,
                Field(
                    description=(
                        "База таблицы; пусто — база по умолчанию у подключения."
                    ),
                ),
            ] = None,
        ) -> tuple[str, ToolResult]:
            """Схема таблицы: колонки, типы, default-выражения, комментарии."""
            executor = owner._executor
            params: dict[str, Any] = {"table": table}
            if ch_database:
                sql = owner.COLUMNS_SQL + (
                    "where\n"
                    "    database = {db:String}\n"
                    "    and table = {table:String}\n"
                    "order by\n"
                    "    position\n"
                )
                params["db"] = ch_database
            else:
                sql = owner.COLUMNS_SQL + (
                    "where\n"
                    "    database = currentDatabase()\n"
                    "    and table = {table:String}\n"
                    "order by\n"
                    "    position\n"
                )

            try:
                result = executor.execute(
                    sql,
                    connection_name=connection_name,
                    params=params,
                )
            except ValueError as e:
                return pack_result(owner._unknown_target(e))
            except ChQueryError as e:
                return pack_result(owner._failed(e))

            return pack_result(
                TableResult(
                    rows=result.rows,
                    note=owner._note(executor, result.truncated),
                )
            )

        return ch_describe_table

    def _query(self) -> BaseTool:
        owner = self

        @tool(response_format="content_and_artifact")
        def ch_query(
            sql: Annotated[
                str,
                Field(
                    min_length=1,
                    description=(
                        "Произвольный read-only SQL ClickHouse. Сессия открыта "
                        "с readonly, запись и DDL отклоняются сервером. Если "
                        "строк больше лимита — добавьте LIMIT в сам запрос."
                    ),
                ),
            ],
            connection_name: Annotated[
                str,
                Field(min_length=1, description="Имя подключения"),
            ],
        ) -> tuple[str, ToolResult]:
            """Выполнить read-only SQL на подключении connection_name."""
            executor = owner._executor
            try:
                result = executor.execute(
                    sql,
                    connection_name=connection_name,
                )
            except CollectorCapacityError:
                return pack_result(owner._too_large(executor))
            except CollectorRowLimitError:
                return pack_result(owner._too_many_rows(executor))
            except ValueError as e:
                return pack_result(owner._unknown_target(e))
            except ChQueryError as e:
                return pack_result(owner._failed(e))

            return pack_result(
                TableResult(
                    rows=result.rows,
                    note=owner._note(executor, result.truncated),
                )
            )

        return ch_query


def build_ch_tools(
    cfg: ChExecutorConfig,
    launchers: LauncherFactory,
) -> list[BaseTool]:
    return ChTools(cfg, launchers).build()
