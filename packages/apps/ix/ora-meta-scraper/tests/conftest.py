"""Фикстуры стенда скрапера; модели источников лежат в ora_scraper_stand, общая часть
стенда — в boba.stand.scraper."""

from __future__ import annotations

import pytest
from ora_scraper_stand import LAYOUT, IxStand

from boba.stand.scraper import Golden, ScraperStandDatabase, StandFile


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
