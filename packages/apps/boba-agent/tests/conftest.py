"""Изолированные проверки агента; PostgreSQL задаётся явно для интеграционных тестов."""

from __future__ import annotations

import asyncio
import os
import re
import tomllib
from collections.abc import AsyncGenerator
from pathlib import Path

import psycopg
import pytest
from agent_stand import AgentSystem, InferenceServer
from psycopg.rows import dict_row
from pydantic import PositiveFloat, TypeAdapter

from boba.agent.history import HistoryStore
from boba.agent.models import HISTORY_RECORD_ADAPTER
from boba.db.postgres import AsyncPostgresPool
from boba.db.postgres.connection import PostgresConfig
from boba.krb import KerberosWorkspace


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("boba-agent")
    group.addoption(
        "--agent-config",
        default=os.environ.get("BOBA_AGENT_TEST_CONFIG"),
        help="Service TOML supplying the PostgreSQL connection",
    )
    group.addoption(
        "--agent-database",
        default=os.environ.get("BOBA_AGENT_TEST_DATABASE"),
        help="Database marked as the boba history-sql experiment",
    )
    group.addoption(
        "--agent-timeout",
        type=float,
        default=10,
        help="Test wait timeout in seconds; increase when debugging",
    )


@pytest.fixture
def agent_timeout(pytestconfig: pytest.Config) -> float:
    return TypeAdapter(PositiveFloat).validate_python(
        pytestconfig.getoption("--agent-timeout")
    )


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(scope="session")
def postgres_config(
    tmp_path_factory: pytest.TempPathFactory, pytestconfig: pytest.Config
) -> PostgresConfig:
    """Сервисный конфиг даёт подключение; имя тестовой БД задаётся отдельно."""
    path = pytestconfig.getoption("--agent-config")
    database = pytestconfig.getoption("--agent-database")
    if not path or not database:
        pytest.fail(
            "Set --agent-config and --agent-database "
            "(or BOBA_AGENT_TEST_CONFIG / BOBA_AGENT_TEST_DATABASE)"
        )
    config_path = Path(path).resolve()
    document = tomllib.loads(config_path.read_text(encoding="utf-8"))
    cfg = PostgresConfig.model_validate(document["postgres"])
    KerberosWorkspace.configure(
        str(config_path.parent / "krb" / "krb5.conf"),
        str(tmp_path_factory.mktemp("agent-krb")),
    )
    return cfg.model_copy(update={"dbname": database})


@pytest.fixture
async def postgres(postgres_config: PostgresConfig) -> PostgresConfig:
    async with await AsyncPostgresPool.dedicated(postgres_config) as conn:
        row = await (
            await conn.execute(
                "select shobj_description(oid, 'pg_database') "
                "from pg_database where datname = current_database()"
            )
        ).fetchone()
    if row != ("boba history-sql experiment v1",):
        pytest.fail("The database is not marked as the boba history-sql experiment")
    from boba.db.postgres.migrate import (
        MigrationScripts,
        MigrationTarget,
        PostgresMigrations,
    )

    plan = MigrationScripts("boba").application("boba.agent")
    await PostgresMigrations().apply([MigrationTarget(postgres_config, plan)])
    return postgres_config


@pytest.fixture
async def sql_connection(
    postgres: PostgresConfig,
) -> AsyncGenerator[psycopg.AsyncConnection, None]:
    async with (
        await AsyncPostgresPool.dedicated(postgres) as conn,
        conn.transaction(force_rollback=True),
    ):
        yield conn


@pytest.fixture
async def history(postgres: PostgresConfig) -> AsyncGenerator[HistoryStore, None]:
    pool = AsyncPostgresPool(postgres)
    await pool.open()
    try:
        yield HistoryStore(pool)
    finally:
        await pool.close()


@pytest.fixture
async def inference(
    free_tcp_port: int, agent_timeout: float
) -> AsyncGenerator[InferenceServer, None]:
    server = InferenceServer(free_tcp_port, agent_timeout)
    worker = asyncio.create_task(server.server.serve())
    try:
        async with asyncio.timeout(agent_timeout):
            while not server.server.started:
                if worker.done():
                    await worker
                    pytest.fail("Inference server stopped during startup")

                await asyncio.sleep(0.01)

        yield server

    finally:
        server.server.should_exit = True
        async with asyncio.timeout(agent_timeout):
            await worker


@pytest.fixture
async def system(
    history: HistoryStore,
    inference: InferenceServer,
    postgres: PostgresConfig,
    agent_timeout: float,
) -> AsyncGenerator[AgentSystem, None]:
    stand = AgentSystem(history, inference, postgres, agent_timeout)
    await stand.hub.start(postgres)
    await stand.seed_provider()
    try:
        yield stand
    finally:
        await stand.close()


@pytest.fixture
async def demo_records(sql_connection):
    root = Path(__file__).resolve().parents[4]
    for name in ("fixtures.sql", "openai_fixtures.sql"):
        source = (root / "experiments/history-sql" / name).read_text()
        source = re.sub(r"(?m)^(begin|commit);\s*$", "", source)
        await sql_connection.execute(source, prepare=False)

    async with psycopg.AsyncRawCursor(sql_connection, row_factory=dict_row) as cursor:
        await cursor.execute(
            "select to_jsonb(r) - 'scope' || jsonb_strip_nulls(jsonb_build_object('scope', r.scope)) as record "
            "from boba.records r where session_id in ('sql-acp-demo', 'sql-acp-actions') or session_id like 'openai-%' order by session_id, seq"
        )
        rows = [row["record"] for row in await cursor.fetchall()]

    for row in rows:
        if row["session_id"].startswith("openai-error-"):
            continue
        if row["kind"] == "custom_context_kind":
            continue
        HISTORY_RECORD_ADAPTER.validate_python(row)
    return rows


@pytest.fixture
async def task_runtime(history, postgres):
    from functools import partial

    from agent_stand import DirectTaskRuntime

    from boba.tasks.worker import TaskNotifications

    notifications = TaskNotifications(partial(AsyncPostgresPool.dedicated, postgres))
    await notifications.start()
    runtime = DirectTaskRuntime(history, notifications)
    try:
        yield runtime
    finally:
        await runtime.close()
        await notifications.close()
