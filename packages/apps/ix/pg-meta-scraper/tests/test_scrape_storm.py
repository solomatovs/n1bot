"""Шторм: десятки одновременных прогонов по всем целям стенда PostgreSQL в одну базу
ix, поверх задача, которая обрывает случайные сессии. После шторма контрольный проход
обязан привести каждый scope к эталону без нарушений инвариантов и без deadlock."""

from __future__ import annotations

import pytest
from scraper_stand import IxStand

from boba.stand.scraper import Golden, ScraperStandDatabase, Storm

pytestmark = [pytest.mark.load, pytest.mark.anyio]

STAND = IxStand.required()


class TestScrapeStorm:
    async def test_storm_then_control_pass_reaches_golden(
        self, ix_stand: IxStand, ix_database: ScraperStandDatabase, golden: Golden
    ) -> None:
        report = await Storm(ix_stand, ix_database).run()

        first_errors = [outcome.error for outcome in report.failures[:5]]
        assert report.succeeded > 0, (
            f"storm: no run succeeded; first errors: {first_errors}"
        )
        assert report.kills > 0, (
            "storm: the killer terminated nothing, the storm did not overlap"
        )
        assert report.invariants_after_storm == {}, (
            "storm: invariants broken right after the storm"
        )
        assert report.invariants_after_control == {}, "control pass: invariants broken"
        assert report.deadlocks == 0, "deadlocks happened during the storm"

        for source in ix_stand.listed():
            assert await ix_database.scope_nodes(source.host) > 0, (
                f"{source.name}: scope is empty after control pass"
            )
            if golden.has(source.name):
                fingerprint = await ix_database.fingerprint(source.host)
                assert fingerprint == golden.of(source.name), (
                    f"{source.name}: fingerprint differs: {fingerprint.render()}"
                )
