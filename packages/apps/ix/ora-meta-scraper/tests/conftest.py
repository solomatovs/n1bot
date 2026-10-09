"""Фикстуры стенда скрапера и группы pytest-xdist; модели источников лежат в
ora_scraper_stand, общая часть стенда — в boba.stand.scraper."""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path

import pytest
from ora_scraper_stand import LAYOUT, IxStand

from boba.stand.collection import SuiteItems
from boba.stand.names import StandSuite
from boba.stand.scraper import Golden, ScraperStandDatabase, StandFile


def pytest_configure(config: pytest.Config) -> None:
    """Метка набора и процесса в именах стенда ставится до импорта модулей тестов."""
    StandSuite(config).configure("orascr")


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Группы ставятся раньше хука xdist: он читает их в том же событии. Хук
    получает тесты всего прогона, группы набора идут только его тестам."""
    groups = ScraperGroups(IxStand.required())
    for item in SuiteItems(Path(__file__)).own(items):
        item.add_marker(pytest.mark.xdist_group(groups.of(item)))


class ScraperGroup(StrEnum):
    """Группы xdist скрапера Oracle."""

    EDGE_DEMO = "edge_demo"
    MEMORY_MODULE = "test_ora_streaming_memory.py"


class ScraperGroups:
    """Группа xdist теста скрапера. Эталонный прогон, шторм и форматы работают в
    ora_scrape_sources на всех серверах и идут одним процессом; тест памяти работает в
    ora_bulk_sources и расходится по процессам по серверу. Где нагрузочная PDB та же,
    что у эталона, тест памяти идёт вместе с эталоном: чужой DDL сбил бы обход."""

    def __init__(self, stand: IxStand) -> None:
        self._stand = stand

    def of(self, item: pytest.Item) -> str:
        if item.path.name != ScraperGroup.MEMORY_MODULE:
            return ScraperGroup.EDGE_DEMO.value

        callspec = getattr(item, "callspec", None)
        if callspec is None:
            return ScraperGroup.EDGE_DEMO.value

        name = str(callspec.params["name"])
        if self._stand.bulk_shares_scrape(name):
            return ScraperGroup.EDGE_DEMO.value

        return name


@pytest.fixture(scope="session")
def ix_stand() -> IxStand:
    return IxStand.required()


@pytest.fixture(scope="session")
async def ix_database(ix_stand: IxStand) -> ScraperStandDatabase:
    database = ScraperStandDatabase(ix_stand, LAYOUT)
    await database.recreate_for_scraper()
    return database


@pytest.fixture(scope="session")
def golden() -> Golden:
    return Golden(LAYOUT.path(StandFile.GOLDEN))
