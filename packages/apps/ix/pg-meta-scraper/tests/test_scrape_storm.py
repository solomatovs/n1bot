"""Шторм: десятки одновременных прогонов по всем целям стенда в одну базу ix, поверх
процесс, который обрывает случайные сессии. После шторма контрольный проход обязан
привести каждый scope к эталону без нарушений инвариантов и без deadlock."""

from __future__ import annotations

import random
import threading
import time
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from typing import ClassVar

import psycopg
import pytest
from conftest import DemoDataset, Golden, IxDatabase, IxSource, IxStand
from pydantic import BaseModel

from boba.pg_meta_scraper.worker import Pipeline, ScrapeWorkerError

pytestmark = pytest.mark.load

STAND = IxStand.required()


class StormOutcome(BaseModel):
    """Итог одного прогона внутри шторма."""

    source: str
    ok: bool
    error: str = ""


class Killer:
    """Каждые 200 мс обрывает две случайные сессии скрапера на базе ix. Ошибка потока не
    глотается: поднимается при выходе из контекста."""

    PERIOD_SEC: ClassVar[float] = 0.2
    VICTIMS: ClassVar[int] = 2

    def __init__(self, dsn: str, database: str) -> None:
        self._dsn = dsn
        self._database = database
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._error: BaseException | None = None
        self.kills = 0

    def __enter__(self) -> Killer:
        self._thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        self._stop.set()
        self._thread.join()
        if self._error is not None:
            raise AssertionError(
                f"killer thread failed: {self._error}"
            ) from self._error

    def _run(self) -> None:
        try:
            self._loop()
        except BaseException as exc:
            self._error = exc

    def _loop(self) -> None:
        with psycopg.connect(self._dsn, autocommit=True) as conn:
            while not self._stop.is_set():
                rows = conn.execute(
                    "select pg_terminate_backend(pid) from pg_stat_activity "
                    "where application_name = %(app)s and datname = %(db)s and pid <> "
                    "pg_backend_pid()"
                    "order by random() limit %(n)s",
                    {"app": Pipeline.APP_NAME, "db": self._database, "n": self.VICTIMS},
                ).fetchall()
                self.kills += len(rows)
                time.sleep(self.PERIOD_SEC)


class Storm:
    """Пул потоков, каждый гоняет скрапер по случайным целям заданное число раз."""

    THREADS: ClassVar[int] = 60
    RUNS_PER_THREAD: ClassVar[int] = 3

    def __init__(self, database: IxDatabase, sources: Sequence[IxSource]) -> None:
        self._database = database
        self._sources = list(sources)

    def run(self) -> list[StormOutcome]:
        with ThreadPoolExecutor(max_workers=self.THREADS) as pool:
            futures = [
                pool.submit(self._thread_runs, seed) for seed in range(self.THREADS)
            ]
            outcomes: list[StormOutcome] = []
            for future in futures:
                outcomes.extend(future.result())
        return outcomes

    def _thread_runs(self, seed: int) -> list[StormOutcome]:
        rng = random.Random(seed)  # noqa: S311 — выбор цели шторма, не крипто
        outcomes: list[StormOutcome] = []
        for _ in range(self.RUNS_PER_THREAD):
            source = rng.choice(self._sources)
            outcomes.append(self._one(source))
        return outcomes

    def _one(self, source: IxSource) -> StormOutcome:
        try:
            self._database.scrape(source)
        except ScrapeWorkerError as exc:
            return StormOutcome(source=source.name, ok=False, error=str(exc)[:120])
        return StormOutcome(source=source.name, ok=True)


class Deadlocks:
    """Счётчик deadlock базы ix из pg_stat_database."""

    def __init__(self, dsn: str, database: str) -> None:
        self._dsn = dsn
        self._database = database

    def count(self) -> int:
        with psycopg.connect(self._dsn) as conn:
            row = conn.execute(
                "select deadlocks from pg_stat_database where datname = %(db)s",
                {"db": self._database},
            ).fetchone()
        if row is None:
            raise AssertionError(f"pg_stat_database has no row for {self._database}")
        return int(row[0])


class TestScrapeStorm:
    def test_storm_then_control_pass_reaches_golden(
        self, ix_stand: IxStand, ix_database: IxDatabase, golden: Golden
    ) -> None:
        for source in ix_stand.sources:
            DemoDataset(source).recreate()

        deadlocks = Deadlocks(ix_stand.ix_dsn, ix_stand.database)
        before = deadlocks.count()

        with Killer(ix_stand.ix_database_dsn, ix_stand.database) as killer:
            outcomes = Storm(ix_database, ix_stand.sources).run()

        succeeded = sum(1 for o in outcomes if o.ok)
        failed = [o for o in outcomes if not o.ok]
        assert succeeded > 0, (
            f"storm: no run succeeded; first errors: {[o.error for o in failed[:5]]}"
        )
        assert killer.kills > 0, (
            "storm: the killer terminated nothing, the storm did not overlap"
        )

        assert ix_database.invariants() == {}, (
            "storm: invariants broken right after the storm"
        )

        for source in ix_stand.sources:
            ix_database.scrape(source)

        assert ix_database.invariants() == {}, "control pass: invariants broken"
        assert deadlocks.count() - before == 0, "deadlocks happened during the storm"

        for source in ix_stand.sources:
            assert ix_database.scope_nodes(source.host) > 0, (
                f"{source.name}: scope is empty after control pass"
            )
            if golden.has(source.name):
                assert ix_database.fingerprint(source.host) == golden.of(source.name), (
                    f"{source.name}: fingerprint differs"
                )
