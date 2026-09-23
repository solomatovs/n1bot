"""Прогон скрапера по всем целям стенда Oracle: демонстрационный набор
пересоздаётся на источнике, снимается в чистую базу ix, проверяются инварианты,
эталонный отпечаток, ссылки по формулам поверхностей и повторный прогон."""

from __future__ import annotations

import pytest
from ora_scraper_stand import IxStand

from boba.stand.scraper import Golden, ScraperStandDatabase

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

STAND = IxStand.required()


class TestScrapeStand:
    @pytest.mark.parametrize("name", [source.name for source in STAND.listed()])
    async def test_source_lands_consistent_and_equal_to_golden(
        self,
        name: str,
        ix_stand: IxStand,
        ix_database: ScraperStandDatabase,
        golden: Golden,
    ) -> None:
        source = ix_stand.source(name)
        if source.demo:
            await source.demo_dataset().recreate()

        summary = await ix_database.scrape(source)
        applied = sum(row.applied for row in summary)
        assert applied > 0, f"{name}: first run applied nothing"

        assert await ix_database.invariants() == {}, f"{name}: invariants broken"

        audit = await ix_database.audit_urls(source.host, "oracle")
        assert audit.problems == (), f"{name}: urls: {audit.problems}"
        assert audit.seen > 0, f"{name}: no nodes to build urls for"

        if golden.has(name):
            fingerprint = await ix_database.fingerprint(source.host)
            assert fingerprint == golden.of(name), (
                f"{name}: fingerprint differs from golden: {fingerprint.render()}"
            )

        second = await ix_database.scrape(source)
        changed = [row.op for row in second if row.applied != 0]
        assert changed == [], f"{name}: second run changed {changed}"
