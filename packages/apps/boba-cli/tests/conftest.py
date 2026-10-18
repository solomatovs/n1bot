"""Фикстуры стенда `boba acp`, общие для тестов команды, бинарника и
истории в Postgres: фейковая модель и регистрация MCP-over-ACP клиента; опция
`--acp-tck` — путь к исполняемому файлу acp-tck для test_cli_tck.py, опция
`--boba-bin` — каталог bin собранного выпуска для test_cli_binary.py."""

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
