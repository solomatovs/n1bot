"""Пустой каталог ролей не мешает запуску настоящего MCP-процесса."""

from pathlib import Path
from uuid import uuid4

import pytest
from psycopg import sql

from boba.db.postgres import AsyncPostgresPool
from boba.db.postgres.connection import PostgresConfig
from boba.mcp_server.migrations import configured_migrations
from boba.stand.migrate import StandSchema
from boba.stand.ui.config import ServiceStand, StandPorts
from boba.stand.ui.stand import StandProcess

pytestmark = [pytest.mark.integration, pytest.mark.anyio]


async def test_service_starts_without_roles(
    tmp_path: Path,
    test_database: str,
    test_postgres: PostgresConfig,
) -> None:
    config = ServiceStand(
        workdir=tmp_path,
        app_port=StandPorts().free(),
        db_name=test_database,
        sandbox=False,
        seed_roles=False,
        db_schema=f"no_roles_{uuid4().hex[:12]}",
    )
    config.write()
    schema = StandSchema(
        config.files.paths(config.app),
        "boba.mcp_server",
        configured_migrations(),
    )
    await schema.create()
    process = StandProcess(config=config, log_path=tmp_path / "mcp.log")
    try:
        process.start(30)
        assert process.process is not None
        assert process.process.poll() is None
        async with await AsyncPostgresPool.dedicated(test_postgres) as conn:
            query = sql.SQL("select count(*) from {}.roles").format(
                sql.Identifier(config.db_schema)
            )
            assert await (await conn.execute(query)).fetchone() == (0,)
    finally:
        process.stop()
        await schema.drop()
