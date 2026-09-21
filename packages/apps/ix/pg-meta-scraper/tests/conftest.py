"""Фикстуры стенда скрапера; модели и помощники лежат в scraper_stand."""

from __future__ import annotations

import pytest
from scraper_stand import Golden, IxStand, IxStandDatabase


@pytest.fixture(scope="session")
def ix_stand() -> IxStand:
    return IxStand.required()


@pytest.fixture(scope="session")
async def ix_database(ix_stand: IxStand) -> IxStandDatabase:
    database = IxStandDatabase(ix_stand)
    await database.recreate_for_scraper()
    return database


@pytest.fixture(scope="session")
def golden() -> Golden:
    return Golden()
