"""Старт плагинов с таблицами: boba-mcp на базе без расширения vector выходит
с ошибкой, которая называет плагин, базу и недостающие расширения, а не висит.

Таблицы kb создаёт сам плагин при старте процесса (PluginStart); база из
template0 расширений не несёт, а создать их может только суперпользователь.

pytest -m integration.
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from psycopg import sql

from boba.db.postgres import AsyncPostgresPool
from boba.db.postgres.connection import PostgresConfig
from boba.stand.names import StandNames
from boba.stand.ui.stand import REPO_ROOT, ServiceStand, StandApp, StandPorts

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

EXIT_SEC = 90.0
"""Срок выхода: импорты сервиса, старт плагинов и закрытие контейнера."""


@pytest.fixture
async def bare_database(stand_postgres: PostgresConfig) -> AsyncIterator[str]:
    """Пустая база из template0: без vector, pg_trgm, unaccent и btree_gin."""
    name = StandNames().of("mcp_no_vector")
    drop = sql.SQL("drop database if exists {} with (force)").format(
        sql.Identifier(name)
    )
    create = sql.SQL("create database {} template template0").format(
        sql.Identifier(name)
    )
    async with await AsyncPostgresPool.dedicated(stand_postgres) as conn:
        await conn.execute(drop)
        await conn.execute(create)

    try:
        yield name
    finally:
        async with await AsyncPostgresPool.dedicated(stand_postgres) as conn:
            await conn.execute(drop)


async def test_service_without_vector_exits_naming_the_extensions(
    tmp_path: Path, bare_database: str
) -> None:
    config = ServiceStand(
        workdir=tmp_path,
        app_port=StandPorts().free(),
        db_name=bare_database,
        sandbox=False,
        plugins=("kb",),
    )
    config.write()
    log_path = tmp_path / "mcp.log"
    command = [
        sys.executable,
        "-m",
        StandApp.MCP.module,
        "--config",
        str(config.files.config),
        "--site",
        str(config.files.site),
    ]

    with log_path.open("wb") as log:
        process = subprocess.Popen(
            command,
            env=config.env(),
            cwd=str(REPO_ROOT),
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        try:
            code = process.wait(timeout=EXIT_SEC)
        except subprocess.TimeoutExpired as exc:
            process.kill()
            process.wait()
            tail = "\n".join(log_path.read_text(errors="replace").splitlines()[-40:])
            msg = f"boba-mcp did not exit in {EXIT_SEC}s without vector:\n{tail}"
            raise AssertionError(msg) from exc

    output = log_path.read_text(encoding="utf-8", errors="replace")
    tail = "\n".join(output.splitlines()[-40:])
    assert code != 0, tail
    assert "plugin 'kb' ([tool.kb]) could not prepare its tables" in output, tail
    assert f"/{bare_database}" in output, tail
    assert "vector, pg_trgm, unaccent, btree_gin must exist" in output, tail
