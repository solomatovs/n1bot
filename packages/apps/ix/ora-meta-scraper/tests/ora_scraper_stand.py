"""Стенд скрапера Oracle: секция [ix_stand] со списком ora_sources и набор EDGE_DEMO
на источнике. Раскладка стенда, эталоны, база ix, проверка ссылок и шторм — общие,
в boba.stand.scraper.

Лежит отдельным модулем, а не в conftest: имя conftest у каждого пакета своё, и при
общем прогоне нескольких пакетов импорт из него достаётся чужому файлу.
"""

from __future__ import annotations

from collections.abc import Sequence
from enum import StrEnum
from pathlib import Path

from oracledb import AsyncConnection
from pydantic import BaseModel, ConfigDict, SecretStr

from boba.db.oracle import OracleQueryError
from boba.db.oracle.payload import PayloadOracle
from boba.db.oracle.profile import OracleConfig, PasswordAuth
from boba.ix_core.scrape import ScrapeSource, parse_version
from boba.ora_meta_scraper import worker as scraper
from boba.ora_meta_scraper.worker import OraSource, source_address
from boba.stand.ix import IxStandError
from boba.stand.scraper import DdlFile, DemoRecreate, ScraperStand, StandLayout

__all__ = ["LAYOUT", "DemoDataset", "IxSource", "IxStand"]

LAYOUT = StandLayout(
    stand_dir=Path(__file__).resolve().parent / "stand",
    package_dir=Path(scraper.__file__).resolve().parent,
)


class DemoUser(StrEnum):
    """Схема демонстрационного набора и её пароль на стенде."""

    NAME = "EDGE_DEMO"
    PASSWORD = "edge_demo"


class IxSource(BaseModel):
    """Один источник стенда: имя цели, профиль скрапера и профиль администратора,
    которым пересоздаётся схема EDGE_DEMO. demo говорит, пересоздавать ли набор: у
    чужого сервера прав на это нет, он снимается как есть. Совместим с
    boba.stand.scraper.StandSource."""

    model_config = ConfigDict(frozen=True)

    name: str
    oracle: OracleConfig
    admin: OracleConfig
    demo: bool = True

    @property
    def host(self) -> str:
        return source_address(self.oracle).host

    @property
    def demo_owner(self) -> OracleConfig:
        """Профиль владельца набора: тот же сервер, учётка EDGE_DEMO."""
        auth = PasswordAuth(
            method="password",
            user=DemoUser.NAME.value,
            password=SecretStr(DemoUser.PASSWORD.value),
        )
        return self.admin.model_copy(update={"auth": auth})

    def scrape_source(self) -> ScrapeSource:
        return OraSource(self.oracle)

    def demo_dataset(self) -> DemoRecreate:
        return DemoDataset(self)


class IxStand(ScraperStand[IxSource]):
    """Секция [ix_stand] скрапера Oracle: общий стенд ix плюс список ora_sources."""

    ora_sources: Sequence[IxSource]

    def listed(self) -> Sequence[IxSource]:
        return self.ora_sources


class DemoDataset:
    """Пересоздаёт схему EDGE_DEMO на источнике из stand/ddl, выбирая файлы по версии
    сервера: администратор пересоздаёт пользователя, объекты создаёт сам EDGE_DEMO."""

    def __init__(self, source: IxSource) -> None:
        self._source = source
        self._admin = PayloadOracle(source.admin)
        self._owner = PayloadOracle(source.demo_owner)

    async def recreate(self) -> tuple[int, ...]:
        async with self._admin.opened() as admin:
            server = await self._version(admin)
            await self._recreate_user(admin)

        files = (
            DdlFile(name="01_01_table_customers.sql"),
            DdlFile(name="01_02_comment_table_customers.sql"),
            DdlFile(name="01_03_comment_stmt_email.sql"),
            DdlFile(name="01_04_comment_column_customers_balance.sql"),
            DdlFile(name="01_05_index_customers_name_ix.sql"),
            DdlFile(name="01_06_sequence_customer_seq.sql"),
            DdlFile(name="02_01_table_orders.sql"),
            DdlFile(name="02_02_comment_table_orders.sql"),
            DdlFile(name="02_03_comment_column_orders_customer_id.sql"),
            DdlFile(name="02_04_index_orders_customer_ix.sql"),
            DdlFile(name="02_05_index_orders_status_bx.sql"),
            DdlFile(name="02_06_table_order_items.sql"),
            DdlFile(name="02_07_table_order_staging.sql"),
            DdlFile(name="03_01_view_customer_orders.sql"),
            DdlFile(name="03_02_comment_table_customer_orders.sql"),
            DdlFile(name="03_03_view_open_orders.sql"),
            DdlFile(name="03_04_view_daily_sales.sql"),
            DdlFile(name="03_05_comment_view_daily_sales.sql"),
            DdlFile(name="03_06_index_daily_sales_day_ix.sql"),
            DdlFile(name="03_07_synonym_cust.sql"),
            DdlFile(name="03_08_synonym_all_sales.sql"),
            DdlFile(name="04_01_trigger_orders_biu.sql"),
            DdlFile(name="04_02_trigger_open_orders_ioi.sql"),
            DdlFile(name="04_03_function_order_total.sql"),
            DdlFile(name="04_04_procedure_close_order.sql"),
            DdlFile(name="04_05_package_order_api.sql"),
            DdlFile(name="04_06_package_body_order_api.sql"),
            DdlFile(name="04_07_type_money_t.sql"),
            DdlFile(name="05_01_table_sales.sql", min_version=(18,)),
            DdlFile(name="05_02_comment_table_sales.sql", min_version=(18,)),
            DdlFile(name="05_03_index_sales_region_ix.sql", min_version=(18,)),
        )
        async with self._owner.opened() as owner:
            for file in files:
                if not file.applies(server, ""):
                    continue

                path = LAYOUT.ddl(file.name)
                async with self._owner.rows(owner, path.read_text(encoding="utf-8")):
                    pass

        return server

    async def _recreate_user(self, admin: AsyncConnection) -> None:
        try:
            await self._run(admin, f"drop user {DemoUser.NAME} cascade")
        except OracleQueryError as exc:
            if "ORA-01918" not in str(exc):
                raise

        await self._run(
            admin,
            f"create user {DemoUser.NAME} identified by {DemoUser.PASSWORD} "
            "default tablespace users quota unlimited on users",
        )
        await self._run(
            admin,
            "grant create session, create table, create view, create materialized "
            "view, create sequence, create synonym, create trigger, create procedure, "
            f"create type to {DemoUser.NAME}",
        )

    async def _run(self, conn: AsyncConnection, statement: str) -> None:
        async with self._admin.rows(conn, statement):
            pass

    async def _version(self, conn: AsyncConnection) -> tuple[int, ...]:
        query = "select version from sys.registry$ where cid = 'CATALOG'"
        async with self._admin.rows(conn, query) as stream:
            async for row in stream.blocks:
                return parse_version(str(row[0]))

        raise IxStandError(f"ix stand: {query}: expected one row, got none")
