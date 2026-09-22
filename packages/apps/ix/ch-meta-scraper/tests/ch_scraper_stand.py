"""Помощники стенда скрапера ClickHouse: секция [ix_stand] со списком ch_sources,
набор edge_demo, инварианты, отпечатки и прогон скрапера.

Лежит отдельным модулем, а не в conftest: имя conftest у каждого пакета своё, и при
общем прогоне нескольких пакетов импорт из него достаётся чужому файлу.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import ClassVar

from psycopg import sql
from pydantic import BaseModel, ConfigDict

from boba.ch_meta_scraper import worker as scraper
from boba.ch_meta_scraper.worker import ChSource, source_address
from boba.db.clickhouse.payload import PayloadClickHouse
from boba.db.clickhouse.profile import ClickHouseConfig, ClickHouseSettingsConfig
from boba.db.clickhouse.query import ChQueryBuilder
from boba.db.postgres import AsyncPostgresPool
from boba.db.postgres.query import PgQueryBuilder
from boba.ix_core.scrape import (
    ApplyRow,
    VersionGate,
    parse_version,
    scrape_source,
)
from boba.stand.ix import IxStand as SharedIxStand
from boba.stand.ix import IxStandDatabase as SharedIxStandDatabase
from boba.stand.ix import IxStandError

__all__ = [
    "PACKAGE_DIR",
    "DdlFile",
    "DemoDataset",
    "Fingerprint",
    "Golden",
    "IxSource",
    "IxStand",
    "IxStandDatabase",
    "IxStandError",
    "StandFile",
]

PACKAGE_DIR = Path(scraper.__file__).resolve().parent
STAND_DIR = Path(__file__).resolve().parent / "stand"


class StandFile(StrEnum):
    CANON = "cons/canon.sql"
    CONSISTENCY = "cons/consistency.sql"
    GOLDEN = "cons/golden.txt"
    DDL_DIR = "ddl"
    SCHEMA_DIR = "schema"

    def under_stand(self) -> Path:
        return STAND_DIR / self.value


class IxSource(BaseModel):
    """Один источник стенда: имя цели и профиль сервера. demo говорит, пересоздавать
    ли на нём набор edge_demo: у чужого сервера (kerberos dev-кластер) прав на это
    нет, он снимается как есть."""

    model_config = ConfigDict(frozen=True)

    DEMO_DB: ClassVar[str] = "edge_demo"

    name: str
    clickhouse: ClickHouseConfig
    demo: bool = True

    @property
    def host(self) -> str:
        return source_address(self.clickhouse).host

    @property
    def admin(self) -> ClickHouseConfig:
        """Тот же профиль без readonly-настроек сессии: набор пересоздаётся DDL."""
        return self.clickhouse.model_copy(
            update={"settings": ClickHouseSettingsConfig.model_validate({})}
        )


class IxStand(SharedIxStand):
    """Секция [ix_stand] скрапера ClickHouse: общий стенд ix плюс список ch_sources."""

    ch_sources: Sequence[IxSource]

    def source(self, name: str) -> IxSource:
        for item in self.ch_sources:
            if item.name == name:
                return item

        raise IxStandError(
            f"ix stand: clickhouse source {name!r} is not listed in [{'ix_stand'}]"
        )


@dataclass(frozen=True, kw_only=True)
class DdlFile(VersionGate):
    """Файл демонстрационного набора: ровно один statement, ворота по версии
    объявлены в стенде, текст уходит серверу как есть."""

    name: str


class DemoDataset:
    """Пересоздаёт edge_demo на источнике из stand/ddl, выбирая файлы по версии
    сервера."""

    def __init__(self, source: IxSource) -> None:
        self._source = source

    async def recreate(self) -> tuple[int, ...]:
        async with PayloadClickHouse.opened_config(self._source.admin) as client:
            result = await client.query("select version()")
            first = next(iter(result.result_rows))
            server = parse_version(str(first[0]))

            drop = (
                ChQueryBuilder()
                .add("drop database if exists {db:Identifier}", db=IxSource.DEMO_DB)
                .build()
            )
            await client.command(drop.text, parameters=drop.params)

            create = (
                ChQueryBuilder()
                .add("create database {db:Identifier}", db=IxSource.DEMO_DB)
                .build()
            )
            await client.command(create.text, parameters=create.params)

            files = (
                DdlFile(name="01_01_table_customers.sql"),
                DdlFile(name="02_01_table_orders.sql"),
                DdlFile(name="03_01_table_products.sql"),
                DdlFile(name="03_02_table_events_log.sql"),
                DdlFile(name="04_01_view_v_paid.sql"),
                DdlFile(name="04_02_table_daily_sales.sql"),
                DdlFile(name="04_03_view_mv_daily_sales.sql"),
                DdlFile(name="04_04_view_mv_customer_orders.sql"),
                DdlFile(name="05_01_dictionary_dict_customers.sql"),
                DdlFile(name="06_01_function_amount_rub.sql"),
                DdlFile(name="07_01_reload_dictionary_dict_customers.sql"),
            )
            for file in files:
                if not file.applies(server, ""):
                    continue

                path = StandFile.DDL_DIR.under_stand() / file.name
                await client.command(path.read_text(encoding="utf-8"))

        return server


@dataclass(frozen=True, kw_only=True)
class Fingerprint:
    """Канонический отпечаток одного источника: число строк и md5."""

    rows: int
    digest: str

    def render(self) -> str:
        return f"{self.rows} {self.digest}"


def parse_fingerprint(raw: str) -> Fingerprint:
    rows, digest = raw.split()

    return Fingerprint(rows=int(rows), digest=digest)


class Golden:
    """Эталонные отпечатки stand/cons/golden.txt по имени цели."""

    def __init__(self) -> None:
        self._by_name: dict[str, Fingerprint] = {}
        for line in (
            StandFile.GOLDEN.under_stand().read_text(encoding="utf-8").splitlines()
        ):
            if not line.strip():
                continue
            name, rows, digest = line.split()
            self._by_name[name] = Fingerprint(rows=int(rows), digest=digest)

    def has(self, name: str) -> bool:
        return name in self._by_name

    def of(self, name: str) -> Fingerprint:
        return self._by_name[name]


class IxStandDatabase(SharedIxStandDatabase):
    """База ix стенда скрапера: общее пересоздание плюс инварианты, отпечатки и
    прогон скрапера."""

    def __init__(self, stand: IxStand) -> None:
        super().__init__(stand)
        self._stand = stand

    async def recreate_for_scraper(self) -> None:
        await self.recreate([PACKAGE_DIR / StandFile.SCHEMA_DIR])

    async def invariants(self) -> dict[str, int]:
        """Инварианты структуры, у которых счётчик не ноль."""
        query = (
            PgQueryBuilder(schema=sql.Identifier(self._stand.db_schema))
            .read(StandFile.CONSISTENCY.under_stand())
            .build()
        )
        async with await AsyncPostgresPool.dedicated(self._stand.ix_profile) as conn:
            cur = await conn.execute(query.text, query.params)
            rows = await cur.fetchall()

        broken: dict[str, int] = {}
        for name, count in rows:
            if int(count) != 0:
                broken[str(name)] = int(count)

        return broken

    async def fingerprint(self, host: str) -> Fingerprint:
        query = (
            PgQueryBuilder(schema=sql.Identifier(self._stand.db_schema))
            .read(StandFile.CANON.under_stand(), host=host)
            .build()
        )
        async with await AsyncPostgresPool.dedicated(self._stand.ix_profile) as conn:
            cur = await conn.execute(query.text, query.params)
            row = await cur.fetchone()

        if row is None:
            raise IxStandError(
                f"ix stand: fingerprint of {host}: expected one row, got none"
            )

        return parse_fingerprint(str(row[0]))

    async def scope_nodes(self, host: str) -> int:
        query = (
            PgQueryBuilder()
            .add(
                "select count(*) from {schema}.node where address->>'host' = %(host)s",
                schema=sql.Identifier(self._stand.db_schema),
                host=host,
            )
            .build()
        )
        async with await AsyncPostgresPool.dedicated(self._stand.ix_profile) as conn:
            cur = await conn.execute(query.text, query.params)
            row = await cur.fetchone()

        if row is None:
            raise IxStandError(
                f"ix stand: node count of {host}: expected one row, got none"
            )

        return int(row[0])

    async def scrape(self, source: IxSource) -> Sequence[ApplyRow]:
        report = await scrape_source(
            self._stand.ix_database,
            ChSource(source.clickhouse),
            PACKAGE_DIR,
            3,
        )
        return report.rows
