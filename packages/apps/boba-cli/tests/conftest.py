"""Фикстуры стенда `boba acp`, общие для тестов команды, бинарника и
истории в Postgres: фейковая модель и регистрация MCP-over-ACP клиента; опция
`--acp-tck` — путь к исполняемому файлу acp-tck для test_cli_tck.py."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from test_cli_acp import FakeModel, StandRegistration

from boba.stand.ui.fake_llm import ScenarioName


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--acp-tck",
        action="store",
        default=None,
        metavar="PATH",
        help="acp-tck executable; without it test_cli_tck.py is skipped",
    )


@pytest.fixture
def tck_executable(request: pytest.FixtureRequest) -> Path:
    """Исполняемый файл acp-tck из `--acp-tck`; без опции тест пропускается,
    несуществующий путь — ошибка прогона."""
    given = request.config.getoption("acp_tck", default=None)
    if given is None:
        pytest.skip("acp-tck is not configured: pass --acp-tck <path to acp-tck>")

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
async def answering_model() -> AsyncIterator[FakeModel]:
    """Фейковая модель, которая отвечает и на вопрос без маркера и выдаёт
    ответ медленно: TCK шлёт обычный текст и отменяет ход, пока он идёт."""
    fake = FakeModel(unmarked=ScenarioName.ANSWER, token_delay_sec=0.2)
    await fake.start()
    try:
        yield fake
    finally:
        await fake.stop()


@pytest.fixture
def registration() -> StandRegistration:
    return StandRegistration([StandRegistration.STAND])
