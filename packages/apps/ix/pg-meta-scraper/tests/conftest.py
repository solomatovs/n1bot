"""Стенд скрапера: источники edge-контейнеров и база ix из секции [ix_stand] stand.toml,
демонстрационный набор edge_demo из stand/ddl, инварианты и отпечатки из stand/cons.

Ошибки:
IxStandError — секция [ix_stand] отсутствует или неполна, файл стенда не найден.
"""

from __future__ import annotations

from collections.abc import Sequence
from enum import StrEnum
from pathlib import Path
from typing import Any, ClassVar

import psycopg
import pytest
from psycopg import sql
from pydantic import BaseModel, ConfigDict

from boba.db.postgres import AsyncPostgresPool
from boba.db.postgres.profile import PostgresConfig
from boba.pg_ix_core.schema_name import SchemaName
from boba.pg_meta_scraper import worker as scraper
from boba.pg_meta_scraper.worker import (
    ApplyRow,
    ScrapeWorker,
    ServerInfo,
    SourceAddress,
    VersionGate,
    WorkerConfig,
)
from boba.stand.ix import IxStand as SharedIxStand
from boba.stand.ix import IxStandDatabase as SharedIxStandDatabase
from boba.stand.ix import IxStandError

REPO_ROOT = Path(__file__).resolve().parents[5]
PACKAGE_DIR = Path(scraper.__file__).resolve().parent
STAND_DIR = Path(__file__).resolve().parent / "stand"


class StandFile(StrEnum):
    CANON = "cons/canon.sql"
    CONSISTENCY = "cons/consistency.sql"
    GOLDEN = "cons/golden.txt"
    DDL_DIR = "ddl"
    SCHEMA_DIR = "schema"

    def under_repo(self) -> Path:
        return REPO_ROOT / self.value

    def under_stand(self) -> Path:
        return STAND_DIR / self.value


class IxSource(BaseModel):
    """Один источник стенда: имя цели и профиль его служебной базы; демонстрационный
    набор всегда живёт в edge_demo."""

    model_config = ConfigDict(frozen=True)

    DEMO_DB: ClassVar[str] = "edge_demo"

    name: str
    postgres: PostgresConfig

    def profile_of(self, dbname: str) -> PostgresConfig:
        return self.postgres.model_copy(update={"dbname": dbname})

    @property
    def demo(self) -> PostgresConfig:
        return self.profile_of(self.DEMO_DB)

    @property
    def host(self) -> str:
        return SourceAddress.of(self.postgres).host


class IxStand(SharedIxStand):
    """Секция [ix_stand] скрапера: общий стенд ix плюс список источников."""

    sources: Sequence[IxSource]

    def source(self, name: str) -> IxSource:
        for item in self.sources:
            if item.name == name:
                return item
        raise IxStandError(
            f"ix stand: source {name!r} is not listed in [{self.SECTION}]"
        )


class DdlFile(VersionGate):
    """Файл демонстрационного набора с воротами по версии."""

    path: Path
    text: str

    @classmethod
    def parse(cls, path: Path) -> DdlFile:
        text = path.read_text(encoding="utf-8")
        gate = cls.gate_of(cls.headers_of(text))
        return cls(path=path, text=text, **gate.model_dump())


class DemoDataset:
    """Пересоздаёт edge_demo на источнике из stand/ddl, выбирая варианты по версии
    сервера."""

    def __init__(self, source: IxSource) -> None:
        self._source = source
        self._files = [
            DdlFile.parse(p)
            for p in sorted(StandFile.DDL_DIR.under_stand().glob("*.sql"))
        ]

    async def recreate(self) -> ServerInfo:
        async with await AsyncPostgresPool.dedicated(self._source.postgres) as conn:
            server = await self._server(conn)
            await conn.execute(
                sql.SQL("drop database if exists {}").format(
                    sql.Identifier(IxSource.DEMO_DB)
                )
            )
            await conn.execute(
                sql.SQL("create database {}").format(sql.Identifier(IxSource.DEMO_DB))
            )

        async with await AsyncPostgresPool.dedicated(self._source.demo) as conn:
            for file in self._files:
                if not file.applies(server):
                    continue
                await conn.execute(file.text.encode("utf-8"))

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
        query = self._query(StandFile.CONSISTENCY)
        async with await AsyncPostgresPool.dedicated(self._stand.ix_profile) as conn:
            cur = await conn.execute(query)
            rows = await cur.fetchall()
        broken: dict[str, int] = {}
        for name, count in rows:
            if int(count) != 0:
                broken[str(name)] = int(count)
        return broken

    async def fingerprint(self, host: str) -> Fingerprint:
        query = self._query(StandFile.CANON)
        async with await AsyncPostgresPool.dedicated(self._stand.ix_profile) as conn:
            cur = await conn.execute(query, {"host": host})
            row = await cur.fetchone()
        if row is None:
            raise IxStandError(
                f"ix stand: fingerprint of {host}: expected one row, got none"
            )
        return Fingerprint.parse(str(row[0]))

    async def scope_nodes(self, host: str) -> int:
        query = SchemaName.render(
            "select count(*) from {schema}.node where address->>'host' = %(host)s",
            self._stand.db_schema,
        )
        async with await AsyncPostgresPool.dedicated(self._stand.ix_profile) as conn:
            cur = await conn.execute(query, {"host": host})
            row = await cur.fetchone()
        if row is None:
            raise IxStandError(
                f"ix stand: node count of {host}: expected one row, got none"
            )
        return int(row[0])

    def _query(self, name: StandFile) -> sql.Composed:
        """Запрос стенда под схему графа: в файлах она стоит плейсхолдером."""
        text = name.under_stand().read_text(encoding="utf-8")
        return SchemaName.render(text, self._stand.db_schema)

    async def scrape(self, source: IxSource) -> Sequence[ApplyRow]:
        cfg = WorkerConfig(
            source=source.demo,
            postgres=self._stand.ix_profile,
            krb=self._stand.krb,
            db_schema=self._stand.db_schema,
        )
        return await ScrapeWorker(cfg, PACKAGE_DIR).run()


@pytest.fixture(scope="session")
def ix_stand() -> IxStand:
    return IxStand.required()


@pytest.fixture(scope="session")
async def ix_database(ix_stand: IxStand) -> IxStandDatabase:
    database = IxStandDatabase(ix_stand)
    await database.recreate_for_scraper()
    return database


@pytest.fixture(scope="session")
def golden() -> Golden:
    return Golden()
