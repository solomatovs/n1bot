"""Помощники стенда скрапера ClickHouse: секция [ix_stand] со списком ch_sources,
набор edge_demo, инварианты, отпечатки и прогон скрапера.

Лежит отдельным модулем, а не в conftest: имя conftest у каждого пакета своё, и при
общем прогоне нескольких пакетов импорт из него достаётся чужому файлу.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Sequence
from enum import StrEnum
from pathlib import Path
from typing import ClassVar

from psycopg import sql
from pydantic import BaseModel, ConfigDict

from boba.ch_meta_scraper import worker as scraper
from boba.ch_meta_scraper.worker import ChSource, source_address
from boba.db.clickhouse.payload import PayloadClickHouse
from boba.db.clickhouse.profile import ClickHouseConfig, ClickHouseSettingsConfig
from boba.db.postgres import AsyncPostgresPool
from boba.ix_core.schema_name import SchemaName
from boba.ix_core.scrape import (
    ApplyRow,
    parse_headers,
    parse_version,
    scrape_source,
    version_applies,
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
            f"ix stand: clickhouse source {name!r} is not listed in [{self.SECTION}]"
        )


class DdlFile(BaseModel):
    """Файл демонстрационного набора с воротами по версии в заголовках; statement'ы
    разделены точкой с запятой в конце строки, HTTP-интерфейс принимает по одному."""

    model_config = ConfigDict(frozen=True)

    STATEMENT_END: ClassVar[re.Pattern[str]] = re.compile(r";\s*$", re.M)

    path: Path
    text: str
    headers: dict[str, str]

    @classmethod
    def parse(cls, path: Path) -> DdlFile:
        text = path.read_text(encoding="utf-8")
        return cls(path=path, text=text, headers=parse_headers(text))

    def applies(self, server: tuple[int, ...]) -> bool:
        return version_applies(self.headers, server)

    def statements(self) -> Iterator[str]:
        for piece in self.STATEMENT_END.split(self.text):
            statement = piece.strip()
            if statement:
                yield statement


class DemoDataset:
    """Пересоздаёт edge_demo на источнике из stand/ddl, выбирая файлы по версии
    сервера."""

    def __init__(self, source: IxSource) -> None:
        self._source = source
        self._files = [
            DdlFile.parse(p)
            for p in sorted(StandFile.DDL_DIR.under_stand().glob("*.sql"))
        ]

    async def recreate(self) -> tuple[int, ...]:
        async with PayloadClickHouse.opened_config(self._source.admin) as client:
            result = await client.query("select version()")
            first = next(iter(result.result_rows))
            server = parse_version(str(first[0]))

            await client.command(f"drop database if exists {IxSource.DEMO_DB}")
            await client.command(f"create database {IxSource.DEMO_DB}")

            for file in self._files:
                if not file.applies(server):
                    continue
                for statement in file.statements():
                    await client.command(statement)

        return server


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

    ATTEMPTS: ClassVar[int] = 3

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
        report = await scrape_source(
            self._stand.ix_database,
            ChSource(source.clickhouse),
            PACKAGE_DIR,
            self.ATTEMPTS,
        )
        return report.rows
