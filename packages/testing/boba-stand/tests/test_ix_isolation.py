"""Пересоздание базы одного набора сохраняет таблицы другого владельца ix."""

from __future__ import annotations

import pytest

from boba.stand.ix import IxStand, IxStandDatabase
from boba.stand.ix_index import StandIxStack

pytestmark = [pytest.mark.integration, pytest.mark.anyio]


async def test_recreating_core_does_not_drop_another_owners_index_tables() -> None:
    indexed = IxStandDatabase(IxStand.required(owner="isolation_full"))
    minimal = IxStandDatabase(IxStand.required(owner="isolation_core"))
    await indexed.recreate(StandIxStack().schema_dirs())
    await minimal.recreate(())

    assert await indexed.scalar("select count(*) from {schema}.ix_trgm", {}) == 0
