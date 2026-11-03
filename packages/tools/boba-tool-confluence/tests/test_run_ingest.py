"""Ручной прогон индексации Confluence: функция вызывается напрямую.

Конфиг прогона берётся из [tool.ingest], а хранилище — тестовая база набора:
схема базы знаний готовится в ней боевыми миграциями, рабочая база знаний
приложения не затрагивается.
"""

from __future__ import annotations

from importlib.resources import files
from typing import ClassVar

import pytest
from omegaconf import DictConfig
from psycopg import sql

from boba.config import bind
from boba.connection_broker.crypto import SecretCipher
from boba.connection_broker.store import ConnectionStore
from boba.connections.manifest import ConnectionTypes
from boba.db.pgvector.migrations import Migrations
from boba.db.postgres import AsyncPostgresPool, PostgresSchema
from boba.mcp_server.app import McpAppConfig
from boba.stand.database import TestDatabase
from boba.stand.ui.stand import StandApp
from boba.tool.confluence.ingest_tools import (
    IngestToolConfig,
    confluence_index_page,
)
from boba.tool_io.entry import ToolMain
from boba.tool_io.ports import PipeProgress
from boba.transport.http.connection import HttpConnection

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
            schema_dir = files("boba.db.pgvector") / "schema"
            names = Migrations(store.tables).names()
            await PostgresSchema(store.tables.pg_schema).apply_files(
                conn, schema_dir, names
            )
            await Migrations(store.tables).ensure_vector_index(
                conn, store.embedding.dim
            )
    finally:
        await pool.close()

    return store


@pytest.fixture(scope="module")
async def confluence_connection() -> HttpConnection:
    raw = StandApp.MCP.config_files().loader().load()
    app = bind(raw, "app", McpAppConfig)
    pool = AsyncPostgresPool(app.postgres)
    await pool.open()
    try:
        store = ConnectionStore(
            pool,
            app.db_schema,
            SecretCipher(app.connection_store.cipher.get_secret_value()),
            ConnectionTypes.discover(),
        )
        rows = [row for row in await store.list_all() if row.name == "confluence"]
    finally:
        await pool.close()

    if len(rows) != 1 or not isinstance(rows[0].connection, HttpConnection):
        raise AssertionError("MCP must contain one web connection named confluence")

    return rows[0].connection


async def test_run_confluence_ingest(
    ingest_cfg: IngestToolConfig, confluence_connection: HttpConnection
) -> None:
    body = ToolMain.toolset(confluence_index_page)[0].coroutine
    if body is None:
        raise AssertionError("body is not None")

    content = (
        await body(
            page_id=RunArgs.PAGE_ID,
            connection=confluence_connection,
            attachments=RunArgs.ATTACHMENTS,
            ocr=RunArgs.OCR,
            cfg=ingest_cfg,
            progress=PipeProgress(-1),
        )
    ).llm_view()

    print(content)
