"""Потоковость скрапера Oracle: словарь на сотни объектов против словаря на десятки
тысяч. Каждый прогон идёт в своём процессе, и пик его RSS не должен расти с объёмом
словаря: строки текут из курсора Oracle в COPY по одной, а не собираются в память.

Ошибки стенда: IxStandError — секции [ix_stand] нет, модуль пропускается.
"""

from __future__ import annotations

import asyncio
import multiprocessing
from concurrent.futures import ProcessPoolExecutor

import pytest
from ora_scraper_stand import PACKAGE_DIR, DemoDataset, IxSource, IxStand

from boba.db.oracle.payload import PayloadOracle
from boba.db.oracle.profile import OracleConfig
from boba.ix_core.database import IxDatabase
from boba.ix_core.scrape import scrape_source
from boba.ora_meta_scraper.worker import OraSource

pytestmark = [pytest.mark.load, pytest.mark.anyio]

STAND = IxStand.required()

BULK_TABLES = 2000
BULK_COLUMNS = 10
ATTEMPTS = 3
RSS_SLACK_MIB = 48
GROWTH_RATIO = 1.25
BULK_OVER_SMALL = 10

BULK_DDL = """
begin
    for i in 1..{tables} loop
        execute immediate 'create table edge_demo.bulk_' || i
            || ' (id number(10) primary key, {columns})';
        execute immediate 'comment on table edge_demo.bulk_' || i
            || ' is ''Bulk table ' || i || ' of the streaming stand''';
        execute immediate 'create index edge_demo.bulk_' || i
            || '_ix on edge_demo.bulk_' || i || ' (c1, c2)';
    end loop;
end;
"""


def bulk_columns(count: int) -> str:
    kinds = ["varchar2(50)", "number(18, 2)", "date", "timestamp(6)", "clob"]
    pieces: list[str] = []
    for number in range(1, count + 1):
        pieces.append(f"c{number} {kinds[number % len(kinds)]}")

    return ", ".join(pieces)


async def create_bulk(source: IxSource, tables: int) -> None:
    """Тысячи таблиц с колонками, ключом, комментарием и индексом одним PL/SQL."""
    statement = BULK_DDL.format(tables=tables, columns=bulk_columns(BULK_COLUMNS))
    async with (
        PayloadOracle.opened_config(source.demo_owner) as owner,
        PayloadOracle.rows(owner, statement),
    ):
        pass


def scrape_in_child(database: IxDatabase, oracle: OracleConfig) -> tuple[int, int]:
    """Вход процесса прогона: сколько строк применено и пик RSS процесса в MiB."""
    report = asyncio.run(
        scrape_source(database, OraSource(oracle), PACKAGE_DIR, ATTEMPTS)
    )

    return report.applied(), report.peak_rss_mib


def scrape_apart(database: IxDatabase, oracle: OracleConfig) -> tuple[int, int]:
    """Прогон в свежем процессе, чтобы пик RSS был пиком одного прогона."""
    with ProcessPoolExecutor(
        max_workers=1,
        mp_context=multiprocessing.get_context("spawn"),
        max_tasks_per_child=1,
    ) as pool:
        return pool.submit(scrape_in_child, database, oracle).result()


class TestStreamingMemory:
    @pytest.mark.parametrize(
        "name", [source.name for source in STAND.ora_sources if source.demo]
    )
    async def test_peak_rss_does_not_grow_with_dictionary_size(
        self, name: str, ix_stand: IxStand, ix_database: object
    ) -> None:
        source = ix_stand.source(name)
        database = ix_stand.ix_database

        await DemoDataset(source).recreate()
        applied_small, peak_small = await asyncio.to_thread(
            scrape_apart, database, source.oracle
        )

        await create_bulk(source, BULK_TABLES)
        applied_bulk, peak_bulk = await asyncio.to_thread(
            scrape_apart, database, source.oracle
        )

        await DemoDataset(source).recreate()

        print(
            f"\n{name}: demo {applied_small} rows at {peak_small} MiB, "
            f"bulk {applied_bulk} rows at {peak_bulk} MiB"
        )
        assert applied_bulk >= applied_small * BULK_OVER_SMALL, (
            f"{name}: bulk applied {applied_bulk} rows against {applied_small}"
        )
        assert peak_bulk <= peak_small * GROWTH_RATIO + RSS_SLACK_MIB, (
            f"{name}: bulk dictionary peaked at {peak_bulk} MiB against "
            f"{peak_small} MiB for the demo one ({applied_bulk} vs {applied_small} "
            "rows applied)"
        )
