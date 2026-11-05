"""MCP: явная миграция хранилища и планы плагинов без DDL при старте.

pytest -m integration.
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import AsyncIterator, Iterable, Iterator, Sequence
from pathlib import Path
from typing import Any, ClassVar

import psycopg
import pytest
from omegaconf import DictConfig
from psycopg import sql

from boba.db.postgres import AsyncPostgresPool
from boba.db.postgres.connection import PostgresConfig
from boba.stand.migrate import StandMigrations
from boba.stand.names import StandNames
from boba.stand.overlay import ConfigOverlay
from boba.stand.refs import StandRefs
from boba.stand.toolsetup import ToolSetup
from boba.stand.ui.stand import REPO_ROOT, ServiceStand, StandApp, StandPorts
from boba.toolrun.plugins import PluginKits

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

EXIT_SEC = 90.0
"""Срок ожидания: импорты сервиса и старт плагинов."""


class BareDatabase:
    """Пустая база стенда из template0 для одного теста: без vector, pg_trgm,
    unaccent и btree_gin.

    Сервис ходит в неё ролью из конфига стенда, а создаёт и удаляет её роль
    stand_postgres, которой чужие сессии не завершить. Поэтому drop ждёт, пока
    сессии вышедшего процесса закроются; если они не закрылись в срок, drop
    падает и называет их pid, роль и application_name.
    """

    DRAIN_SEC: ClassVar[float] = 15.0
    POLL_SEC: ClassVar[float] = 0.1

    def __init__(self, postgres: PostgresConfig, name: str) -> None:
        self._postgres = postgres
        self.name = name

    async def create(self) -> None:
        create = sql.SQL("create database {} template template0").format(
            sql.Identifier(self.name)
        )
        async with await AsyncPostgresPool.dedicated(self._postgres) as conn:
            await self._drop(conn)
            await conn.execute(create)

    async def drop(self) -> None:
        async with await AsyncPostgresPool.dedicated(self._postgres) as conn:
            await self._drop(conn)

    async def _drop(self, conn: psycopg.AsyncConnection[Any]) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.DRAIN_SEC

        held = await self._sessions(conn)
        while held:
            if loop.time() >= deadline:
                sessions = "; ".join(held)
                msg = (
                    f"dropping the stand database {self.name}: expected no "
                    f"sessions {self.DRAIN_SEC}s after the service exited, "
                    f"still open: {sessions}"
                )
                raise AssertionError(msg)

            await asyncio.sleep(self.POLL_SEC)
            held = await self._sessions(conn)

        drop = sql.SQL("drop database if exists {}").format(sql.Identifier(self.name))
        await conn.execute(drop)

    async def _sessions(self, conn: psycopg.AsyncConnection[Any]) -> Sequence[str]:
        cursor = await conn.execute(
            "select pid, usename, application_name, state from pg_stat_activity"
            " where datname = %s and pid <> pg_backend_pid()",
            (self.name,),
        )
        rows = await cursor.fetchall()
        return list(self._described(rows))

    def _described(self, rows: Iterable[tuple[Any, ...]]) -> Iterator[str]:
        for pid, user, application, state in rows:
            yield f"pid {pid} of role {user} ({application!r}, {state})"


@pytest.fixture
async def bare_database(stand_postgres: PostgresConfig) -> AsyncIterator[str]:
    database = BareDatabase(stand_postgres, StandNames().database("mcp_no_vector"))
    await database.create()

    try:
        yield database.name
    finally:
        await database.drop()


async def test_migrate_reports_missing_extension_permissions(
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
    command = [
        sys.executable,
        "-m",
        StandApp.MCP.module,
        "migrate",
        *config.files.arguments(StandApp.MCP)[1:],
    ]
    process = await asyncio.create_subprocess_exec(
        *command,
        env=config.env(),
        cwd=str(REPO_ROOT),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, error = await asyncio.wait_for(process.communicate(), EXIT_SEC)
    text = error.decode()
    assert process.returncode != 0
    assert "vector" in text
    assert "permission" in text


async def test_start_has_no_ddl_and_sandbox_returns_a_migration_plan(
    runtime_stand: StandRefs,
    raw_config: DictConfig,
    config_overlay: ConfigOverlay,
    test_database: str,
    test_postgres: PostgresConfig,
) -> None:
    """Загрузка плагина не создаёт таблицы; отдельный план исполняет мигратор."""
    schema = StandNames().of_tree("kb_setup")
    changes = ToolSetup.sandbox_changes()
    changes["tool.kb.connection.dbname"] = test_database
    changes["tool.kb.tables.pg_schema"] = schema
    for plugin_id in raw_config["tool"]:
        changes[f"tool.{plugin_id}.enable"] = plugin_id == "kb"

    raw = config_overlay.tree(changes)

    async with await AsyncPostgresPool.dedicated(test_postgres) as conn:
        await conn.execute(
            sql.SQL("drop schema if exists {} cascade").format(sql.Identifier(schema))
        )

    try:
        kits = await PluginKits(raw, runtime_stand.launchers(raw)).load()
        launch = kits["kb"].launch
        assert launch.manifest.setup
        async with await AsyncPostgresPool.dedicated(test_postgres) as conn:
            cursor = await conn.execute(
                "select table_name from information_schema.tables"
                " where table_schema = %s",
                (schema,),
            )
            assert await cursor.fetchall() == []
        launchers = runtime_stand.launchers(raw)
        document = await asyncio.to_thread(
            launchers.migration_of, launch, launch.section
        )
        await StandMigrations().storage(test_postgres, document)

        async with await AsyncPostgresPool.dedicated(test_postgres) as conn:
            cursor = await conn.execute(
                "select table_name from information_schema.tables"
                " where table_schema = %s",
                (schema,),
            )
            tables = {row[0] for row in await cursor.fetchall()}
    finally:
        async with await AsyncPostgresPool.dedicated(test_postgres) as conn:
            await conn.execute(
                sql.SQL("drop schema if exists {} cascade").format(
                    sql.Identifier(schema)
                )
            )

    assert {"kb_chunks", "kb_collections", "kb_sources"} <= tables, tables
