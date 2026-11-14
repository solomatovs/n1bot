"""Изолированные проверки агента; PostgreSQL задаётся явно для интеграционных тестов."""

from __future__ import annotations

import asyncio
import os
import tomllib
from collections.abc import AsyncIterator
from pathlib import Path

import psycopg
import pytest
import uvicorn
from pydantic import PositiveFloat, TypeAdapter
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from boba.agent.history import HistoryStore
from boba.agent.settings import ProviderSettings
from boba.db.postgres import AsyncPostgresPool
from boba.db.postgres.connection import PostgresConfig
from boba.krb import KerberosWorkspace
from boba.transport.http import HttpTransportConfig
from boba.transport.http.connection import NoneAuth


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
) -> AsyncIterator[psycopg.AsyncConnection]:
    async with (
        await AsyncPostgresPool.dedicated(postgres) as conn,
        conn.transaction(force_rollback=True),
    ):
        yield conn


@pytest.fixture
async def history(postgres: PostgresConfig) -> AsyncIterator[HistoryStore]:
    pool = AsyncPostgresPool(postgres)
    await pool.open()
    try:
        yield HistoryStore(pool)
    finally:
        await pool.close()


class InferenceServer:
    """Локальный HTTP-сервер с заданным ответом и записью полученного тела."""

    def __init__(self, port: int, timeout: float) -> None:
        self.requests: list[bytes] = []
        self.response = Response(status_code=501)
        self.responses: tuple[Response, ...] = ()
        self.settings = ProviderSettings(
            base_url=f"http://127.0.0.1:{port}/v1",
            auth=NoneAuth(method="none"),
            retry_statuses={503: 3},
            retry_backoff_sec=0,
            timeout_sec=max(120, timeout),
            transport=HttpTransportConfig(stream_stall_sec=timeout),
        )
        app = Starlette(
            routes=[Route("/v1/chat/completions", self.handle, methods=["POST"])]
        )
        self.server = uvicorn.Server(
            uvicorn.Config(
                app, host="127.0.0.1", port=port, log_level="error", ws="none"
            )
        )

    async def handle(self, request: Request) -> Response:
        self.requests.append(await request.body())

        if self.responses:
            index = len(self.requests) - 1
            if index >= len(self.responses):
                return Response("Unexpected inference request", status_code=500)

            return self.responses[index]

        return self.response


@pytest.fixture
async def inference(
    free_tcp_port: int, agent_timeout: float
) -> AsyncIterator[InferenceServer]:
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
