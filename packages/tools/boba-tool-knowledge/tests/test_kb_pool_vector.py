"""Пул базы знаний отдаёт соединения, знающие тип vector.

Процесс приложения открывает общий пул на том же подключении раньше, чем
kb-store просит свой: pytest повторяет этот порядок и проверяет, что store
всё равно работает с vector, а не получает чужие соединения без типа.

pytest -m integration.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import Any

import numpy as np
import pytest
from omegaconf import DictConfig
from psycopg import AsyncConnection, sql

from boba.config import bind
from boba.db.pgvector.config import PostgresStoreConfig, PostgresStoreSchema
from boba.db.pgvector.migrations import Migrations
from boba.db.pgvector.store import KbPool
from boba.db.postgres import AsyncPostgresPool
from boba.tool.kb.confluence.ingest_base import ConfluenceIngestConfig

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

SCHEMA = "kb_pool_test"
DIM = 4


@pytest.fixture(scope="module")
def anyio_backend() -> str:
    return "asyncio"




@pytest.fixture(scope="module")
async def store_cfg(raw_config: DictConfig) -> AsyncIterator[PostgresStoreConfig]:
    """Схема стенда под боевыми миграциями; сносится после модуля."""
    app_cfg = bind(raw_config, "tool.ingest", ConfluenceIngestConfig)
    tables = PostgresStoreSchema(
        pg_schema=SCHEMA,
        chunks_table="kb_chunks",
        collections_table="kb_collections",
        sources_table="kb_sources",
    )
    cfg = PostgresStoreConfig(connection=app_cfg.connection, tables=tables)

    pool = AsyncPostgresPool(cfg.connection)
    await pool.open()
    try:
        async with pool.connection() as conn:
            await _execute(conn, _drop_schema())
            await _execute(conn, sql.SQL("create schema {}").format(_schema()))
            await Migrations.apply_bootstrap(conn, schema_cfg=tables)
            await Migrations.ensure_vector_index(conn, dim=DIM, schema_cfg=tables)

        yield cfg
    finally:
        async with pool.connection() as conn:
            await _execute(conn, _drop_schema())

        await pool.close()


def _schema() -> sql.Identifier:
    return sql.Identifier(SCHEMA)


def _drop_schema() -> sql.Composed:
    return sql.SQL("drop schema if exists {} cascade").format(_schema())


async def _execute(conn: AsyncConnection[Any], statement: sql.Composed) -> None:
    async with conn.transaction():
        await conn.execute(statement)


async def _roundtrip(pool: Any, value: Sequence[float]) -> Any:
    """Вектор уходит параметром и возвращается: так его пишет и читает store."""
    async with pool.cursor() as cur:
        await cur.execute("select %s::vector", (np.array(value, dtype=np.float32),))
        row = await cur.fetchone()

    if row is None:
        raise AssertionError("select returns a row")

    return row[0]


async def test_kb_pool_knows_vector_after_a_plain_pool_is_opened(
    store_cfg: PostgresStoreConfig,
) -> None:
    """Общий пул, открытый раньше, не отбирает у store соединения с vector."""
    plain = await AsyncPostgresPool.get(store_cfg.connection)
    kb = await KbPool.open(store_cfg.connection)
    value = [0.5, 0.25, 0.125, 0.0625]

    try:
        returned = await _roundtrip(kb, value)
        if not isinstance(returned, np.ndarray):
            raise AssertionError(
                "the kb pool adapts vector values, got "
                f"{type(returned).__name__}: {returned!r}"
            )

        if [float(item) for item in returned] != value:
            raise AssertionError(f"vector survives the roundtrip, got {returned!r}")
    finally:
        await kb.close()
        await plain.close()
