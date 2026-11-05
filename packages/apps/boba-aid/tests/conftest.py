"""Фикстуры тестов aid: определены в aid_stand и acp_stand, здесь
регистрация, метка набора в именах стенда и группы pytest-xdist."""

from enum import StrEnum
from pathlib import Path
from typing import ClassVar

import pytest
from acp_stand import AgentHistoryStand
from acp_stand import model as model
from acp_stand import standard as standard
from aid_stand import *  # noqa: F403 — модуль стенда и есть набор фикстур
from psycopg import sql

from boba.db.postgres import AsyncPostgresPool
from boba.stand.collection import SuiteItems
from boba.stand.migrate import StandMigrations
from boba.stand.names import StandSuite
from boba.stand.site import Stand
from boba.stand.ui.database import run_blocking


def pytest_configure(config: pytest.Config) -> None:
    """Метка набора и процесса в именах стенда ставится до импорта модулей тестов."""
    StandSuite(config).configure("aid")


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Группы ставятся раньше хука xdist: он читает их в том же событии. Хук
    получает тесты всего прогона, группы набора идут только его тестам."""
    groups = AidGroups()
    for item in SuiteItems(Path(__file__)).own(items):
        item.add_marker(pytest.mark.xdist_group(groups.of(item)))


class AidGroup(StrEnum):
    """Группы xdist, общие для нескольких модулей."""

    E2E = "e2e"


class AidGroups:
    """Группа xdist теста aid: модуль целиком, чтобы модульные фикстуры
    поднимались один раз. e2e-модули запускают настоящее приложение с общим
    именем инстанса и портом BOBA_E2E_PORT и идут одним процессом по очереди."""

    E2E_MODULES: ClassVar[frozenset[str]] = frozenset({"test_canvas_e2e.py"})
    SEPARATOR: ClassVar[str] = "::"

    def of(self, item: pytest.Item) -> str:
        if item.path.name in self.E2E_MODULES:
            return AidGroup.E2E.value

        return item.nodeid.split(self.SEPARATOR, 1)[0]


@pytest.fixture(scope="session")
def agent_history_storage(stand: Stand) -> None:
    run_blocking(_prepare_agent_history(stand))


async def _prepare_agent_history(stand: Stand) -> None:
    history = AgentHistoryStand(stand)
    postgres = history.postgres()
    async with await AsyncPostgresPool.dedicated(postgres) as conn:
        await conn.execute(
            sql.SQL("drop schema if exists {} cascade").format(
                sql.Identifier(history.schema())
            )
        )
    await StandMigrations().application(postgres, history.schema(), "boba.cli")


@pytest.fixture(autouse=True)
def prepare_agent_history(request: pytest.FixtureRequest) -> None:
    if "stand" not in request.fixturenames:
        return

    stand = request.getfixturevalue("stand")
    if not isinstance(stand, Stand):
        return

    request.getfixturevalue("agent_history_storage")
