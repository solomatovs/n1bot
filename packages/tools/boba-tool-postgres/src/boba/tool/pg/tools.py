"""Postgres-инструменты: профили, таблицы, схема, запрос, выгрузка."""

from __future__ import annotations

from typing import Annotated

from langchain.tools import tool
from langchain_core.tools import BaseTool
from pydantic import Field

from boba.tool.pg.caller import PgCaller
from boba.tool.pg.catalog import PgCatalog
from boba.tool.pg.executor import PgExecutor, PgExecutorConfig
from boba.toolkit.launcher import LauncherFactory
from boba.toolkit.result import (
    PgCopyTextResult,
    ToolResult,
    pack_result,
)
from boba.toolkit.sql import SqlErrors, SqlPack

__all__ = ["PgTools", "build_pg_tools"]


class PgTools:
    """Собирает langchain-инструменты поверх PgExecutor."""

    def __init__(
        self,
        cfg: PgExecutorConfig,
        launchers: LauncherFactory,
    ) -> None:
        self._caller = PgCaller("pg", launchers)
        self._cfg = cfg
        self._errors = SqlErrors(max_rows=cfg.max_rows, max_bytes=cfg.max_bytes)

    def build(self) -> list[BaseTool]:
        return [
            self._list_targets(),
            self._list_tables(),
            self._describe_table(),
            self._query(),
            self._export(),
        ]

    @property
    def _executor(self) -> PgExecutor:
        return PgExecutor(cfg=self._cfg, caller=self._caller)

    def _list_targets(self) -> BaseTool:
        cfg = self._cfg

        @tool(response_format="content_and_artifact")
        async def pg_list_targets() -> tuple[str, ToolResult]:
            """Список доступных значений connection_name для postgres-инструментов."""
            return pack_result(SqlPack.targets(cfg))

        return pg_list_targets

    def _list_tables(self) -> BaseTool:
        owner = self

        @tool(response_format="content_and_artifact")
        async def pg_list_tables(
            connection_name: Annotated[
                str,
                Field(min_length=1, description="Имя подключения"),
            ],
            pg_schema: Annotated[
                str | None,
                Field(
                    description=(
                        "Схема; пусто — все схемы, включая системные "
                        "(pg_catalog, information_schema). Их много, и выдача "
                        "упрётся в max_rows — сузьте фильтр."
                    ),
                ),
            ] = None,
            table_pattern: Annotated[
                str | None,
                Field(
                    description=(
                        "Шаблон имени в синтаксисе LIKE: `kb_%`, `%log%`. "
                        "Пусто — без фильтра по имени."
                    ),
                ),
            ] = None,
        ) -> tuple[str, ToolResult]:
            """Таблицы и view подключения из pg_catalog.

            Колонки: schema, table_name, kind, approx_rows, owner, total_bytes,
            comment. kind: r таблица, p партиционированная, v view,
            m материализованное view, f сторонняя таблица. Сложные условия по
            каталогу пишутся запросом к pg_catalog через pg_query.
            """
            executor = owner._executor
            query = PgCatalog.tables(pg_schema, table_pattern)
            try:
                result = await executor.execute(
                    query.text,
                    connection_name=connection_name,
                    row_limit=executor.max_rows_cap,
                    params=query.params,
                )
            except SqlErrors.CATCHES as e:
                return pack_result(owner._errors.pack(e))

            return pack_result(SqlPack.result(result, owner._errors))

        return pg_list_tables

    def _describe_table(self) -> BaseTool:
        owner = self

        @tool(response_format="content_and_artifact")
        async def pg_describe_table(
            connection_name: Annotated[
                str,
                Field(min_length=1, description="Имя коннекшина БД"),
            ],
            table: Annotated[
                str,
                Field(min_length=1, description="Имя таблицы (без схемы)"),
            ],
            pg_schema: Annotated[
                str | None,
                Field(
                    description=(
                        "Схема таблицы; пусто — искать во всех схемах, "
                        "схема каждой найденной видна колонкой schema."
                    ),
                ),
            ] = None,
        ) -> tuple[str, ToolResult]:
            """Схема таблицы из pg_catalog: колонки, нативные типы, ключи.

            Колонки: schema, position, column_name, type, nullable,
            default_expression, identity, generated, primary_key, comment.
            """
            executor = owner._executor
            query = PgCatalog.columns(table, pg_schema)
            try:
                result = await executor.execute(
                    query.text,
                    connection_name=connection_name,
                    row_limit=executor.max_rows_cap,
                    params=query.params,
                )
            except SqlErrors.CATCHES as e:
                return pack_result(owner._errors.pack(e))

            return pack_result(SqlPack.result(result, owner._errors))

        return pg_describe_table

    def _query(self) -> BaseTool:
        owner = self

        @tool(response_format="content_and_artifact")
        async def pg_query(
            connection_name: Annotated[
                str,
                Field(min_length=1, description="Имя подключения"),
            ],
            sql: Annotated[
                str,
                Field(
                    min_length=1,
                    description=(
                        "Произвольный SQL. Запрос с выборкой возвращает строки; "
                        "если их больше лимита — добавьте LIMIT в сам запрос. "
                        "INSERT/UPDATE/DELETE/DDL возвращают число затронутых "
                        "строк и статус сервера."
                    ),
                ),
            ],
        ) -> tuple[str, ToolResult]:
            """Выполнить SQL на подключении: строки либо счётчик затронутых."""
            executor = owner._executor
            try:
                result = await executor.execute(
                    sql,
                    connection_name=connection_name,
                    row_limit=executor.max_rows_cap,
                    params=(),
                )
            except SqlErrors.CATCHES as e:
                return pack_result(owner._errors.pack(e))

            return pack_result(SqlPack.result(result, owner._errors))

        return pg_query

    def _export(self) -> BaseTool:
        owner = self

        @tool(response_format="content_and_artifact")
        async def pg_export(
            connection_name: Annotated[
                str,
                Field(min_length=1, description="Имя подключения"),
            ],
            sql: Annotated[
                str,
                Field(
                    min_length=1,
                    description=(
                        "Read-only SQL для выгрузки. Выполняется через "
                        "COPY (...) TO STDOUT и возвращается текстом; запись и "
                        "DDL внутри COPY сервер отклонит. Если строк больше "
                        "лимита — добавьте LIMIT в сам запрос."
                    ),
                ),
            ],
        ) -> tuple[str, ToolResult]:
            """Выгрузить результат SELECT текстом через COPY ... TO STDOUT."""
            executor = owner._executor
            try:
                text = await executor.execute_copy(sql, connection_name=connection_name)
            except SqlErrors.CATCHES as e:
                return pack_result(owner._errors.pack(e))

            return pack_result(PgCopyTextResult(text=text))

        return pg_export


def build_pg_tools(
    cfg: PgExecutorConfig,
    launchers: LauncherFactory,
) -> list[BaseTool]:
    return PgTools(cfg, launchers).build()
