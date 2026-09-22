"""Шторм: десятки одновременных прогонов по всем целям стенда Oracle в одну базу
ix, поверх задача, которая обрывает случайные сессии. После шторма контрольный проход
обязан привести каждый scope к эталону без нарушений инвариантов и без deadlock."""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Sequence
from typing import ClassVar

import pytest
from ora_scraper_stand import DemoDataset, Golden, IxSource, IxStand, IxStandDatabase
from pydantic import BaseModel

from boba.db.postgres import AsyncPostgresPool
from boba.db.postgres.profile import PostgresConfig
from boba.ix_core.scrape import ScrapeWorkerError

pytestmark = [pytest.mark.load, pytest.mark.anyio]

logger = logging.getLogger("ora-storm")

STAND = IxStand.required()


class StormOutcome(BaseModel):
    """Итог одного прогона внутри шторма."""

    source: str
    ok: bool
    error: str = ""


class Killer:
    """Каждые 200 мс обрывает две случайные сессии скрапера на базе ix. Ошибка
    задачи не глотается: поднимается при выходе из контекста."""

    PERIOD_SEC: ClassVar[float] = 0.2
    VICTIMS: ClassVar[int] = 2

    def __init__(self, postgres: PostgresConfig, database: str) -> None:
        self._postgres = postgres
        self._database = database
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self.kills = 0

    async def __aenter__(self) -> Killer:
        self._task = asyncio.create_task(self._loop())
        return self

    async def __aexit__(self, *_: object) -> None:
        self._stop.set()
        if self._task is None:
            return

        try:
            await self._task
        except Exception as exc:
            raise AssertionError(f"killer task failed: {exc}") from exc

    async def _loop(self) -> None:
        async with await AsyncPostgresPool.dedicated(self._postgres) as conn:
            while not self._stop.is_set():
                cur = await conn.execute(
                    "select pg_terminate_backend(pid) from pg_stat_activity "
                    "where application_name = %(app)s and datname = %(db)s and pid <> "
                    "pg_backend_pid()"
                    "order by random() limit %(n)s",
                    {
                        "app": self._postgres.application_name,
                        "db": self._database,
                        "n": self.VICTIMS,
                    },
                )
                rows = await cur.fetchall()
                self.kills += len(rows)
                await asyncio.sleep(self.PERIOD_SEC)


class Storm:
    """Пачка задач, каждая гоняет скрапер по случайным целям заданное число раз."""

    TASKS: ClassVar[int] = 60
    RUNS_PER_TASK: ClassVar[int] = 3

    def __init__(self, database: IxStandDatabase, sources: Sequence[IxSource]) -> None:
        self._database = database
        self._sources = list(sources)

    async def run(self) -> list[StormOutcome]:
        runs = [self._task_runs(seed) for seed in range(self.TASKS)]
        outcomes: list[StormOutcome] = []
        for batch in await asyncio.gather(*runs):
            outcomes.extend(batch)
        return outcomes

    async def _task_runs(self, seed: int) -> list[StormOutcome]:
        rng = random.Random(seed)  # noqa: S311 — выбор цели шторма, не крипто
        outcomes: list[StormOutcome] = []
        for _ in range(self.RUNS_PER_TASK):
            source = rng.choice(self._sources)
            outcomes.append(await self._one(source))
        return outcomes

    async def _one(self, source: IxSource) -> StormOutcome:
        try:
            await self._database.scrape(source)
        except ScrapeWorkerError as exc:
            return StormOutcome(source=source.name, ok=False, error=str(exc)[:120])
        return StormOutcome(source=source.name, ok=True)


class Deadlocks:
    """Счётчик deadlock базы ix из pg_stat_database."""

    def __init__(self, postgres: PostgresConfig, database: str) -> None:
        self._postgres = postgres
        self._database = database

    async def count(self) -> int:
        async with await AsyncPostgresPool.dedicated(self._postgres) as conn:
            cur = await conn.execute(
                "select deadlocks from pg_stat_database where datname = %(db)s",
                {"db": self._database},
            )
            row = await cur.fetchone()
        if row is None:
            raise AssertionError(f"pg_stat_database has no row for {self._database}")
        return int(row[0])


class TestScrapeStorm:
    async def test_storm_then_control_pass_reaches_golden(
        self, ix_stand: IxStand, ix_database: IxStandDatabase, golden: Golden
    ) -> None:
        for source in ix_stand.ora_sources:
            if source.demo:
                await DemoDataset(source).recreate()

        deadlocks = Deadlocks(ix_stand.postgres, ix_stand.database)
        before = await deadlocks.count()

        async with Killer(ix_stand.ix_profile, ix_stand.database) as killer:
            outcomes = await Storm(ix_database, ix_stand.ora_sources).run()

        succeeded = sum(1 for o in outcomes if o.ok)
        failed = [o for o in outcomes if not o.ok]
        logger.info(
            "storm: runs=%d succeeded=%d failed=%d killed=%d",
            len(outcomes),
            succeeded,
            len(failed),
            killer.kills,
        )
        for outcome in failed[:5]:
            logger.info("storm failure %s: %s", outcome.source, outcome.error)
        assert succeeded > 0, (
            f"storm: no run succeeded; first errors: {[o.error for o in failed[:5]]}"
        )
        assert killer.kills > 0, (
            "storm: the killer terminated nothing, the storm did not overlap"
        )

        assert await ix_database.invariants() == {}, (
            "storm: invariants broken right after the storm"
        )

        for source in ix_stand.ora_sources:
            await ix_database.scrape(source)

        assert await ix_database.invariants() == {}, "control pass: invariants broken"
        logger.info("storm: deadlocks=%d", await deadlocks.count() - before)
        assert await deadlocks.count() - before == 0, (
            "deadlocks happened during the storm"
        )

        for source in ix_stand.ora_sources:
            assert await ix_database.scope_nodes(source.host) > 0, (
                f"{source.name}: scope is empty after control pass"
            )
            if golden.has(source.name):
                fingerprint = await ix_database.fingerprint(source.host)
                assert fingerprint == golden.of(source.name), (
                    f"{source.name}: fingerprint differs"
                )
