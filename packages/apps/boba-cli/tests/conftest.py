"""Фикстуры стенда `boba acp`, общие для тестов команды, бинарника и
истории в Postgres: фейковая модель, регистрация MCP-over-ACP клиента, база
стенда со своей схемой истории на тест; опция `--acp-tck` — путь к
исполняемому файлу acp-tck для test_cli_tck.py, опция `--boba-bin` — каталог
bin собранного выпуска для test_cli_binary.py."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from omegaconf import DictConfig
from psycopg import sql
from test_cli_acp import AcpProcess, FakeModel, StandRegistration
from test_cli_acp_postgres import PostgresStand

from boba.config import bind
from boba.db.postgres import AsyncPostgresPool
from boba.db.postgres.connection import PostgresConfig
from boba.stand.database import TestDatabase
from boba.stand.ui.fake_llm import ScenarioName


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--acp-tck",
        action="store",
        default=None,
        metavar="PATH",
        help="acp-tck executable; without it test_cli_tck.py fails",
    )
    parser.addoption(
        "--boba-bin",
        action="store",
        default=None,
        metavar="DIR",
        help=(
            "bin directory of the built release for the binary tests; "
            "default: release/<version>/bin of the main tree"
        ),
    )


@pytest.fixture
def tck_executable(request: pytest.FixtureRequest) -> Path:
    """Исполняемый файл acp-tck из `--acp-tck`; без опции или с
    несуществующим путём тест падает."""
    given = request.config.getoption("acp_tck", default=None)
    if given is None:
        pytest.fail("acp-tck is not configured: pass --acp-tck <path to acp-tck>")

    path = Path(str(given))
    if not path.is_file():
        pytest.fail(f"--acp-tck {path} is not a file")

    return path


@pytest.fixture
async def model() -> AsyncIterator[FakeModel]:
    fake = FakeModel()
    await fake.start()
    try:
        yield fake
    finally:
        await fake.stop()


@pytest.fixture
async def boba(model: FakeModel, tmp_path: Path) -> AsyncIterator[AcpProcess]:
    """Процесс `boba acp` над фейковой моделью; убивается после теста."""
    process = AcpProcess(tmp_path, model)
    try:
        yield process
    finally:
        await process.kill()


@pytest.fixture
async def answering_model() -> AsyncIterator[FakeModel]:
    """Фейковая модель, которая отвечает и на вопрос без маркера: TCK шлёт
    обычный текст. Медленный ответ на вопрос отмены кладёт книгой сам тест."""
    fake = FakeModel(unmarked=ScenarioName.ANSWER)
    await fake.start()
    try:
        yield fake
    finally:
        await fake.stop()


@pytest.fixture
def registration() -> StandRegistration:
    return StandRegistration([StandRegistration.STAND], 0)


@pytest.fixture
async def postgres(raw_config: DictConfig) -> PostgresConfig:
    cfg = bind(raw_config, path="postgres", model=PostgresConfig)
    database = await TestDatabase.ensure(cfg)

    return TestDatabase.config_of(cfg, database)


@pytest.fixture
async def pool(postgres: PostgresConfig) -> AsyncIterator[AsyncPostgresPool]:
    opened = AsyncPostgresPool(postgres)
    await opened.open()
    try:
        yield opened
    finally:
        await opened.close()


@pytest.fixture
async def schema(pool: AsyncPostgresPool) -> AsyncIterator[str]:
    """Своя схема истории на тест; после теста сносится."""
    name = f"acp_hist_{uuid.uuid4().hex[:12]}"
    yield name
    async with pool.connection() as conn:
        await conn.execute(
            sql.SQL("drop schema if exists {} cascade").format(sql.Identifier(name))
        )


@pytest.fixture
def stand(postgres: PostgresConfig, schema: str) -> PostgresStand:
    return PostgresStand(postgres, schema)
