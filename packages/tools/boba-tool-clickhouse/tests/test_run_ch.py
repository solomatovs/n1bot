"""Ручной прогон ch-инструментов: функции вызываются напрямую с явным cfg.

Соединение берётся из [tool.ch] конфига приложения, аргументы прогона
задаются в RunArgs.
"""

from __future__ import annotations

from typing import ClassVar

import pytest

from boba.db.clickhouse import ClickHouseConfig
from boba.settings import bind
from boba.tool.ch.tools import ChToolConfig, ch_list_tables, ch_query
from boba.toolkit.entry import ToolMain

pytestmark = [pytest.mark.run, pytest.mark.anyio]


class RunArgs:
    """Аргументы прогона: правятся перед запуском."""

    CONNECTION: ClassVar[str] = "main"

    SQL: ClassVar[str] = "select 1 as answer"


@pytest.fixture(scope="module")
def ch_cfg(raw_config) -> ChToolConfig:
    """Лимиты из [tool.ch], whitelist — сервисный [clickhouse] под именем main."""
    limits = bind(raw_config, path="tool.ch", model=ChToolConfig)
    service = bind(raw_config, path="clickhouse", model=ClickHouseConfig)
    return limits.model_copy(update={"profiles": {RunArgs.CONNECTION: service}})


async def test_run_ch_query(ch_cfg: ChToolConfig) -> None:
    body = ToolMain.toolset(ch_query)[0].coroutine
    if body is None:
        raise AssertionError("body is not None")

    content, artifact = await body(
        sql=RunArgs.SQL, connection_name=RunArgs.CONNECTION, cfg=ch_cfg
    )

    print(content)
    print(artifact)


async def test_run_ch_list_tables(ch_cfg: ChToolConfig) -> None:
    body = ToolMain.toolset(ch_list_tables)[0].coroutine
    if body is None:
        raise AssertionError("body is not None")

    content, _artifact = await body(
        connection_name=RunArgs.CONNECTION,
        ch_database=None,
        offset=0,
        max_rows=50,
        max_chars=20000,
        cfg=ch_cfg,
    )

    print(content)
