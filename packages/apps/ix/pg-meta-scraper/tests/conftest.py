"""Стенд скрапера: источники edge-контейнеров и база ix из секции [ix_stand] stand.toml,
демонстрационный набор edge_demo из stand/ddl, инварианты и отпечатки из stand/cons.

Ошибки:
IxStandError — секция [ix_stand] отсутствует или неполна, файл стенда не найден.
"""

from __future__ import annotations

import tomllib
from collections.abc import Sequence
from enum import StrEnum
from pathlib import Path
from typing import ClassVar

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from pydantic import BaseModel, ConfigDict, ValidationError

from boba.pg_ix_core import main as core
from boba.pg_ix_core.schema_name import SchemaName, StorageSchema
from boba.pg_ix_core.upgrade import SchemaUpgrade, UpgradeConfig
from boba.pg_meta_scraper import worker as scraper
from boba.pg_meta_scraper.worker import (
    ApplyRow,
    ScrapeWorker,
    ServerInfo,
    VersionGate,
    WorkerConfig,
)
from boba.runtime.config import ConfigLocator
from boba.stand.site import StandLayers

REPO_ROOT = Path(__file__).resolve().parents[5]
PACKAGE_DIR = Path(scraper.__file__).resolve().parent
CORE_SCHEMA_DIR = Path(core.__file__).resolve().parent / "schema"
STAND_DIR = Path(__file__).resolve().parent / "stand"


class IxStandError(Exception):
    """Конфиг стенда скрапера недоступен или неполон."""


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
    """Один источник стенда: имя цели и DSN без базы; база всегда edge_demo."""

    model_config = ConfigDict(frozen=True)

    DEMO_DB: ClassVar[str] = "edge_demo"
    MAINTENANCE_DB: ClassVar[str] = "postgres"

    name: str
    dsn: str

    def dsn_of(self, dbname: str) -> str:
        return make_conninfo(self.dsn, dbname=dbname)

    @property
    def demo_dsn(self) -> str:
        return self.dsn_of(self.DEMO_DB)

    @property
    def host(self) -> str:
        return str(conninfo_to_dict(self.dsn)["host"])


class IxStand(BaseModel):
    """Секция [ix_stand]: база ix для прогонов, схема графа и список источников."""

    model_config = ConfigDict(frozen=True)

    SECTION: ClassVar[str] = "ix_stand"

    ix_dsn: str
    database: str
    db_schema: str = StorageSchema.DEFAULT
    sources: Sequence[IxSource]

    @classmethod
    def load(cls) -> IxStand:
        path = ConfigLocator.path().parent / StandLayers.FILE
        if not path.is_file():
            raise IxStandError(f"ix stand: {path} not found")
        with path.open("rb") as handle:
            document = tomllib.load(handle)
        section = document.get(cls.SECTION)
        if section is None:
            raise IxStandError(f"ix stand: {path} has no [{cls.SECTION}] section")
        try:
            return cls.model_validate(section)
        except ValidationError as exc:
            raise IxStandError(f"ix stand: [{cls.SECTION}] in {path}: {exc}") from exc

    @classmethod
    def required(cls) -> IxStand:
        try:
            return cls.load()
        except IxStandError as exc:
            pytest.skip(str(exc), allow_module_level=True)

    def source(self, name: str) -> IxSource:
        for item in self.sources:
            if item.name == name:
                return item
        raise IxStandError(
            f"ix stand: source {name!r} is not listed in [{self.SECTION}]"
        )

    @property
    def ix_database_dsn(self) -> str:
        return make_conninfo(self.ix_dsn, dbname=self.database)


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

    def recreate(self) -> ServerInfo:
        with psycopg.connect(
            self._source.dsn_of(IxSource.MAINTENANCE_DB), autocommit=True
        ) as conn:
            server = self._server(conn)
            conn.execute(
                sql.SQL("drop database if exists {}").format(
                    sql.Identifier(IxSource.DEMO_DB)
                )
            )
            conn.execute(
                sql.SQL("create database {}").format(sql.Identifier(IxSource.DEMO_DB))
            )

        with psycopg.connect(self._source.demo_dsn, autocommit=True) as conn:
            for file in self._files:
                if not file.applies(server):
                    continue
                conn.execute(file.text.encode("utf-8"))

        return server

    @staticmethod
    def _server(conn: psycopg.Connection) -> ServerInfo:
        version = conn.execute("show server_version_num").fetchone()
        banner = conn.execute("select version()").fetchone()
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


class IxDatabase:
    """База ix стенда: пересоздаётся с ядром пакета pg-ix-core и схемой
    пакета."""

    def __init__(self, stand: IxStand) -> None:
        self._stand = stand

    def recreate(self) -> None:
        with psycopg.connect(self._stand.ix_dsn, autocommit=True) as conn:
            conn.execute(
                sql.SQL("drop database if exists {} with (force)").format(
                    sql.Identifier(self._stand.database)
                )
            )
            conn.execute(
                sql.SQL("create database {}").format(
                    sql.Identifier(self._stand.database)
                )
            )

        upgrade = UpgradeConfig(
            dsn=self._stand.ix_database_dsn, db_schema=self._stand.db_schema
        )
        SchemaUpgrade(CORE_SCHEMA_DIR, requires_core=False).run(upgrade)
        SchemaUpgrade(PACKAGE_DIR / StandFile.SCHEMA_DIR).run(upgrade)

    def invariants(self) -> dict[str, int]:
        """Инварианты структуры, у которых счётчик не ноль."""
        query = self._query(StandFile.CONSISTENCY)
        with psycopg.connect(self._stand.ix_database_dsn) as conn:
            rows = conn.execute(query).fetchall()
        broken: dict[str, int] = {}
        for name, count in rows:
            if int(count) != 0:
                broken[str(name)] = int(count)
        return broken

    def fingerprint(self, host: str) -> Fingerprint:
        query = self._query(StandFile.CANON)
        with psycopg.connect(self._stand.ix_database_dsn) as conn:
            row = conn.execute(query, {"host": host}).fetchone()
        if row is None:
            raise IxStandError(
                f"ix stand: fingerprint of {host}: expected one row, got none"
            )
        return Fingerprint.parse(str(row[0]))

    def scope_nodes(self, host: str) -> int:
        query = SchemaName.render(
            "select count(*) from {schema}.node where address->>'host' = %(host)s",
            self._stand.db_schema,
        )
        with psycopg.connect(self._stand.ix_database_dsn) as conn:
            row = conn.execute(query, {"host": host}).fetchone()
        if row is None:
            raise IxStandError(
                f"ix stand: node count of {host}: expected one row, got none"
            )
        return int(row[0])

    def _query(self, name: StandFile) -> bytes:
        """Запрос стенда под схему графа: в файлах она стоит плейсхолдером."""
        text = name.under_stand().read_text(encoding="utf-8")
        return SchemaName.render(text, self._stand.db_schema)

    def scrape(self, source: IxSource) -> Sequence[ApplyRow]:
        cfg = WorkerConfig(
            source_dsn=source.demo_dsn, ix_dsn=self._stand.ix_database_dsn
        )
        return ScrapeWorker(cfg, PACKAGE_DIR).run()


@pytest.fixture(scope="session")
def ix_stand() -> IxStand:
    return IxStand.required()


@pytest.fixture(scope="session")
def ix_database(ix_stand: IxStand) -> IxDatabase:
    database = IxDatabase(ix_stand)
    database.recreate()
    return database


@pytest.fixture(scope="session")
def golden() -> Golden:
    return Golden()
