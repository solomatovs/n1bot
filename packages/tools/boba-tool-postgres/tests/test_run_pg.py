"""Ручной прогон операций postgres: PostgresOps вызывается напрямую.

Соединение берётся из [tool.pg.profiles] конфига приложения, аргументы вызова
задаются в RunArgs.
"""

from __future__ import annotations

from typing import ClassVar

import pytest

from boba.settings import bind
from boba.tool.pg import PgExecutorConfig
from boba.tool.pg.payload import PostgresOps
from boba.tool.pg.protocol import PgCopyRequest, PgQueryRequest

pytestmark = [pytest.mark.run, pytest.mark.anyio]


class RunArgs:
    """Аргументы прогона: правятся перед запуском."""

    CONNECTION: ClassVar[str] = "main"

    SQL: ClassVar[str] = "select 1 as answer"

    ROW_LIMIT: ClassVar[int] = 100

    MAX_BYTES: ClassVar[int] = 1_000_000


@pytest.fixture(scope="module")
def pg_connection(raw_config):
    cfg = bind(raw_config, path="tool.pg", model=PgExecutorConfig)

    return cfg.profiles[RunArgs.CONNECTION]


async def test_run_pg_query(pg_connection, payload, chunks) -> None:
    request = PgQueryRequest(
        op=PgQueryRequest.OP,
        connection=pg_connection,
        sql=RunArgs.SQL,
        params=(),
        row_limit=RunArgs.ROW_LIMIT,
    )

    trailer = await PostgresOps.query(payload.of(request), chunks.write)

    print(chunks.rows())
    print(trailer)


async def test_run_pg_copy(pg_connection, payload, chunks) -> None:
    request = PgCopyRequest(
        op=PgCopyRequest.OP,
        connection=pg_connection,
        sql=RunArgs.SQL,
        max_bytes=RunArgs.MAX_BYTES,
    )

    trailer = await PostgresOps.copy(payload.of(request), chunks.write)

    print(chunks.text())
    print(trailer)
