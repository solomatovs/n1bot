"""Стенд скрапера ClickHouse: секция [ix_stand] со списком ch_sources и набор
edge_demo на источнике. Раскладка стенда, эталоны, база ix, проверка ссылок и
шторм — общие, в boba.stand.scraper.

Лежит отдельным модулем, а не в conftest: имя conftest у каждого пакета своё, и при
общем прогоне нескольких пакетов импорт из него достаётся чужому файлу.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import ClassVar

from pydantic import BaseModel, ConfigDict

from boba.ch_meta_scraper import worker as scraper
from boba.ch_meta_scraper.worker import ChSource, source_address
from boba.db.clickhouse.connection import ClickHouseConfig, ClickHouseSettingsConfig
from boba.db.clickhouse.payload import PayloadClickHouse
from boba.db.clickhouse.query import ChQueryBuilder
from boba.ix_core.scrape import ScrapeSource, parse_version
from boba.stand.scraper import DdlFile, DemoRecreate, ScraperStand, StandLayout

__all__ = ["LAYOUT", "DemoDataset", "IxSource", "IxStand"]

LAYOUT = StandLayout(
    stand_dir=Path(__file__).resolve().parent / "stand",
    package_dir=Path(scraper.__file__).resolve().parent,
)


class IxSource(BaseModel):
    """Один источник стенда: имя цели и профиль сервера. demo говорит, пересоздавать
    ли на нём набор edge_demo: у чужого сервера (kerberos dev-кластер) прав на это
    нет, он снимается как есть. Совместим с boba.stand.scraper.StandSource."""

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

    def scrape_source(self) -> ScrapeSource:
        return ChSource(self.clickhouse)

    def demo_dataset(self) -> DemoRecreate:
        return DemoDataset(self)


class IxStand(ScraperStand[IxSource]):
    """Секция [ix_stand] скрапера ClickHouse: общий стенд ix плюс список ch_sources."""

    ch_sources: Sequence[IxSource]

    def listed(self) -> Sequence[IxSource]:
        return self.ch_sources


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
                .add(
                    "drop database if exists {db:Identifier}",
                    db=IxSource.DEMO_DB,
                )
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

                path = LAYOUT.ddl(file.name)
                await client.command(path.read_text(encoding="utf-8"))

        return server
