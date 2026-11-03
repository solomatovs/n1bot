"""Ручной прогон confluence-чтения: функции вызываются напрямую с явным cfg.

Профиль соединения берётся из [tool.confluence] конфига приложения.
"""

from __future__ import annotations

from typing import ClassVar

import pytest

from boba.config import bind
from boba.connection_broker.crypto import SecretCipher
from boba.connection_broker.store import ConnectionStore
from boba.connections.manifest import ConnectionTypes
from boba.db.postgres import AsyncPostgresPool
from boba.mcp_server.app import McpAppConfig
from boba.stand.ui.stand import StandApp
from boba.tool.confluence.tools import (
    ConfluenceToolsConfig,
    confluence_fetch,
    confluence_search,
    confluence_spaces,
)
from boba.tool_io.entry import ToolMain
from boba.transport.http.connection import HttpConnection

pytestmark = [pytest.mark.run, pytest.mark.anyio]


class RunArgs:
    """Аргументы прогона: правятся перед запуском."""

    PAGE_ID: ClassVar[str] = "983136"

    QUERY: ClassVar[str] = "данные"


@pytest.fixture(scope="module")
def confluence_cfg(raw_config) -> ConfluenceToolsConfig:
    return bind(raw_config, path="tool.confluence", model=ConfluenceToolsConfig)


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


async def test_run_confluence_spaces(
    confluence_cfg: ConfluenceToolsConfig, confluence_connection: HttpConnection
) -> None:
    body = ToolMain.toolset(confluence_spaces)[0].coroutine
    if body is None:
        raise AssertionError("body is not None")

    content = (
        await body(limit=10, connection=confluence_connection, cfg=confluence_cfg)
    ).llm_view()

    print(content)


async def test_run_confluence_search(
    confluence_cfg: ConfluenceToolsConfig, confluence_connection: HttpConnection
) -> None:
    body = ToolMain.toolset(confluence_search)[0].coroutine
    if body is None:
        raise AssertionError("body is not None")

    content = (
        await body(
            query=RunArgs.QUERY,
            limit=5,
            offset=0,
            connection=confluence_connection,
            cfg=confluence_cfg,
        )
    ).llm_view()

    print(content)


async def test_run_confluence_fetch(
    confluence_cfg: ConfluenceToolsConfig, confluence_connection: HttpConnection
) -> None:
    body = ToolMain.toolset(confluence_fetch)[0].coroutine
    if body is None:
        raise AssertionError("body is not None")

    content = (
        await body(
            page_id=RunArgs.PAGE_ID,
            connection=confluence_connection,
            cfg=confluence_cfg,
        )
    ).llm_view()

    print(content[:500])
