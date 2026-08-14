"""ClickHouse-инструменты: профили, таблицы, схема, запрос — на общем SQL-слое."""

from __future__ import annotations

from typing import Annotated, Any, ClassVar

from langchain.tools import tool
from langchain_core.tools import BaseTool
from pydantic import Field

from boba.db.clickhouse import ClickHouseConfig
from boba.toolkit.launcher import LauncherFactory
from boba.toolkit.result import ToolResult, pack_result
from boba.toolkit.sql import (
    CatalogQuery,
    SqlErrors,
    SqlExecutor,
    SqlPack,
    SqlPayloadCaller,
    SqlProfiles,
    SqlQueryRequest,
)

__all__ = [
    "ChCaller",
    "ChCatalog",
    "ChExecutorConfig",
    "ChQueryRequest",
    "ChTools",
    "build_ch_tools",
]

ChParams = dict[str, Any]
"""Именованные параметры ClickHouse под подстановку {name:Type}."""


class ChExecutorConfig(SqlProfiles[ClickHouseConfig]):
    """Конфиг секции [tool.ch]."""

    SECTION: ClassVar[str] = "tool.ch"

    profiles: dict[str, ClickHouseConfig] = Field(
        default_factory=dict,
        description=(
            "dict[connection_name, clickhouse-профиль ссылкой]: "
            '`[tool.ch.profiles] main = "${clickhouse.main}"`. '
            "Ключ — значение tool-arg `connection_name` (LLM выбирает БД по нему)."
        ),
    )


class ChQueryRequest(SqlQueryRequest[ClickHouseConfig, ChParams]):
    """Запрос строк с лимитом; параметры именованные, под {name:Type}."""

    OP: ClassVar[str] = "ch_query"


class ChCaller(SqlPayloadCaller[ClickHouseConfig, ChParams]):
    """Вызов clickhouse-payload'а."""

    ENTRY: ClassVar[tuple[str, ...]] = ("python3", "-m", "boba.tool.ch.payload")

    REQUEST: ClassVar[type[SqlQueryRequest[Any, Any]]] = ChQueryRequest


class ChCatalog:
    """Каталожные запросы ClickHouse: фильтры уезжают именованными параметрами."""

    SYSTEM_DATABASES: ClassVar[str] = (
        "('system', 'INFORMATION_SCHEMA', 'information_schema')"
    )

    TABLES_SELECT: ClassVar[str] = """
select
    database,
    name as table,
    engine,
    total_rows
from
    system.tables
"""
    TABLES_ORDER: ClassVar[str] = """
order by
    database,
    name
"""
    COLUMNS_SELECT: ClassVar[str] = """
select
    name,
    type,
    default_kind,
    default_expression,
    comment
from
    system.columns
"""
    COLUMNS_ORDER: ClassVar[str] = """
order by
    position
"""

    @classmethod
    def tables(cls, ch_database: str | None) -> CatalogQuery[ChParams]:
        """Таблицы и view; без фильтра системные базы исключаются."""
        params: ChParams = {}

        if ch_database:
            condition = "database = {db:String}"
            params["db"] = ch_database
        else:
            condition = f"database not in {cls.SYSTEM_DATABASES}"

        text = cls._assemble(cls.TABLES_SELECT, [condition], cls.TABLES_ORDER)

        return CatalogQuery(text=text, params=params)

    @classmethod
    def columns(cls, table: str, ch_database: str | None) -> CatalogQuery[ChParams]:
        """Колонки таблицы; без базы берётся база по умолчанию у подключения."""
        params: ChParams = {"table": table}
        conditions = ["table = {table:String}"]

        if ch_database:
            conditions.append("database = {db:String}")
            params["db"] = ch_database
        else:
            conditions.append("database = currentDatabase()")

        text = cls._assemble(cls.COLUMNS_SELECT, conditions, cls.COLUMNS_ORDER)

        return CatalogQuery(text=text, params=params)

    @classmethod
    def _assemble(cls, select: str, conditions: list[str], order: str) -> str:
        where = "\n    and ".join(conditions)

        return f"{select}where\n    {where}\n{order}"


class ChTools:
    """Собирает langchain-инструменты поверх общего SqlExecutor."""

    def __init__(
        self,
        cfg: ChExecutorConfig,
        launchers: LauncherFactory,
    ) -> None:
        self._caller = ChCaller("ch", launchers)
        self._cfg = cfg
        self._errors = SqlErrors(max_rows=cfg.max_rows, max_bytes=cfg.max_bytes)

    def build(self) -> list[BaseTool]:
        return [
            self._list_targets(),
            self._list_tables(),
            self._describe_table(),
            self._query(),
        ]

    @property
    def _executor(self) -> SqlExecutor[ClickHouseConfig, ChParams]:
        return SqlExecutor(cfg=self._cfg, caller=self._caller)

    async def _catalog(
        self,
        query: CatalogQuery[ChParams],
        connection_name: str,
    ) -> ToolResult:
        """Каталожный запрос: отказ упаковывается тем же пакером, что и данные."""
        executor = self._executor
        try:
            result = await executor.execute(
                query.text,
                connection_name=connection_name,
                row_limit=executor.max_rows_cap,
                params=query.params,
            )
        except SqlErrors.CATCHES as e:
            return self._errors.pack(e)

        return SqlPack.result(result, self._errors)

    def _list_targets(self) -> BaseTool:
        cfg = self._cfg

        @tool(response_format="content_and_artifact")
        async def ch_list_targets() -> tuple[str, ToolResult]:
            """Список доступных значений connection_name для ClickHouse-инструментов."""
            return pack_result(SqlPack.targets(cfg))

        return ch_list_targets

    def _list_tables(self) -> BaseTool:
        owner = self

        @tool(response_format="content_and_artifact")
        async def ch_list_tables(
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
            query = ChCatalog.tables(ch_database)

            return pack_result(await owner._catalog(query, connection_name))

        return ch_list_tables

    def _describe_table(self) -> BaseTool:
        owner = self

        @tool(response_format="content_and_artifact")
        async def ch_describe_table(
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
            query = ChCatalog.columns(table, ch_database)

            return pack_result(await owner._catalog(query, connection_name))

        return ch_describe_table

    def _query(self) -> BaseTool:
        owner = self

        @tool(response_format="content_and_artifact")
        async def ch_query(
            sql: Annotated[
                str,
                Field(
                    min_length=1,
                    description=(
                        "Произвольный SQL ClickHouse. Если строк больше лимита "
                        "— добавьте LIMIT в сам запрос."
                    ),
                ),
            ],
            connection_name: Annotated[
                str,
                Field(min_length=1, description="Имя подключения"),
            ],
        ) -> tuple[str, ToolResult]:
            """Выполнить SQL на подключении connection_name."""
            executor = owner._executor
            try:
                result = await executor.execute(
                    sql,
                    connection_name=connection_name,
                    row_limit=executor.max_rows_cap,
                    params={},
                )
            except SqlErrors.CATCHES as e:
                return pack_result(owner._errors.pack(e))

            return pack_result(SqlPack.result(result, owner._errors))

        return ch_query


def build_ch_tools(
    cfg: ChExecutorConfig,
    launchers: LauncherFactory,
) -> list[BaseTool]:
    return ChTools(cfg, launchers).build()
