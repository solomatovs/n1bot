"""Ручной прогон индексации Confluence: функция вызывается напрямую.

Конфиг прогона берётся из [tool.ingest], а хранилище — тестовая база набора:
схема базы знаний готовится в ней боевыми миграциями, рабочая база знаний
приложения не затрагивается.
"""

from __future__ import annotations

from typing import ClassVar

import pytest
from omegaconf import DictConfig
from psycopg import sql

from boba.config import bind
from boba.db.pgvector.migrations import Migrations
from boba.db.postgres import AsyncPostgresPool
from boba.stand.database import TestDatabase
from boba.tool.confluence.ingest_tools import (
    IngestToolConfig,
    confluence_index_page,
)
from boba.toolkit.entry import ToolMain
from boba.toolkit.ports import ToolProgress

pytestmark = [pytest.mark.run, pytest.mark.anyio]


class RunArgs:
    """Аргументы прогона: правятся перед запуском."""

    PAGE_ID: ClassVar[str] = "983136"

    ATTACHMENTS: ClassVar[bool] = False

    OCR: ClassVar[bool] = False


@pytest.fixture(scope="module")
async def ingest_cfg(raw_config: DictConfig) -> IngestToolConfig:
    """Конфиг прогона с хранилищем в тестовой базе набора. База берётся здесь,
    а не сессионной фикстурой: цикл событий у модуля свой."""
    cfg = bind(raw_config, path="tool.ingest", model=IngestToolConfig)
    database = await TestDatabase.ensure(cfg.connection)
    store = cfg.model_copy(
        update={"connection": TestDatabase.config_of(cfg.connection, database)}
    )
    schema = sql.SQL("create schema if not exists {}").format(
        sql.Identifier(store.tables.pg_schema)
    )

    pool = AsyncPostgresPool(store.connection)
    await pool.open()
    try:
        async with pool.connection() as conn:
            await conn.execute(schema)
            await Migrations(store.tables).apply(conn)
            await Migrations(store.tables).ensure_vector_index(
                conn, store.embedding.dim
            )
    finally:
        await pool.close()

    return store


async def test_run_confluence_ingest(ingest_cfg: IngestToolConfig) -> None:
    body = ToolMain.toolset(confluence_index_page)[0].coroutine
    if body is None:
        raise AssertionError("body is not None")

    content = (
        await body(
            page_id=RunArgs.PAGE_ID,
            attachments=RunArgs.ATTACHMENTS,
            ocr=RunArgs.OCR,
            cfg=ingest_cfg,
            progress=ToolProgress(-1),
        )
    ).llm_view()

    print(content)
