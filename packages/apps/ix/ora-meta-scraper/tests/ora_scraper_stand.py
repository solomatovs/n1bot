"""Помощники стенда скрапера Oracle: секция [ix_stand] со списком ora_sources,
набор EDGE_DEMO, инварианты, отпечатки и прогон скрапера.

Лежит отдельным модулем, а не в conftest: имя conftest у каждого пакета своё, и при
общем прогоне нескольких пакетов импорт из него достаётся чужому файлу.
"""

from __future__ import annotations

from collections.abc import Sequence
from enum import StrEnum
from pathlib import Path

from oracledb import AsyncConnection
from psycopg import sql
from pydantic import BaseModel, ConfigDict, SecretStr

from boba.db.oracle import OracleQueryError
from boba.db.oracle.payload import PayloadOracle
from boba.db.oracle.profile import OracleConfig, PasswordAuth
from boba.db.postgres import AsyncPostgresPool
from boba.db.postgres.query import PgQueryBuilder
from boba.ix_core.scrape import (
    ApplyRow,
    VersionGate,
    parse_version,
    scrape_source,
)
from boba.ora_meta_scraper import worker as scraper
from boba.ora_meta_scraper.worker import OraSource, source_address
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


class DemoUser(StrEnum):
    """Схема демонстрационного набора и её пароль на стенде."""

    NAME = "EDGE_DEMO"
    PASSWORD = "edge_demo"


class IxSource(BaseModel):
    """Один источник стенда: имя цели, профиль скрапера и профиль администратора,
    которым пересоздаётся схема EDGE_DEMO. demo говорит, пересоздавать ли набор: у
    чужого сервера прав на это нет, он снимается как есть."""

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


class IxStand(SharedIxStand):
    """Секция [ix_stand] скрапера Oracle: общий стенд ix плюс список ora_sources."""

    ora_sources: Sequence[IxSource]

    def source(self, name: str) -> IxSource:
        for item in self.ora_sources:
            if item.name == name:
                return item

        raise IxStandError(
            f"ix stand: oracle source {name!r} is not listed in [{'ix_stand'}]"
        )


class DdlFile(VersionGate):
    """Файл демонстрационного набора: ровно один statement, ворота по версии
    объявлены в стенде, текст уходит серверу как есть."""

    name: str


class DemoDataset:
    """Пересоздаёт схему EDGE_DEMO на источнике из stand/ddl, выбирая файлы по версии
    сервера: администратор пересоздаёт пользователя, объекты создаёт сам EDGE_DEMO."""

    def __init__(self, source: IxSource) -> None:
        self._source = source

    async def recreate(self) -> tuple[int, ...]:
        async with PayloadOracle.opened_config(self._source.admin) as admin:
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
        async with PayloadOracle.opened_config(self._source.demo_owner) as owner:
            for file in files:
                if not file.applies(server, ""):
                    continue

                path = StandFile.DDL_DIR.under_stand() / file.name
                await self._run(owner, path.read_text(encoding="utf-8"))

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

    @staticmethod
    async def _run(conn: AsyncConnection, statement: str) -> None:
        async with PayloadOracle.rows(conn, statement):
            pass

    @staticmethod
    async def _version(conn: AsyncConnection) -> tuple[int, ...]:
        query = "select version from sys.registry$ where cid = 'CATALOG'"
        async with PayloadOracle.rows(conn, query) as stream:
            async for row in stream.blocks:
                return parse_version(str(row[0]))

        raise IxStandError(f"ix stand: {query}: expected one row, got none")


class Fingerprint(BaseModel):
    """Канонический отпечаток одного источника: число строк и md5."""

    model_config = ConfigDict(frozen=True)

    rows: int
    digest: str

    @classmethod
    def parse(cls, raw: str) -> Fingerprint:
        rows, digest = raw.split()
        return cls(rows=int(rows), digest=digest)

    def render(self) -> str:
        return f"{self.rows} {self.digest}"


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

        return Fingerprint.parse(str(row[0]))

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
            OraSource(source.oracle),
            PACKAGE_DIR,
            3,
        )
        return report.rows
