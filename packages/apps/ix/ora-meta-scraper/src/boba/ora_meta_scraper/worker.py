"""Источник Oracle для общего цикла скрапера boba.ix_core.scrape: одно thin-соединение
python-oracledb на попытку, ворота файлов по версии словаря из sys.registry$, строки
запроса потоком. Всё по README пакета.

Ошибки:
ScrapeSourceError — сервер недоступен (сеть, listener, вход) или отклонил запрос.
ScrapeWorkerError — из ядра: ix недоступен, контракт файлов нарушен, попытки
    исчерпаны.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Mapping, Sequence
from contextlib import asynccontextmanager
from pathlib import Path

from oracledb import AsyncConnection

from boba.db.oracle import OracleError, OracleQueryError
from boba.db.oracle.payload import PayloadOracle
from boba.db.oracle.profile import OracleConfig
from boba.ix_core.scrape import (
    ScraperConfigBase,
    ScrapeSession,
    ScrapeSource,
    ScrapeSourceError,
    SourceAddressBase,
    SourceConfigBase,
    SourceRows,
    StreamRows,
    parse_version,
    run_cli,
    version_applies,
)

__all__ = [
    "OraSource",
    "ScraperConfig",
    "SourceAddress",
    "SourceConfig",
    "read_server_version",
]

SCHEME = "oracle"
VERSION_QUERY = "select version from sys.registry$ where cid = 'CATALOG'"
SECTION = "ix.ora_meta_scraper"
PROG = "boba-ora-meta-scraper"
DESCRIPTION = "Снятие словаря Oracle в граф ix: схема пакета, scrape, раскладка, merge."


class SourceConfig(SourceConfigBase):
    """Источник снятия: имя для выбора из командной строки и профиль подключения."""

    oracle: OracleConfig


class ScraperConfig(ScraperConfigBase[SourceConfig]):
    """Секция [ix.ora_meta_scraper]: база ix и список источников. Границы сессии
    источника (connect_timeout, call_timeout) задаёт его профиль."""

    def scrape_source(self, item: SourceConfig) -> ScrapeSource:
        return OraSource(item.oracle)


class SourceAddress(SourceAddressBase):
    """Scope источника: сервис, к которому идёт соединение, лежит в database."""

    scheme: str = SCHEME
    database: str


def source_address(oracle: OracleConfig) -> SourceAddress:
    return SourceAddress(host=oracle.host, port=oracle.port, database=oracle.service)


async def read_server_version(conn: AsyncConnection, where: str) -> tuple[int, ...]:
    try:
        async with PayloadOracle.rows(conn, VERSION_QUERY) as stream:
            async for row in stream.blocks:
                return parse_version(str(row[0]))
    except OracleQueryError as exc:
        raise ScrapeSourceError(f"{VERSION_QUERY} on {where}: {exc}") from exc

    raise ScrapeSourceError(f"{VERSION_QUERY} on {where}: expected one row, got none")


class OraSession(ScrapeSession):
    """Сессия источника: открытое соединение и версия словаря."""

    def __init__(
        self, conn: AsyncConnection, server: tuple[int, ...], where: str
    ) -> None:
        self._conn = conn
        self._server = server
        self._where = where

    def applies(self, headers: Mapping[str, str]) -> bool:
        return version_applies(headers, self._server)

    @asynccontextmanager
    async def fetch_rows(
        self, name: str, query: str, params: Mapping[str, Sequence[object]]
    ) -> AsyncGenerator[SourceRows, None]:
        if params:
            listed = ", ".join(params)
            raise ScrapeSourceError(
                f"query {name} on {self._where}: oracle scrape files take no "
                f"@params (lists are not bindable), got {listed}"
            )

        try:
            async with PayloadOracle.rows(self._conn, query) as stream:
                yield StreamRows(
                    stream.names, stream.blocks, name, self._where, (OracleQueryError,)
                )
        except OracleQueryError as exc:
            raise ScrapeSourceError(f"query {name} on {self._where}: {exc}") from exc


class OraSource(ScrapeSource):
    """Реализация ScrapeSource для Oracle: thin-соединение по профилю boba-db-oracle,
    словарь читается из системных таблиц SYS.*$ по точечным грантам."""

    def __init__(self, cfg: OracleConfig) -> None:
        self._cfg = cfg
        self._address = source_address(cfg)

    @property
    def address(self) -> SourceAddress:
        return self._address

    def describe(self) -> str:
        return self._cfg.where()

    @asynccontextmanager
    async def open_session(self) -> AsyncGenerator[ScrapeSession, None]:
        try:
            async with PayloadOracle.opened_config(self._cfg) as conn:
                server = await read_server_version(conn, self.describe())
                yield OraSession(conn, server, self.describe())
        except OracleError as exc:
            raise ScrapeSourceError(
                f"connecting to {self.describe()} as {self._cfg.trace()}: {exc}"
            ) from exc


def main() -> None:
    package_dir = Path(__file__).resolve().parent
    run_cli(PROG, DESCRIPTION, SECTION, package_dir, ScraperConfig)


if __name__ == "__main__":
    main()
