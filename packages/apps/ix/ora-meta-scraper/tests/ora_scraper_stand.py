"""Помощники стенда скрапера Oracle: секция [ix_stand] со списком ora_sources,
набор EDGE_DEMO, инварианты, отпечатки и прогон скрапера.

Лежит отдельным модулем, а не в conftest: имя conftest у каждого пакета своё, и при
общем прогоне нескольких пакетов импорт из него достаётся чужому файлу.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Sequence
from enum import StrEnum
from pathlib import Path
from typing import ClassVar

from oracledb import AsyncConnection
from psycopg import sql
from pydantic import BaseModel, ConfigDict, SecretStr

from boba.db.oracle import OracleQueryError
from boba.db.oracle.payload import PayloadOracle
from boba.db.oracle.profile import OracleConfig, PasswordAuth
from boba.db.postgres import AsyncPostgresPool
from boba.ix_core.schema_name import SchemaName
from boba.ix_core.scrape import (
    ApplyRow,
    parse_headers,
    parse_version,
    scrape_source,
    version_applies,
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
            f"ix stand: oracle source {name!r} is not listed in [{self.SECTION}]"
        )


class DdlFile(BaseModel):
    """Файл демонстрационного набора с воротами по версии в заголовках; statement'ы
    разделены строкой из одного символа `/`, как в скриптах sqlplus, поэтому блоки
    PL/SQL с точками с запятой внутри остаются целыми."""

    model_config = ConfigDict(frozen=True)

    STATEMENT_END: ClassVar[re.Pattern[str]] = re.compile(r"^/\s*$", re.M)

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
    """Пересоздаёт схему EDGE_DEMO на источнике из stand/ddl, выбирая файлы по версии
    сервера: администратор пересоздаёт пользователя, объекты создаёт сам EDGE_DEMO."""

    DROP_USER: ClassVar[str] = f"drop user {DemoUser.NAME} cascade"
    CREATE_USER: ClassVar[str] = (
        f"create user {DemoUser.NAME} identified by {DemoUser.PASSWORD} "
        "default tablespace users quota unlimited on users"
    )
    GRANTS: ClassVar[str] = (
        "grant create session, create table, create view, create materialized view, "
        "create sequence, create synonym, create trigger, create procedure, "
        f"create type to {DemoUser.NAME}"
    )
    NO_SUCH_USER: ClassVar[str] = "ORA-01918"

    def __init__(self, source: IxSource) -> None:
        self._source = source
        self._files = [
            DdlFile.parse(p)
            for p in sorted(StandFile.DDL_DIR.under_stand().glob("*.sql"))
        ]

    async def recreate(self) -> tuple[int, ...]:
        async with PayloadOracle.opened_config(self._source.admin) as admin:
            server = await self._version(admin)
            await self._recreate_user(admin)

        async with PayloadOracle.opened_config(self._source.demo_owner) as owner:
            for file in self._files:
                if not file.applies(server):
                    continue
                for statement in file.statements():
                    await self._run(owner, statement)

        return server

    async def _recreate_user(self, admin: AsyncConnection) -> None:
        try:
            await self._run(admin, self.DROP_USER)
        except OracleQueryError as exc:
            if self.NO_SUCH_USER not in str(exc):
                raise

        await self._run(admin, self.CREATE_USER)
        await self._run(admin, self.GRANTS)

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
            OraSource(source.oracle),
            PACKAGE_DIR,
            self.ATTEMPTS,
        )
        return report.rows
