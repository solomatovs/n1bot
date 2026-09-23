"""Источник Oracle для общего цикла скрапера boba.ix_core.scrape: одно thin-соединение
python-oracledb на попытку, ворота файлов по версии словаря из sys.registry$, строки
запроса потоком. Область снятия — фрагменты Scope, которые файлы scrape/ берут
плейсхолдерами `{owners}` и `{objects}`. Всё по README пакета.

Ошибки:
ScrapeSourceError — сервер недоступен (сеть, listener, вход) или отклонил запрос.
ScrapeWorkerError — из ядра: ix недоступен, контракт файлов нарушен, попытки
    исчерпаны.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from oracledb import AsyncConnection

from boba.db.oracle import OracleError, OracleQueryError
from boba.db.oracle.connection import OracleConfig
from boba.db.oracle.payload import PayloadOracle
from boba.db.oracle.query import OraQueryBuilder, OraSql
from boba.ix_core.scrape import (
    BlockStream,
    CopyFormat,
    ScrapeFile,
    ScraperConfigBase,
    ScrapeSession,
    ScrapeSource,
    ScrapeSourceError,
    SourceAddressBase,
    SourceBlocks,
    SourceConfigBase,
    parse_version,
    run_cli,
)

__all__ = [
    "OraSource",
    "ScraperConfig",
    "SourceAddress",
    "SourceConfig",
    "read_server_version",
]

SCHEME = "oracle"
SECTION = "ix.ora_meta_scraper"
PROG = "boba-ora-meta-scraper"
DESCRIPTION = "Снятие словаря Oracle в граф ix: схема пакета, scrape, раскладка, merge."


class Scope(StrEnum):
    """Область снятия: пользовательские схемы и обычные объекты словаря."""

    OWNERS = (
        "(select u.user# from sys.user$ u where u.type# = 1 "
        "and bitand(nvl(u.spare1, 0), 256) = 0)"
    )
    OBJECTS = (
        "o.subname is null and o.linkname is null and o.remoteowner is null "
        "and bitand(o.flags, 128) = 0"
    )


class SourceConfig(SourceConfigBase):
    """Источник снятия: имя для выбора из командной строки и профиль подключения."""

    oracle: OracleConfig


class ScraperConfig(ScraperConfigBase[SourceConfig]):
    """Секция [ix.ora_meta_scraper]: база ix и список источников. Границы сессии
    источника (connect_timeout, call_timeout) задаёт его профиль."""

    def scrape_source(self, item: SourceConfig) -> ScrapeSource:
        return OraSource(item.oracle)


@dataclass(frozen=True, kw_only=True)
class SourceAddress(SourceAddressBase):
    """Scope источника: сервис, к которому идёт соединение, лежит в database."""

    scheme: str = SCHEME
    database: str


def source_address(oracle: OracleConfig) -> SourceAddress:
    return SourceAddress(host=oracle.host, port=oracle.port, database=oracle.service)


async def read_server_version(
    payload: PayloadOracle, conn: AsyncConnection, where: str
) -> tuple[int, ...]:
    query = "select version from sys.registry$ where cid = 'CATALOG'"

    try:
        async with payload.rows(conn, query) as stream:
            async for row in stream.blocks:
                return parse_version(str(row[0]))
    except OracleQueryError as exc:
        raise ScrapeSourceError(f"{query} on {where}: {exc}") from exc

    raise ScrapeSourceError(f"{query} on {where}: expected one row, got none")


class OraSession(ScrapeSession):
    """Сессия источника: открытое соединение и версия словаря."""

    def __init__(
        self,
        payload: PayloadOracle,
        conn: AsyncConnection,
        server: tuple[int, ...],
        where: str,
    ) -> None:
        self._payload = payload
        self._conn = conn
        self._server = server
        self._where = where

    def applies(self, file: ScrapeFile) -> bool:
        return file.applies(self._server, "")

    @asynccontextmanager
    async def fetch_blocks(
        self, name: str, path: Path, params: Mapping[str, Sequence[object]]
    ) -> AsyncGenerator[SourceBlocks, None]:
        """CSV пачек строк драйвера как csv-формат COPY."""
        if params:
            listed = ", ".join(params)
            raise ScrapeSourceError(
                f"query {name} on {self._where}: oracle scrape files take no "
                f"params (lists are not bindable), got {listed}"
            )

        query = (
            OraQueryBuilder(owners=OraSql(Scope.OWNERS), objects=OraSql(Scope.OBJECTS))
            .read(path)
            .build()
        )

        label = f"{name} ({path.name}) on {self._where}"
        try:
            async with self._payload.csv(self._conn, query.text) as stream:
                yield BlockStream(
                    stream.names,
                    CopyFormat.CSV,
                    stream.blocks,
                    label,
                    (OracleQueryError,),
                )
        except OracleQueryError as exc:
            raise ScrapeSourceError(f"query {label}: {exc}") from exc


class OraSource(ScrapeSource):
    """Реализация ScrapeSource для Oracle: thin-соединение по профилю boba-db-oracle,
    словарь читается из системных таблиц SYS.*$ по точечным грантам."""

    def __init__(self, cfg: OracleConfig) -> None:
        self._cfg = cfg
        self._payload = PayloadOracle(cfg)
        self._address = source_address(cfg)

    @property
    def files(self) -> Sequence[ScrapeFile]:
        return (
            ScrapeFile(name="database", wave=1, query="1_database.sql"),
            ScrapeFile(name="tablespaces", wave=1, query="1_tablespaces.sql"),
            ScrapeFile(name="users", wave=1, query="1_users.sql"),
            ScrapeFile(name="ccol", wave=2, query="2_ccol.sql"),
            ScrapeFile(name="cdef", wave=2, query="2_cdef.sql"),
            ScrapeFile(name="columns", wave=2, query="2_columns.sql"),
            ScrapeFile(name="comments", wave=2, query="2_comments.sql"),
            ScrapeFile(name="con", wave=2, query="2_con.sql"),
            ScrapeFile(name="dependencies", wave=2, query="2_dependencies.sql"),
            ScrapeFile(name="icol", wave=2, query="2_icol.sql"),
            ScrapeFile(name="indexes", wave=2, query="2_indexes.sql"),
            ScrapeFile(name="mviews", wave=2, query="2_mviews.sql"),
            ScrapeFile(name="objects", wave=2, query="2_objects.sql"),
            ScrapeFile(name="partcol", wave=2, query="2_partcol.sql"),
            ScrapeFile(name="partobj", wave=2, query="2_partobj.sql"),
            ScrapeFile(name="sequences", wave=2, query="2_sequences.sql"),
            ScrapeFile(name="synonyms", wave=2, query="2_synonyms.sql"),
            ScrapeFile(name="tables", wave=2, query="2_tables.sql"),
            ScrapeFile(name="triggers", wave=2, query="2_triggers.sql"),
            ScrapeFile(name="views", wave=2, query="2_views.sql"),
        )

    @property
    def address(self) -> SourceAddress:
        return self._address

    def describe(self) -> str:
        return self._cfg.address_prefix()

    async def _configure(self, conn: AsyncConnection) -> None:
        """Сессия в UTC и с точкой как десятичным разделителем: даты и числа в CSV
        не должны зависеть от NLS сервера."""
        for statement in (
            "alter session set time_zone = 'UTC'",
            "alter session set nls_numeric_characters = '.,'",
        ):
            async with self._payload.rows(conn, statement):
                pass

    @asynccontextmanager
    async def open_session(self) -> AsyncGenerator[ScrapeSession, None]:
        try:
            async with self._payload.opened() as conn:
                await self._configure(conn)
                server = await read_server_version(self._payload, conn, self.describe())
                yield OraSession(self._payload, conn, server, self.describe())
        except OracleError as exc:
            raise ScrapeSourceError(
                f"connecting to {self.describe()} as {self._cfg.trace()}: {exc}"
            ) from exc


async def main() -> None:
    package_dir = Path(__file__).resolve().parent
    await run_cli(PROG, DESCRIPTION, SECTION, package_dir, ScraperConfig)


def cli() -> None:
    """Точка входа консольного скрипта: единственный asyncio.run на процесс."""
    asyncio.run(main())


if __name__ == "__main__":
    asyncio.run(main())
