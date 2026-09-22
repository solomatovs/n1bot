"""Источник PostgreSQL/Greenplum для общего цикла скрапера boba.ix_core.scrape:
сессия источника только на чтение с таймаутами, ворота файлов по
server_version_num и признаку Greenplum, серверный курсор на каждый запрос.
Всё по README пакета, шаги 1–8.

Ошибки:
ScrapeSourceError — источник недоступен (сеть, kerberos, libpq) или отклонил
    запрос; ScrapeSourceBusyError — замок или сериализация на источнике, прогон
    повторяется.
ScrapeWorkerError — из ядра: ix недоступен, контракт файлов нарушен, попытки
    исчерпаны.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from enum import StrEnum
from pathlib import Path
from typing import Any, ClassVar

import psycopg
from psycopg import sql
from psycopg.errors import LockNotAvailable, SerializationFailure
from pydantic import BaseModel, ConfigDict

from boba.db.postgres import AsyncPostgresPool, PostgresError
from boba.db.postgres.profile import PostgresConfig
from boba.ix_core.scrape import (
    ScraperConfigBase,
    ScrapeSession,
    ScrapeSource,
    ScrapeSourceBusyError,
    ScrapeSourceError,
    SourceAddressBase,
    SourceConfigBase,
    SourceRows,
    run_cli,
)
from boba.kerberos import KerberosError

__all__ = [
    "PgSource",
    "ScraperConfig",
    "ServerInfo",
    "SourceAddress",
    "SourceConfig",
    "VersionGate",
    "WorkerConfig",
]

SECTION = "ix.meta_scraper"
PROG = "boba-pg-meta-scraper"
DESCRIPTION = (
    "Снятие каталога PostgreSQL или Greenplum в граф ix: схема пакета, "
    "scrape, раскладка, merge."
)


class GateHeader(StrEnum):
    """Заголовки ворот файла по серверу."""

    MIN = "min"
    MAX = "max"
    ONLY = "only"
    NOT = "not"


class Marker(StrEnum):
    GP = "gp"
    GREENPLUM = "Greenplum"
    SCHEME = "postgresql"


class SourceConfig(SourceConfigBase):
    """Источник снятия: имя для выбора из командной строки и профиль подключения."""

    postgres: PostgresConfig


class WorkerConfig(BaseModel):
    """Один прогон: откуда снимаем и границы сессии источника."""

    model_config = ConfigDict(frozen=True)

    source: PostgresConfig
    lock_timeout: str = "2s"
    statement_timeout: str = "30s"


class ScraperConfig(ScraperConfigBase[SourceConfig]):
    """Секция [ix.meta_scraper]: база ix, список источников и границы прогона.

    lock_timeout и statement_timeout ограничивают сессию источника; границы сессии
    ix задаёт postgres.options той же секции.
    """

    lock_timeout: str = "2s"
    statement_timeout: str = "30s"

    def scrape_source(self, item: SourceConfig) -> ScrapeSource:
        return PgSource(
            WorkerConfig(
                source=item.postgres,
                lock_timeout=self.lock_timeout,
                statement_timeout=self.statement_timeout,
            )
        )


class SourceAddress(SourceAddressBase):
    scheme: str = Marker.SCHEME
    database: str

    @classmethod
    def of(cls, postgres: PostgresConfig) -> SourceAddress:
        host = postgres.host
        if host is None:
            host = postgres.hostaddr

        if host is None:
            raise ScrapeSourceError(
                f"source {postgres.where()}: expected host or hostaddr in the profile"
            )

        if postgres.port is None:
            raise ScrapeSourceError(
                f"source {postgres.where()}: expected port in the profile"
            )

        if postgres.dbname is None:
            raise ScrapeSourceError(
                f"source {postgres.where()}: expected dbname in the profile"
            )

        return cls(host=host, port=postgres.port, database=postgres.dbname)


class ServerInfo(BaseModel):
    version_num: int
    is_greenplum: bool

    @classmethod
    async def of(cls, conn: psycopg.AsyncConnection[Any]) -> ServerInfo:
        cur = await conn.execute("show server_version_num")
        record = await cur.fetchone()
        version_cur = await conn.execute("select version()")
        version_record = await version_cur.fetchone()
        if record is None or version_record is None:
            raise ScrapeSourceError(
                "source: expected server_version_num and version(), got none"
            )

        return cls(
            version_num=int(record[0]),
            is_greenplum=Marker.GREENPLUM in str(version_record[0]),
        )


class VersionGate(BaseModel):
    """Ворота файла по серверу: заголовки @min, @max, @only gp, @not gp. Базовый класс
    для DDL стенда в тестах."""

    min_version: int = 0
    max_version: int = 999999999
    only_gp: bool = False
    not_gp: bool = False

    @classmethod
    def gate_of(cls, headers: Mapping[str, str]) -> VersionGate:
        return VersionGate(
            min_version=int(headers.get(GateHeader.MIN, "0")),
            max_version=int(headers.get(GateHeader.MAX, "999999999")),
            only_gp=headers.get(GateHeader.ONLY) == Marker.GP,
            not_gp=headers.get(GateHeader.NOT) == Marker.GP,
        )

    def applies(self, server: ServerInfo) -> bool:
        if server.version_num < self.min_version:
            return False

        if server.version_num > self.max_version:
            return False

        if self.only_gp and not server.is_greenplum:
            return False

        gp_excluded = self.not_gp and server.is_greenplum

        return not gp_excluded


class PgRows(SourceRows):
    """Строки серверного курсора; ошибка чтения уходит ScrapeSourceError."""

    def __init__(self, cur: psycopg.AsyncCursor[Any], name: str, where: str) -> None:
        self._cur = cur
        self._name = name
        self._where = where

    @property
    def columns(self) -> Sequence[str]:
        names: list[str] = []
        for column in self._cur.description or ():
            names.append(column.name)

        return names

    async def __aiter__(self) -> AsyncIterator[Sequence[object]]:
        try:
            async for row in self._cur:
                yield row
        except (LockNotAvailable, SerializationFailure) as exc:
            raise ScrapeSourceBusyError(
                f"reading {self._name} from {self._where}: {exc}"
            ) from exc
        except psycopg.Error as exc:
            raise ScrapeSourceError(
                f"reading {self._name} from {self._where}: {type(exc).__name__}: {exc}"
            ) from exc


class PgSession(ScrapeSession):
    """Сессия источника: соединение только на чтение и версия сервера."""

    ITERSIZE: ClassVar[int] = 2000

    def __init__(
        self, conn: psycopg.AsyncConnection[Any], server: ServerInfo, where: str
    ) -> None:
        self._conn = conn
        self._server = server
        self._where = where

    @property
    def server(self) -> ServerInfo:
        return self._server

    def applies(self, headers: Mapping[str, str]) -> bool:
        return VersionGate.gate_of(headers).applies(self._server)

    @asynccontextmanager
    async def fetch_rows(
        self, name: str, query: str, params: Mapping[str, Sequence[object]]
    ) -> AsyncGenerator[SourceRows, None]:
        async with self._conn.transaction(), self._conn.cursor(name=name) as cur:
            cur.itersize = self.ITERSIZE
            try:
                await cur.execute(query.encode("utf-8"), dict(params))
            except (LockNotAvailable, SerializationFailure) as exc:
                raise ScrapeSourceBusyError(f"query on {self._where}: {exc}") from exc
            except psycopg.Error as exc:
                raise ScrapeSourceError(
                    f"query on {self._where}: {type(exc).__name__}: {exc}; "
                    f"query: {query[:200]!r}"
                ) from exc

            yield PgRows(cur, name, self._where)


class PgSource(ScrapeSource):
    """Реализация ScrapeSource для PostgreSQL и Greenplum: отдельное соединение в
    autocommit, lock_timeout (с 9.3) и statement_timeout из конфига, транзакции
    только на чтение."""

    LOCK_TIMEOUT_SINCE: ClassVar[int] = 90300

    def __init__(self, cfg: WorkerConfig) -> None:
        self._cfg = cfg
        self._address = SourceAddress.of(cfg.source)

    @property
    def address(self) -> SourceAddress:
        return self._address

    def describe(self) -> str:
        return self._cfg.source.where()

    @asynccontextmanager
    async def open_session(self) -> AsyncGenerator[ScrapeSession, None]:
        try:
            conn = await AsyncPostgresPool.dedicated(self._cfg.source)
        except (psycopg.Error, PostgresError, KerberosError) as exc:
            raise ScrapeSourceError(
                f"connecting to {self.describe()} as {self._cfg.source.trace()}: "
                f"{type(exc).__name__}: {exc}"
            ) from exc

        async with conn:
            try:
                server = await ServerInfo.of(conn)
                await self._configure(conn, server)
            except psycopg.Error as exc:
                raise ScrapeSourceError(
                    f"preparing session on {self.describe()}: "
                    f"{type(exc).__name__}: {exc}"
                ) from exc

            yield PgSession(conn, server, self.describe())

    async def _configure(
        self, conn: psycopg.AsyncConnection[Any], server: ServerInfo
    ) -> None:
        if server.version_num >= self.LOCK_TIMEOUT_SINCE:
            await conn.execute(
                sql.SQL("set lock_timeout = {}").format(
                    sql.Literal(self._cfg.lock_timeout)
                )
            )

        await conn.execute(
            sql.SQL("set statement_timeout = {}").format(
                sql.Literal(self._cfg.statement_timeout)
            )
        )
        await conn.execute("set default_transaction_read_only = on")


def main() -> None:
    package_dir = Path(__file__).resolve().parent
    run_cli(PROG, DESCRIPTION, SECTION, package_dir, ScraperConfig)


if __name__ == "__main__":
    main()
