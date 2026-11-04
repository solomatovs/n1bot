"""Ручной прогон web-инструментов с web-профилем из каталога MCP."""

from __future__ import annotations

import pytest

from boba.config import bind
from boba.crypto import SecretCipher
from boba.mcp_server.connections.store import ConnectionStore
from boba.connections.manifest import ConnectionTypes
from boba.db.postgres import AsyncPostgresPool
from boba.mcp_server.app import McpAppConfig
from boba.stand.ui.stand import StandApp
from boba.tool.web.tools import WebToolsConfig, web_fetch_page, web_grep_page
from boba.tool_io.entry import ToolMain
from boba.tool_io.ports import PipeProgress
from boba.transport.http.connection import HttpConnection

pytestmark = [pytest.mark.run, pytest.mark.anyio]


@pytest.fixture(scope="module")
def web_cfg(raw_config) -> WebToolsConfig:
    """Лимиты выдачи из [tool.web]."""
    return bind(raw_config, path="tool.web", model=WebToolsConfig)


@pytest.fixture(scope="module")
async def connection() -> HttpConnection:
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


@pytest.fixture(scope="module")
def covered_url(connection: HttpConnection) -> str:
    return f"{connection.public_url()}/"


async def test_run_web_fetch(
    web_cfg: WebToolsConfig, connection: HttpConnection, covered_url: str
) -> None:
    body = ToolMain.toolset(web_fetch_page)[0].coroutine
    if body is None:
        raise AssertionError("body is not None")

    content = (
        await body(
            progress=PipeProgress(-1),
            url=covered_url,
            connection=connection,
            as_markdown=True,
            line_offset=0,
            line_count=20,
            cfg=web_cfg,
        )
    ).llm_view()

    print(content)


async def test_run_web_grep(
    web_cfg: WebToolsConfig, connection: HttpConnection, covered_url: str
) -> None:
    body = ToolMain.toolset(web_grep_page)[0].coroutine
    if body is None:
        raise AssertionError("body is not None")

    content = (
        await body(
            progress=PipeProgress(-1),
            url=covered_url,
            connection=connection,
            pattern=".",
            cfg=web_cfg,
        )
    ).llm_view()

    print(content)
