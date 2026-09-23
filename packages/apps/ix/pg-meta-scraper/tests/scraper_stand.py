"""Стенд скрапера PostgreSQL: секция [ix_stand] со списком sources и набор edge_demo
на источнике. Раскладка стенда, эталоны, база ix, проверка ссылок и шторм — общие,
в boba.stand.scraper.

Лежит отдельным модулем, а не в conftest: имя conftest у каждого пакета своё, и при
общем прогоне нескольких пакетов импорт из него достаётся чужому файлу.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any, ClassVar

import psycopg
from psycopg import sql
from pydantic import BaseModel, ConfigDict

from boba.db.postgres import AsyncPostgresPool
from boba.db.postgres.connection import PostgresConfig
from boba.db.postgres.query import PgQueryBuilder
from boba.ix_core.scrape import ScrapeSource
from boba.pg_meta_scraper import worker as scraper
from boba.pg_meta_scraper.worker import (
    PgSource,
    ServerInfo,
    WorkerConfig,
    source_address,
)
from boba.stand.ix import IxStandError
from boba.stand.scraper import DdlFile, DemoRecreate, ScraperStand, StandLayout

__all__ = ["LAYOUT", "DemoDataset", "IxSource", "IxStand"]

LAYOUT = StandLayout(
    stand_dir=Path(__file__).resolve().parent / "stand",
    package_dir=Path(scraper.__file__).resolve().parent,
)


class IxSource(BaseModel):
    """Один источник стенда: имя цели и профиль его служебной базы; демонстрационный
    набор всегда живёт в edge_demo. Совместим с boba.stand.scraper.StandSource."""

    model_config = ConfigDict(frozen=True)

    DEMO_DB: ClassVar[str] = "edge_demo"

    name: str
    postgres: PostgresConfig
    demo: bool = True

    def profile_of(self, dbname: str) -> PostgresConfig:
        return self.postgres.model_copy(update={"dbname": dbname})

    @property
    def demo_profile(self) -> PostgresConfig:
        return self.profile_of(self.DEMO_DB)

    @property
    def host(self) -> str:
        return source_address(self.postgres).host

    def scrape_source(self) -> ScrapeSource:
        return PgSource(WorkerConfig(source=self.demo_profile))

    def demo_dataset(self) -> DemoRecreate:
        return DemoDataset(self)


class IxStand(ScraperStand[IxSource]):
    """Секция [ix_stand] скрапера: общий стенд ix плюс список sources."""

    sources: Sequence[IxSource]

    def listed(self) -> Sequence[IxSource]:
        return self.sources


class DemoDataset:
    """Пересоздаёт edge_demo на источнике из stand/ddl, выбирая варианты по версии
    сервера."""

    def __init__(self, source: IxSource) -> None:
        self._source = source

    async def recreate(self) -> ServerInfo:
        async with await AsyncPostgresPool.dedicated(self._source.postgres) as conn:
            server = await self._server(conn)
            query = (
                PgQueryBuilder()
                .add(
                    "drop database if exists {db}", db=sql.Identifier(IxSource.DEMO_DB)
                )
                .build()
            )
            await conn.execute(query.text, query.params)
            query = (
                PgQueryBuilder()
                .add("create database {db}", db=sql.Identifier(IxSource.DEMO_DB))
                .build()
            )
            await conn.execute(query.text, query.params)

        files = (
            DdlFile(name="01_base.sql", min_version=(80300,)),
            DdlFile(
                name="02a_customers_identity.sql", min_version=(100000,), unless="gp"
            ),
            DdlFile(name="02b_customers_serial.sql", max_version=(99999,), unless="gp"),
            DdlFile(name="02c_customers_gp.sql", only="gp"),
            DdlFile(name="03_products.sql", min_version=(80300,), unless="gp"),
            DdlFile(name="03b_products_gp.sql", only="gp"),
            DdlFile(name="04_orders.sql", min_version=(80300,)),
            DdlFile(name="04a_orders_index_include.sql", min_version=(110000,)),
            DdlFile(name="04b_orders_index_noinclude.sql", max_version=(109999,)),
            DdlFile(name="04c_orders_statistics.sql", min_version=(100000,)),
            DdlFile(name="05_order_items.sql", min_version=(80300,)),
            DdlFile(name="05a_generated_stored.sql", min_version=(120000,)),
            DdlFile(name="05b_generated_virtual.sql", min_version=(180000,)),
            DdlFile(name="06a_shipments_setnullcols.sql", min_version=(150000,)),
            DdlFile(name="06b_shipments_setnull.sql", max_version=(149999,)),
            DdlFile(name="07a_bookings_range.sql", min_version=(90200,), unless="gp"),
            DdlFile(name="07b_bookings_box.sql", max_version=(90199,)),
            DdlFile(name="07c_bookings_gp.sql", only="gp"),
            DdlFile(name="08_invoices.sql", min_version=(80300,)),
            DdlFile(name="09a_events_pg11.sql", min_version=(110000,)),
            DdlFile(
                name="09b_events_pg10.sql", min_version=(100000,), max_version=(109999,)
            ),
            DdlFile(name="09c_events_inherit.sql", max_version=(99999,)),
            DdlFile(name="10_cities.sql", min_version=(80300,)),
            DdlFile(name="11_views.sql", min_version=(80300,)),
            DdlFile(name="11a_matview.sql", min_version=(90300,)),
            DdlFile(name="12_functions.sql", min_version=(80300,)),
            DdlFile(name="12a_begin_atomic.sql", min_version=(140000,)),
            DdlFile(name="90_greenplum.sql", only="gp"),
        )
        async with await AsyncPostgresPool.dedicated(self._source.demo_profile) as conn:
            for file in files:
                if not file.applies((server.version_num,), server.flavor()):
                    continue

                path = LAYOUT.ddl(file.name)
                await conn.execute(path.read_text(encoding="utf-8").encode("utf-8"))

        return server

    @staticmethod
    async def _server(conn: psycopg.AsyncConnection[Any]) -> ServerInfo:
        version_cur = await conn.execute("show server_version_num")
        version = await version_cur.fetchone()
        banner_cur = await conn.execute("select version()")
        banner = await banner_cur.fetchone()
        if version is None or banner is None:
            raise IxStandError(
                "source: expected server_version_num and version(), got none"
            )

        return ServerInfo(
            version_num=int(version[0]), is_greenplum="Greenplum" in str(banner[0])
        )
