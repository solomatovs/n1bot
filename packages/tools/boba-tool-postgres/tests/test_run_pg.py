"""Ручной прогон pg-инструментов: функции вызываются напрямую с явным cfg.

Соединение берётся из [tool.pg] конфига приложения, аргументы прогона
задаются в RunArgs.
"""

from __future__ import annotations

from typing import ClassVar

import pytest

from boba.config import bind
from boba.db.postgres.profile import PostgresConfig
from boba.tool.pg.tools import PgToolConfig, pg_copy, pg_list_tables, pg_query
from boba.toolkit.entry import ToolMain

pytestmark = [pytest.mark.run, pytest.mark.anyio]


class RunArgs:
    """Аргументы прогона: правятся перед запуском."""

    CONNECTION: ClassVar[str] = "main"

    SQL: ClassVar[str] = "select 1 as answer"


@pytest.fixture(scope="module")
def pg_cfg(raw_config) -> PgToolConfig:
    """Лимиты из [tool.pg], whitelist — сервисный [postgres] под именем main."""
    limits = bind(raw_config, path="tool.pg", model=PgToolConfig)
    service = bind(raw_config, path="postgres", model=PostgresConfig)
    return limits.model_copy(update={"profiles": {RunArgs.CONNECTION: service}})


async def test_run_pg_query(pg_cfg: PgToolConfig) -> None:
    body = ToolMain.toolset(pg_query)[0].coroutine
    if body is None:
        raise AssertionError("body is not None")

    content, artifact = await body(
        connection_name=RunArgs.CONNECTION, sql=RunArgs.SQL, cfg=pg_cfg
    )

    print(content)
    print(artifact)


async def test_run_pg_list_tables(pg_cfg: PgToolConfig) -> None:
    body = ToolMain.toolset(pg_list_tables)[0].coroutine
    if body is None:
        raise AssertionError("body is not None")

    content, _artifact = await body(
        connection_name=RunArgs.CONNECTION,
        pg_schema="public",
        table_pattern=None,
        offset=0,
        max_rows=50,
        max_chars=20000,
        cfg=pg_cfg,
    )

    print(content)


async def test_run_pg_copy(pg_cfg: PgToolConfig) -> None:
    body = ToolMain.toolset(pg_copy)[0].coroutine
    if body is None:
        raise AssertionError("body is not None")

    content, _artifact = await body(
        connection_name=RunArgs.CONNECTION, sql=RunArgs.SQL, cfg=pg_cfg
    )

    print(content)
