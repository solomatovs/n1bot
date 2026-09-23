"""Стенд скраперов каталога: общая часть стендов pg/ch/ora-meta-scraper.

Пакетный стенд даёт модель источника (профили, host, ScrapeSource) и пересоздание
своего демонстрационного набора; всё остальное живёт здесь: раскладка каталога
stand/, файл DDL с воротами по версии, отпечаток и эталоны golden.txt, база ix с
инвариантами и прогоном, проверка ссылок и шторм одновременных прогонов.

Ошибки:
IxStandError — файл стенда не найден, запрос стенда вернул пустоту, задача
    шторма упала.
"""

from __future__ import annotations

import asyncio
import logging
import random
from abc import abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass
from enum import IntEnum, StrEnum
from pathlib import Path
from typing import Any, ClassVar, Generic, Protocol, TypeVar
from urllib.parse import parse_qsl, urlsplit

from psycopg import sql

from boba.db.postgres import AsyncPostgresPool
from boba.db.postgres.connection import PostgresConfig
from boba.db.postgres.query import PgQueryBuilder
from boba.ix_core.scrape import (
    ApplyRow,
    ScrapeSource,
    ScrapeWorkerError,
    VersionGate,
    scrape_source,
)
from boba.stand.ix import IxStand, IxStandDatabase, IxStandError

__all__ = [
    "DdlFile",
    "DemoRecreate",
    "Fingerprint",
    "Golden",
    "ScraperStand",
    "ScraperStandDatabase",
    "StandFile",
    "StandLayout",
    "StandSource",
    "Storm",
    "StormOutcome",
    "StormReport",
    "UrlAudit",
]

logger = logging.getLogger("ix-stand")


class StandFile(StrEnum):
    """Файлы каталога stand/ пакета и каталог схемы в самом пакете."""

    CANON = "cons/canon.sql"
    CONSISTENCY = "cons/consistency.sql"
    GOLDEN = "cons/golden.txt"
    DDL_DIR = "ddl"
    SCHEMA_DIR = "schema"


class StandLayout:
    """Каталоги стенда пакета: stand/ рядом с тестами и каталог пакета, где лежат
    schema/, scrape/ и layout/ скрапера."""

    def __init__(self, stand_dir: Path, package_dir: Path) -> None:
        self._stand_dir = stand_dir
        self._package_dir = package_dir

    @property
    def package_dir(self) -> Path:
        return self._package_dir

    @property
    def schema_dir(self) -> Path:
        return self._package_dir / StandFile.SCHEMA_DIR

    def path(self, file: StandFile) -> Path:
        return self._stand_dir / file.value

    def ddl(self, name: str) -> Path:
        return self._stand_dir / StandFile.DDL_DIR / name


class StandSource(Protocol):
    """Источник стенда глазами общей части: имя цели, host в адресе node, признак
    demo (пересоздавать ли набор) и ScrapeSource для прогона. Пакетная модель
    источника (pydantic) совместима структурно."""

    @property
    def name(self) -> str: ...

    @property
    def host(self) -> str: ...

    @property
    def demo(self) -> bool: ...

    def scrape_source(self) -> ScrapeSource: ...

    def demo_dataset(self) -> DemoRecreate: ...


class DemoRecreate(Protocol):
    """Пересоздание демонстрационного набора на источнике."""

    async def recreate(self) -> object: ...


S = TypeVar("S", bound=StandSource)


class ScraperStand(IxStand, Generic[S]):
    """Секция [ix_stand] стенда скрапера: общий стенд ix плюс список источников
    пакета. Наследник объявляет поле списка своей модели и отдаёт его через
    listed()."""

    @abstractmethod
    def listed(self) -> Sequence[S]:
        """Источники стенда по порядку секции."""

    def source(self, name: str) -> S:
        for item in self.listed():
            if item.name == name:
                return item

        raise IxStandError(f"ix stand: source {name!r} is not listed in [ix_stand]")

    async def recreate_demos(self) -> None:
        """Наборы всех источников с demo заново: чужой сервер снимается как есть."""
        for source in self.listed():
            if not source.demo:
                continue

            await source.demo_dataset().recreate()


@dataclass(frozen=True, kw_only=True)
class DdlFile(VersionGate):
    """Файл демонстрационного набора: ровно один statement, ворота по версии
    объявлены в стенде, текст уходит серверу как есть."""

    name: str


@dataclass(frozen=True, kw_only=True)
class Fingerprint:
    """Канонический отпечаток одного источника: число строк и md5."""

    rows: int
    digest: str

    def render(self) -> str:
        return f"{self.rows} {self.digest}"

    @classmethod
    def parse(cls, raw: str) -> Fingerprint:
        rows, digest = raw.split()

        return cls(rows=int(rows), digest=digest)


class Golden:
    """Эталонные отпечатки golden.txt по имени цели: `имя строки md5` на строку."""

    def __init__(self, path: Path) -> None:
        self._by_name: dict[str, Fingerprint] = {}
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue

            name, rows, digest = line.split()
            self._by_name[name] = Fingerprint(rows=int(rows), digest=digest)

    def has(self, name: str) -> bool:
        return name in self._by_name

    def of(self, name: str) -> Fingerprint:
        return self._by_name[name]


class ScraperStandDatabase(IxStandDatabase):
    """База ix стенда скрапера: пересоздание с ядром и схемой пакета, инварианты,
    отпечатки и прогон источника ядром ix_core.scrape."""

    ATTEMPTS: ClassVar[int] = 3

    def __init__(self, stand: ScraperStand[Any], layout: StandLayout) -> None:
        super().__init__(stand)
        self._layout = layout

    async def recreate_for_scraper(self) -> None:
        await self.recreate([self._layout.schema_dir])

    async def invariants(self) -> dict[str, int]:
        """Инварианты структуры, у которых счётчик не ноль."""
        query = (
            PgQueryBuilder(schema=sql.Identifier(self.stand.db_schema))
            .read(self._layout.path(StandFile.CONSISTENCY))
            .build()
        )
        broken: dict[str, int] = {}
        async with self.connection() as conn:
            cur = await conn.execute(query.text, query.params)
            async for name, count in cur:
                if int(count) == 0:
                    continue

                broken[str(name)] = int(count)

        return broken

    async def fingerprint(self, host: str) -> Fingerprint:
        query = (
            PgQueryBuilder(schema=sql.Identifier(self.stand.db_schema))
            .read(self._layout.path(StandFile.CANON), host=host)
            .build()
        )
        async with self.connection() as conn:
            cur = await conn.execute(query.text, query.params)
            row = await cur.fetchone()

        if row is None:
            raise IxStandError(
                f"ix stand: fingerprint of {host}: expected one row, got none"
            )

        return Fingerprint.parse(str(row[0]))

    async def scope_nodes(self, host: str) -> int:
        query = (
            PgQueryBuilder(schema=sql.Identifier(self.stand.db_schema))
            .add(
                "select count(*) from {schema}.node where address->>'host' = %(host)s",
                host=host,
            )
            .build()
        )
        async with self.connection() as conn:
            cur = await conn.execute(query.text, query.params)
            row = await cur.fetchone()

        if row is None:
            raise IxStandError(
                f"ix stand: node count of {host}: expected one row, got none"
            )

        return int(row[0])

    async def scrape(self, source: StandSource) -> Sequence[ApplyRow]:
        report = await scrape_source(
            self.stand.ix_database,
            source.scrape_source(),
            self._layout.package_dir,
            self.ATTEMPTS,
        )

        return report.rows

    async def audit_urls(self, host: str, scheme: str) -> UrlAudit:
        """Ссылки всех node источника по формулам его поверхностей: формула
        объявлена скрапером, поэтому его же прогон её и проверяет. База стенда
        копит узлы всех целей, чужие пропускаются."""
        urls = await self.urls()
        seen = 0
        problems: list[str] = []
        for surface, address in await self.nodes():
            if address.get("host") != host:
                continue

            seen += 1
            url = urls.url_of(surface, address)
            if not url:
                problems.append(f"{surface}: no url formula for {address}")
                continue

            split = urlsplit(url)
            if split.scheme != scheme:
                problems.append(f"{surface}: scheme {split.scheme!r} in {url}")

            if split.hostname != host:
                problems.append(f"{surface}: host {split.hostname!r} in {url}")

            expected_path = ""
            if "database" in address:
                expected_path = "/" + address["database"]

            if split.path != expected_path:
                problems.append(f"{surface}: path {split.path!r} in {url}")

            roles = dict(parse_qsl(split.query, keep_blank_values=True))
            for role, value in roles.items():
                if address.get(role) == value:
                    continue

                problems.append(f"{surface}: {role}={value!r} in {url} vs {address}")

        return UrlAudit(seen=seen, problems=tuple(problems))


@dataclass(frozen=True, kw_only=True)
class UrlAudit:
    """Итог проверки ссылок одного источника: сколько node проверено и что не так."""

    seen: int
    problems: Sequence[str]


@dataclass(frozen=True, kw_only=True)
class StormOutcome:
    """Итог одного прогона внутри шторма."""

    source: str
    ok: bool
    error: str = ""


@dataclass(frozen=True, kw_only=True)
class StormReport:
    """Итог шторма: прогоны, обрывы, инварианты после шторма и после контрольного
    прохода, прирост deadlock. Проверяет тест."""

    outcomes: Sequence[StormOutcome]
    kills: int
    deadlocks: int
    invariants_after_storm: dict[str, int]
    invariants_after_control: dict[str, int]

    @property
    def succeeded(self) -> int:
        count = 0
        for outcome in self.outcomes:
            if outcome.ok:
                count += 1

        return count

    @property
    def failures(self) -> Sequence[StormOutcome]:
        failed: list[StormOutcome] = []
        for outcome in self.outcomes:
            if outcome.ok:
                continue

            failed.append(outcome)

        return failed


class Killer:
    """Каждые 200 мс обрывает две случайные сессии скрапера на базе ix. Ошибка
    задачи не глотается: поднимается при выходе из контекста."""

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
            raise IxStandError(f"ix stand: killer task failed: {exc}") from exc

    async def _loop(self) -> None:
        query = (
            PgQueryBuilder()
            .add(
                """
                select pg_terminate_backend(pid)
                from pg_stat_activity
                where application_name = %(app)s
                    and datname = %(db)s
                    and pid <> pg_backend_pid()
                order by random()
                limit %(n)s
                """,
                app=self._postgres.application_name,
                db=self._database,
                n=2,
            )
            .build()
        )
        async with await AsyncPostgresPool.dedicated(self._postgres) as conn:
            while not self._stop.is_set():
                cur = await conn.execute(query.text, query.params)
                rows = await cur.fetchall()
                self.kills += len(rows)
                await asyncio.sleep(0.2)


class Deadlocks:
    """Счётчик deadlock базы ix из pg_stat_database."""

    def __init__(self, postgres: PostgresConfig, database: str) -> None:
        self._postgres = postgres
        self._database = database

    async def count(self) -> int:
        query = (
            PgQueryBuilder()
            .add(
                "select deadlocks from pg_stat_database where datname = %(db)s",
                db=self._database,
            )
            .build()
        )
        async with await AsyncPostgresPool.dedicated(self._postgres) as conn:
            cur = await conn.execute(query.text, query.params)
            row = await cur.fetchone()

        if row is None:
            raise IxStandError(
                f"ix stand: pg_stat_database has no row for {self._database}"
            )

        return int(row[0])


class StormShape(IntEnum):
    """Размер шторма: сколько задач и сколько прогонов в каждой."""

    TASKS = 60
    RUNS_PER_TASK = 3


class Storm:
    """Шторм: десятки одновременных прогонов по всем целям стенда в одну базу ix,
    поверх задача, которая обрывает случайные сессии; затем контрольный проход по
    каждой цели. Наборы demo-источников пересоздаются перед штормом."""

    def __init__(
        self, stand: ScraperStand[Any], database: ScraperStandDatabase
    ) -> None:
        self._stand = stand
        self._database = database

    async def run(self) -> StormReport:
        await self._stand.recreate_demos()

        deadlocks = Deadlocks(self._stand.postgres, self._stand.database)
        before = await deadlocks.count()

        async with Killer(self._stand.ix_profile, self._stand.database) as killer:
            outcomes = await self._storm()

        after_storm = await self._database.invariants()

        for source in self._stand.listed():
            await self._database.scrape(source)

        after_control = await self._database.invariants()
        report = StormReport(
            outcomes=outcomes,
            kills=killer.kills,
            deadlocks=await deadlocks.count() - before,
            invariants_after_storm=after_storm,
            invariants_after_control=after_control,
        )
        logger.info(
            "storm: runs=%d succeeded=%d failed=%d killed=%d deadlocks=%d",
            len(report.outcomes),
            report.succeeded,
            len(report.failures),
            report.kills,
            report.deadlocks,
        )
        for outcome in report.failures[:5]:
            logger.info("storm failure %s: %s", outcome.source, outcome.error)

        return report

    async def _storm(self) -> list[StormOutcome]:
        runs = [self._task_runs(seed) for seed in range(StormShape.TASKS)]
        outcomes: list[StormOutcome] = []
        for batch in await asyncio.gather(*runs):
            outcomes.extend(batch)

        return outcomes

    async def _task_runs(self, seed: int) -> list[StormOutcome]:
        rng = random.Random(seed)  # noqa: S311 — выбор цели шторма, не крипто
        sources = list(self._stand.listed())
        outcomes: list[StormOutcome] = []
        for _ in range(StormShape.RUNS_PER_TASK):
            source = rng.choice(sources)
            outcomes.append(await self._one(source))

        return outcomes

    async def _one(self, source: StandSource) -> StormOutcome:
        try:
            await self._database.scrape(source)
        except ScrapeWorkerError as exc:
            return StormOutcome(source=source.name, ok=False, error=str(exc)[:120])

        return StormOutcome(source=source.name, ok=True)
