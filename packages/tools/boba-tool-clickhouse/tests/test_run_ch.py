"""Ручной прогон ch-инструментов: функции вызываются напрямую.

Профиль соединения в бою подаёт хост из строк пользователя; здесь он
берётся из сервисной секции [clickhouse] и передаётся параметром.
"""

from __future__ import annotations

from typing import ClassVar

import pytest

from boba.config import bind
from boba.db.clickhouse.connection import ClickHouseConfig
from boba.tool.ch.tools import ChToolConfig, ch_query
from boba.toolkit.entry import ToolMain
from boba.toolkit.ports import ToolProgress

pytestmark = [pytest.mark.run, pytest.mark.anyio]


class RunArgs:
    """Аргументы прогона: правятся перед запуском."""

    SQL: ClassVar[str] = "select 1 as answer"


@pytest.fixture(scope="module")
def ch_cfg(raw_config) -> ChToolConfig:
    """Лимиты выдачи из [tool.ch]."""
    return bind(raw_config, path="tool.ch", model=ChToolConfig)


@pytest.fixture(scope="module")
def connection(raw_config) -> ClickHouseConfig:
    """Профиль соединения: сервисный [clickhouse] на месте строки пользователя."""
    return bind(raw_config, path="clickhouse", model=ClickHouseConfig)


async def test_run_ch_query(ch_cfg: ChToolConfig, connection: ClickHouseConfig) -> None:
    body = ToolMain.toolset(ch_query)[0].coroutine
    if body is None:
        raise AssertionError("body is not None")

    artifact = await body(
        progress=ToolProgress(-1),
        sql=RunArgs.SQL,
        connection=connection,
        offset=0,
        limit=50,
    )

    print(artifact.llm_view())
    print(artifact)
