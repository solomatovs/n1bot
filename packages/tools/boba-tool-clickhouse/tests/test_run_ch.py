"""Ручной прогон операций clickhouse: ClickHouseOps вызывается напрямую.

Соединение берётся из [tool.ch.profiles] конфига приложения, аргументы вызова
задаются в RunArgs.
"""

from __future__ import annotations

from typing import ClassVar

import pytest

from boba.settings import bind
from boba.tool.ch import ChExecutorConfig
from boba.tool.ch.payload import ClickHouseOps
from boba.tool.ch.protocol import ChQueryRequest

pytestmark = [pytest.mark.run, pytest.mark.anyio]


class RunArgs:
    """Аргументы прогона: правятся перед запуском."""

    CONNECTION: ClassVar[str] = "main"

    SQL: ClassVar[str] = "select 1 as answer"

    ROW_LIMIT: ClassVar[int] = 100


@pytest.fixture(scope="module")
def ch_connection(raw_config):
    cfg = bind(raw_config, path="tool.ch", model=ChExecutorConfig)

    return cfg.profiles[RunArgs.CONNECTION]


async def test_run_ch_query(ch_connection, payload, chunks) -> None:
    request = ChQueryRequest(
        op=ChQueryRequest.OP,
        connection=ch_connection,
        sql=RunArgs.SQL,
        params={},
        row_limit=RunArgs.ROW_LIMIT,
    )

    trailer = await ClickHouseOps.query(payload.of(request), chunks.write)

    print(chunks.rows())
    print(trailer)
