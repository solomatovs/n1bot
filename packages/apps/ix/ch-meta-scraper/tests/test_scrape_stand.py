"""Прогон скрапера по всем целям стенда ClickHouse: демонстрационный набор
пересоздаётся на источнике, снимается в чистую базу ix, проверяются инварианты,
эталонный отпечаток, ссылки по формулам поверхностей и повторный прогон."""

from __future__ import annotations

from urllib.parse import parse_qsl, urlsplit

import pytest
from ch_scraper_stand import DemoDataset, Golden, IxStand, IxStandDatabase

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

STAND = IxStand.required()


class TestScrapeStand:
    @pytest.mark.parametrize("name", [source.name for source in STAND.ch_sources])
    async def test_source_lands_consistent_and_equal_to_golden(
        self,
        name: str,
        ix_stand: IxStand,
        ix_database: IxStandDatabase,
        golden: Golden,
    ) -> None:
        source = ix_stand.source(name)
        if source.demo:
            await DemoDataset(source).recreate()

        summary = await ix_database.scrape(source)
        applied = sum(row.applied for row in summary)
        assert applied > 0, f"{name}: first run applied nothing"

        assert await ix_database.invariants() == {}, f"{name}: invariants broken"

        await self._check_urls(ix_database, source.host)

        if golden.has(name):
            fingerprint = await ix_database.fingerprint(source.host)
            assert fingerprint == golden.of(name), (
                f"{name}: fingerprint differs from golden: {fingerprint.render()}"
            )

        second = await ix_database.scrape(source)
        changed = [row.op for row in second if row.applied != 0]
        assert changed == [], f"{name}: second run changed {changed}"

    @staticmethod
    async def _check_urls(ix_database: IxStandDatabase, host: str) -> None:
        """Формула ссылки объявлена скрапером, поэтому его же прогон её и проверяет:
        у каждой node этого источника ссылка собралась и роли в ней те же, что в
        адресе. База стенда копит узлы всех целей, поэтому чужие пропускаем."""
        urls = await ix_database.urls()
        seen = 0
        for surface, address in await ix_database.nodes():
            if address.get("host") != host:
                continue

            seen += 1
            url = urls.url_of(surface, address)
            assert url, f"{surface}: no url formula for {address}"

            split = urlsplit(url)
            assert split.scheme == "clickhouse", f"{surface}: {url}"
            assert split.hostname == host, f"{surface}: {url}"

            expected_path = ""
            if "database" in address:
                expected_path = "/" + address["database"]
            assert split.path == expected_path, f"{surface}: {url}"

            roles = dict(parse_qsl(split.query, keep_blank_values=True))
            for role, value in roles.items():
                assert address.get(role) == value, f"{surface}: {url} vs {address}"

        assert seen > 0, f"{host}: no nodes to build urls for"
