"""Команды migrate трёх приложений на настоящем PostgreSQL без запуска серверов."""

from __future__ import annotations

import asyncio
import shutil
import sys
from pathlib import Path
from uuid import uuid4

import pytest
from psycopg import sql

from boba.aid.config.app import AppConfig
from boba.db.postgres import AsyncPostgresPool, PostgresSchema
from boba.db.postgres.connection import PostgresConfig
from boba.krb import KerberosWorkspace
from boba.stand.ui.toml_text import TomlText

pytestmark = [pytest.mark.integration, pytest.mark.anyio]


@pytest.mark.parametrize(
    ("module", "table"),
    [
        ("boba.aid.main", "mcp_tokens"),
        ("boba.cli.app", "session_records"),
        ("boba.mcp_server.program", "connections"),
    ],
)
async def test_migrate_uses_only_storage_config_and_reconciles_repeat(  # noqa: PLR0913 — фикстуры стенда
    module: str,
    table: str,
    tmp_path: Path,
    test_postgres: PostgresConfig,
    runtime_config: AppConfig,
    pool: AsyncPostgresPool,
) -> None:
    name = f"migrate_cli_{uuid4().hex[:12]}"
    instance = runtime_config.paths.model_copy(
        update={"data_dir": str(tmp_path / "data")}
    )
    connection = test_postgres.model_dump(mode="json", exclude_none=True)
    location = {"schema": name, "postgres": "${postgres}"}
    app: dict[str, object] = {"data": "${data}", **location}
    if module == "boba.cli.app":
        app["agent"] = {"history": location}

    document: dict[str, object] = {
        "data": instance.data_dir,
        "postgres": connection,
        "app": app,
    }
    config = tmp_path / "config.toml"
    config.write_text(TomlText.dumps(document), encoding="utf-8")
    shutil.copytree(Path(KerberosWorkspace.krb5_config()).parent, tmp_path / "krb")

    try:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            module,
            "migrate",
            "--config",
            str(config),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        output, errors = await asyncio.wait_for(process.communicate(), 60)
        assert process.returncode == 0, errors.decode()
        assert b"migrate: completed" in output
        async with pool.connection() as conn:
            assert await PostgresSchema(name).has_table(conn, table)
            if module == "boba.aid.main":
                assert await PostgresSchema(name).has_table(conn, "grants")
                assert not await PostgresSchema(name).has_table(conn, "connections")
            if module == "boba.mcp_server.program":
                assert await PostgresSchema(name).has_table(conn, "grants")

        repeat = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            module,
            "migrate",
            "--config",
            str(config),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        output, errors = await asyncio.wait_for(repeat.communicate(), 60)
        assert repeat.returncode == 0, errors.decode()
        assert b"migrate: completed" in output
    finally:
        async with pool.connection() as conn:
            await conn.execute(
                sql.SQL("drop schema if exists {} cascade").format(sql.Identifier(name))
            )
