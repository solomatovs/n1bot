"""Ручной прогон pg-инструментов: функции вызываются напрямую.

Профиль соединения в бою подаёт хост из строк пользователя; здесь он
берётся из сервисной секции [postgres] и передаётся параметром.
"""

from __future__ import annotations

from typing import ClassVar

import pytest

from boba.config import bind
from boba.db.postgres.connection import PostgresConfig
from boba.tool.pg.tools import PgToolConfig, pg_list_tables, pg_query
from boba.toolkit.entry import ToolMain

pytestmark = [pytest.mark.run, pytest.mark.anyio]


class RunArgs:
    """Аргументы прогона: правятся перед запуском."""

    SQL: ClassVar[str] = "select 1 as answer"


@pytest.fixture(scope="module")
def pg_cfg(raw_config) -> PgToolConfig:
    """Лимиты выдачи из [tool.pg]."""
    return bind(raw_config, path="tool.pg", model=PgToolConfig)


@pytest.fixture(scope="module")
def connection(raw_config) -> PostgresConfig:
    """Профиль соединения: сервисный [postgres] на месте строки пользователя."""
    return bind(raw_config, path="postgres", model=PostgresConfig)


async def test_run_pg_query(pg_cfg: PgToolConfig, connection: PostgresConfig) -> None:
    body = ToolMain.toolset(pg_query)[0].coroutine
    if body is None:
        raise AssertionError("body is not None")

    artifact = await body(connection=connection, sql=RunArgs.SQL, offset=0, limit=50)

    content = artifact.llm_view()

    print(content)
    print(artifact)


async def test_run_pg_list_tables(
    pg_cfg: PgToolConfig, connection: PostgresConfig
) -> None:
    body = ToolMain.toolset(pg_list_tables)[0].coroutine
    if body is None:
        raise AssertionError("body is not None")

    content = (
        await body(
            connection=connection,
            pg_schema="public",
            table_pattern=None,
            offset=0,
            limit=50,
        )
    ).llm_view()

    print(content)
