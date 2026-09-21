"""Прогон скрапера по всем целям стенда: демонстрационный набор пересоздаётся на
источнике, снимается в чистую базу ix, проверяются инварианты, эталонный отпечаток
и повторный прогон."""

from __future__ import annotations

import pytest
from conftest import DemoDataset, Golden, IxStand, IxStandDatabase

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

STAND = IxStand.required()


class TestScrapeStand:
    @pytest.mark.parametrize("name", [source.name for source in STAND.sources])
    async def test_source_lands_consistent_and_equal_to_golden(
        self,
        name: str,
        ix_stand: IxStand,
        ix_database: IxStandDatabase,
        golden: Golden,
    ) -> None:
        source = ix_stand.source(name)
        await DemoDataset(source).recreate()

        summary = await ix_database.scrape(source)
        applied = sum(row.applied for row in summary)
        assert applied > 0, f"{name}: first run applied nothing"

        assert await ix_database.invariants() == {}, f"{name}: invariants broken"

        if golden.has(name):
            fingerprint = await ix_database.fingerprint(source.host)
            assert fingerprint == golden.of(name), (
                f"{name}: fingerprint differs from golden"
            )

        second = await ix_database.scrape(source)
        changed = [row.op for row in second if row.applied != 0]
        assert changed == [], f"{name}: second run changed {changed}"
