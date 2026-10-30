"""Старт плагинов с таблицами: хранилище готовит подкоманда setup программы
плагина по секции, где keytab-профиль заменён билетом вызова.

Таблицы kb создаёт setup в песочнице плагина; boba-mcp на базе без
расширения vector выходит с ошибкой, которая называет плагин, базу и
недостающие расширения, а не висит: база из template0 расширений не несёт, а
создать их может только суперпользователь.

pytest -m integration.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
from collections.abc import AsyncIterator, Iterable, Iterator, Sequence
from pathlib import Path
from typing import Any, ClassVar

import psycopg
import pytest
from omegaconf import DictConfig
from psycopg import sql

from boba.auth.credentials import KerberosCredentialSource, NoRefresh
from boba.connection_broker.tickets import SectionTickets, ServiceTickets
from boba.connections.manifest import ConnectionTypes
from boba.db.postgres import AsyncPostgresPool
from boba.db.postgres.connection import PostgresConfig
from boba.runtime.plugins import PluginKits
from boba.stand.names import StandNames
from boba.stand.overlay import ConfigOverlay
from boba.stand.refs import StandRefs
from boba.stand.toolsetup import ToolSetup
from boba.stand.ui.stand import REPO_ROOT, ServiceStand, StandApp, StandPorts
from boba.stand_core.context import CallStand

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

EXIT_SEC = 90.0
"""Срок выхода: импорты сервиса, старт плагинов и закрытие контейнера."""


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
        *config.files.arguments(StandApp.MCP),
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
    assert "plugin kb: `setup`" in output, tail
    assert "preparing its storage failed" in output, tail
    assert f"/{bare_database}" in output, tail
    assert "vector, pg_trgm, unaccent, btree_gin must exist" in output, tail


def _credentials() -> KerberosCredentialSource:
    return KerberosCredentialSource(None, NoRefresh())


async def test_setup_in_the_sandbox_creates_the_kb_tables(
    runtime_stand: StandRefs,
    call_stand: CallStand,
    raw_config: DictConfig,
    test_database: str,
    test_postgres: PostgresConfig,
) -> None:
    """kb объявил setup: на старте его программа в песочнице создаёт таблицы
    в своей схеме, keytab-профиль уехал туда билетом вызова."""
    sandboxed = ToolSetup.sandbox_raw(raw_config)
    schema = StandNames().of_tree("kb_setup")
    # таблицы, заданные ссылкой, наложение сливает с развёрнутой копией
    changes: dict[str, object] = {
        "tool.kb.connection.dbname": test_database,
        "tool.kb.tables.pg_schema": schema,
    }
    for plugin_id in sandboxed["tool"]:
        changes[f"tool.{plugin_id}.enable"] = plugin_id == "kb"

    raw = ConfigOverlay(sandboxed).merged(changes)
    arming = SectionTickets(ServiceTickets(_credentials), ConnectionTypes.discover)

    async with await AsyncPostgresPool.dedicated(test_postgres) as conn:
        await conn.execute(
            sql.SQL("drop schema if exists {} cascade").format(sql.Identifier(schema))
        )

    try:
        kits = await PluginKits(raw, runtime_stand.launchers(raw), arming).load()
        assert kits["kb"].launch.manifest.setup

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
